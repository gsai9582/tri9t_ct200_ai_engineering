# Engineering Approach Document: CT-200 Technical Manual Pipeline & Stale Traceability

## 1. Overview & System Design
This document explains the technical architecture, design decisions, failure modes, and debugging methodology behind the CardioTrack CT-200 Medical Device Manual ingestion, versioning, and test-case generation pipeline.

The backend is built with **FastAPI**, **Pydantic v2**, **SQLAlchemy + SQLite**, and **PyMuPDF (`pymupdf`)**. The system parses unstructured PDFs into a browsable relational tree, maintains version-pinned selections with text snapshots, generates concrete QA test cases via LLM (Google Gemini) with Pydantic validation, and flags stale traceability whenever underlying requirements change in subsequent revisions.

---

## 2. Document Extraction & OCR Strategy

### Approach Selected & Why
I selected **PyMuPDF (`pymupdf`)** as the primary extraction engine combined with **layout-aware table detection (`page.find_tables()`)** and an automated **Tesseract OCR fallback (`pytesseract`)**.

**Why PyMuPDF over naive text extractors (pypdf/pdfplumber/pdfminer):**
1. **Speed & Direct C-bindings**: Fast execution (<50ms per document) with high-fidelity coordinate rendering.
2. **Native Table Recognition**: PyMuPDF 1.23+ provides built-in geometric table detection that identifies cell borders and headers, allowing us to serialize tables directly into structured Markdown tables rather than garbled line fragments.
3. **Bounding Box Isolation**: Allows extracting text blocks outside of detected table regions, preventing table cell contents from polluting or duplicating into the plain text stream.
4. **Targeted OCR Fallback**: If a page yields fewer than 40 characters (indicative of a scanned image or non-extractable text layer), the pipeline renders the page pixmap at 2x resolution (`matrix=pymupdf.Matrix(2, 2)`) and routes it through Tesseract OCR.

---

## 3. Structural Hierarchy Reconstruction

The CT-200 manual is not a cleanly formatted tutorial document; it exhibits specific structural irregularities that break generic parsers. To reconstruct the true document hierarchy, I implemented a stack-based parser (`parse_blocks`) operating under the following principles:

1. **Stack Traversal for Skipped Levels**:
   - The manual contains `2.1.1.1 Battery Life Under Typical Use` (Level 4) directly under `2.1 General Specifications` (Level 2), with no intermediate `2.1.1` section.
   - When a heading arrives, the parser calculates its depth from the dot count (`level = number.count('.') + 1`).
   - Rather than assuming `parent_level = level - 1`, the parser walks backward through the ancestor stack to find the nearest preceding section with `candidate.level < level`. In this case, it attaches `2.1.1.1` directly to `2.1 General Specifications`.
2. **Stack Maintenance for Out-of-Order Subsections**:
   - In Section 3, `3.4 Auto Shutoff` appears *before* `3.3 Result Display and Classification`.
   - Naive parsers that rely on monotonically increasing section numbers or naive previous-node parenting would misparent `3.3` as a child of `3.4`.
   - In our stack, when `3.4` arrives, `3.1` and `3.2` are popped, leaving `3. Device Operation` (Level 1) as parent. When `3.3` arrives, `3.4` is popped (since `3.4.level >= 3.3.level`), and `3.3` correctly attaches to `3. Device Operation` as a sibling.
3. **Disambiguation of Duplicate Heading Names**:
   - The document contains two distinct sections named `Error Codes`: `4.2 Error Codes` (under `4. Alarms and Safety Behavior`) and `7.1 Error Codes` (under `7. Troubleshooting`).
   - Headings are never used as primary keys. Nodes receive unique synthetic IDs, and logical identity is constructed using hierarchical paths (`parent_logical / normalized_heading`) with occurrence disambiguation.
4. **Preservation of Numbered Clinical Lists**:
   - Section 3.3 contains a 5-item clinical classification list (`1. Normal: ...`, `2. Elevated: ...`, `5. Hypertensive Crisis: ...`).
   - The parser uses a regex guard (`NUMBERED_ITEM` / specific clinical keywords) to ensure these are preserved as `[LIST ITEM]` body elements rather than erroneously spawned as top-level section headings.
5. **Cross-Page Paragraph Continuation**:
   - Section 3.1 begins at the bottom of Page 2 ("Press and hold...") and concludes on Page 3 ("Use the profile button...").
   - Similarly, in v2, Section 8.1 spans across the Page 6 / Page 7 boundary.
   - The parser tracks active nodes across page boundaries, appending continued body text and updating `page_end`.
6. **Unicode Ligature & Character Normalization**:
   - The source PDF contains typographical ligatures (`ﬁ` in "Specifications", `ﬀ` in "Cuff", `ﬂ` in "Inflation") and non-breaking hyphens (`\u2011`).
   - All extracted text is normalized via `unicodedata.normalize('NFKD', text)` to eliminate presentation forms that break full-text search, regex matching, and console rendering on Windows cp1252.

---

## 4. Debugging Methodology: Failures Identified & Fixed

| Failure Observed | How It Was Identified | Root Cause | Engineering Fix Applied |
| :--- | :--- | :--- | :--- |
| **Fragmented Table Rows** | Manual inspection of Page 4 Error Codes vs PDF screenshot. | Plain-text line extraction split multi-line cells ("leak detected", "E2, prompts retry") into disconnected text lines. | Switched to PyMuPDF `find_tables()`. Formatted tables as Markdown grids and filtered out text blocks within table bounding boxes. |
| **Windows Console Encoding Crash** | Python test script crashed with `UnicodeEncodeError: 'charmap' codec can't encode character '\ufb01'`. | PyMuPDF extracted raw unicode ligatures (`\ufb01`, `\ufb00`, `\ufb02`) which failed on Windows cp1252 shell. | Added `normalize_text()` using Unicode NFKD normalization, converting ligatures to ASCII (`fi`, `ff`, `fl`) and replacing `\u2011` with standard `-`. |
| **False Heading Detections in Lists** | Validation script inspecting parsed section count showed 33 sections instead of 28. | Section regex matched `1. Normal: systolic < 120...` as a top-level Section 1. | Added regex filter in `parse_blocks` checking for clinical prefixes, categorizing them as `[LIST ITEM]` under Section 3.3. |
| **Stateful Test Pollution in Memory DB** | Integration test failed with `AssertionError: assert 500 == 200`. | SQLite `:memory:` creates a new isolated database per connection unless `StaticPool` is configured. | Configured `StaticPool` on the test engine to allow multiple requests in the same test transaction to share state. |

---

## 5. Document Versioning & Matching Strategy

### Strategy Chosen: Hierarchical Path + Normalized Heading + Occurrence Disambiguation
Each document version is stored independently in SQLite (`DocumentVersion` + `Node`). To recognize whether a node in v2 represents the same logical entity as in v1:
1. When a node is ingested, its logical key is calculated as:
   `logical_id = sha256(f"{parent_logical_id}/{normalized_heading}#{occurrence_index}")[:24]`
2. On re-ingestion:
   - If an existing node in the previous version has the same `logical_id`, the node inherits that logical ID.
   - Content hash (`sha256(node.full_text)`) is compared:
     - Equal hashes $\rightarrow$ `match_status = "unchanged"`.
     - Differing hashes $\rightarrow$ `match_status = "modified"`.
   - If no matching logical ID exists in previous versions $\rightarrow$ `match_status = "added"`.
   - If a previous logical ID is absent in the new version $\rightarrow$ reported as `removed` by the comparison endpoint.

### Known Failure Modes & Where It Breaks:
1. **Renaming a Section**: If `2.1 General Specifications` is renamed to `2.1 Technical Specifications`, the path changes. The system will interpret this as removing the old section and adding a new one, breaking historical link continuity.
2. **Reparenting a Subsection**: Moving a subsection under a different parent section alters its `parent_logical_id`, appearing as a deletion and recreation.
3. **Reordering Sibling Headings with Identical Names**: Disambiguation relies on document appearance order (`#occurrence`). If two identically named sections swap order in the document, their logical IDs will swap.
4. **Formatting-Only Changes**: A change in whitespace or punctuation alters the SHA-256 hash, marking the node as modified even if the engineering requirement is semantically identical.

*Production Remedy*: A production system should combine path-based matching with semantic embeddings (e.g. cosine similarity over heading and body text) and provide an explicit human review queue for ambiguous transitions.

---

## 6. Version-Pinned Selections & Generation Storage

- **Selections**: Stored in relational tables (`Selection`, `SelectionItem`). When a user creates a selection, the system validates that all selected nodes belong to the same document version. It snapshots `heading_snapshot`, `text_snapshot`, `content_hash`, `logical_id`, and `version_id`. Even when v2 or v3 is ingested, the selection snapshot remains 100% immutable.
- **Generation Store**: Implemented as a file-based NoSQL JSON document store (`generated/generations.json`). This satisfies the requirement for a flexible document store for nested LLM output without imposing external database installation requirements on evaluators.

---

## 7. LLM QA Generation & Structured Output Defense

### Prompt Design
The prompt (`app/prompts/qa_generation.txt`) instructs the model to act as a medical device QA engineer. Crucially:
- It demands 3 to 5 executable test cases with title, requirement, preconditions, concrete steps, verifiable expected result, risk rating (`low`, `medium`, `high`), and exact source node traceability.
- It instructs the model: *"Treat the supplied source as the only authority. Never invent requirements or clinical guidance. If a requirement is ambiguous or contradictory, explicitly flag it rather than guessing."*

### Defensive Validation & Retry Architecture
1. **Gemini JSON Mode**: Uses `responseMimeType: "application/json"` with temperature 0.1 for high determinism.
2. **Strict Pydantic Validation**: Every generated test case is parsed through `GeneratedTestCase`. If any field is missing, steps are empty, risk is not an allowed enum, or fewer than 3 test cases are returned, validation fails.
3. **Bounded Retry**: The client performs 1 immediate retry on validation or provider failure.
4. **Deterministic Local Fallback**: If the API key is not configured or if provider calls exhaust retries, the system falls back to a deterministic rule-based generator that synthesizes boundary, functional, and repeatability test cases from the snapshot text. The provider field is explicitly labeled (`local_fallback` or `local_fallback_after_provider_error`) to maintain engineering integrity.
5. **Idempotency Policy**: A SHA-256 fingerprint is computed over the sorted source content hashes. Submitting the exact same selection returns the cached generation, preventing duplicate LLM API costs. Passing `force=true` overrides the cache to generate fresh test cases.

---

## 8. Staleness & Impact Detection

### How It Works at Retrieval Time
When a user calls `GET /generations?selection_id=...` or `GET /generations?node_id=...`:
1. The service looks up each `source_ref` stored with the generation.
2. It queries SQLite for the latest `DocumentVersion` of the parent document.
3. If the generation was created against the latest version $\rightarrow$ `status: "current"`, `is_stale: false`.
4. If a newer document version exists, it finds the peer node in that latest version sharing the same `logical_id`:
   - Peer missing $\rightarrow$ `status: "source_removed"`, `is_stale: true`.
   - Peer hash == snapshot hash $\rightarrow$ `status: "current"`, `is_stale: false`.
   - Peer hash != snapshot hash $\rightarrow$ `status: "stale"`, `is_stale: true`, returning `old_hash`, `current_hash`, and a diff note.

### Honest Discussion of Limitations:
- **Punctuation vs. Safety Thresholds**: A typo fix (e.g. adding a comma) and a safety threshold change (e.g. changing battery endurance from 300 to 250 cycles or emergency deflate timing from 2.0s to 1.5s) produce the exact same `stale` signal.
- **Should they be treated the same?**: From a strict medical device regulatory perspective (FDA 21 CFR Part 820 / ISO 13485), any change to requirement text requires formal review to prove it had no clinical impact. However, an ideal impact detector would categorize diffs into *cosmetic*, *linguistic*, *numeric/threshold*, and *clinical/safety-critical* to prioritize engineer review.

---

## 9. Required Decision Log

### 1. What's the one part of this system most likely to silently give wrong results without erroring? How would you catch it?
**The hierarchy parser's section heading regex matcher.** If the author of a medical manual writes a bold numbered paragraph like `"3.3 Device shall not exceed 40 deg C under fault conditions"` inside a troubleshooting section, or if a table row happens to start with a section-like number, the regex will treat it as a new section heading, silently splitting the body text and mis-parenting subsequent paragraphs without raising any Python exception.
**How I would catch it:** I would add a heuristic validation pass that checks for anomalous heading lengths, font size / weight metadata extracted from PyMuPDF, and sequence monotonicity (flagging large jumps in section numbering like 2.1 followed by 7.1 without intervening parents) and surface them in a `/documents/{id}/anomalies` audit endpoint.

### 2. Where did you choose simplicity over correctness because of time, and what would break first if this went to production as-is?
**Version matching based on heading path and normalized titles.** I chose this because it is deterministic and requires no external ML inference during ingestion.
**What would break first in production:** Section renaming and document reorganization. In real regulatory updates, technical writers frequently rephrase headings (e.g., renaming `2.2 Cuff Specifications` to `2.2 Upper-Arm Cuff Requirements`) or move safety controls from `Alarms and Safety` into `Device Operation`. The current system would treat the renamed section as an `added` node and a `removed` node, severing all historical traceability links from previous test cases.

### 3. Name one input (to your parser, your versioning matcher, or your LLM call) that you did not handle, and what your system does when it sees it.
**A pure scanned PDF containing rotated or multi-column text where Tesseract OCR yields disorganized reading order.** If a user submits a scanned page with multiple columns, PyMuPDF's fallback pixmap extraction feeds the entire image into Tesseract without bounding-box column de-warping. Tesseract reads across columns horizontally, merging sentences from column A into column B. The parser produces garbled body text, but does not error, resulting in corrupted requirements and nonsensical generated test cases.

---

## 10. What I Would Improve With More Time

1. **Geometry-Aware Semantic Chunking**: Extract bounding boxes for all typography styles (font weight, size) to distinguish headings from bold body paragraphs definitively.
2. **Semantic Matching with Embedding Fallback**: If path-based matching fails during re-ingestion, compute vector embeddings (e.g., Gemini `text-embedding-004`) to suggest match candidates to the engineer for review.
3. **Regulatory Diff Classification Engine**: Implement an AST or rule-based diff classifier that categorizes text changes into:
   - *Formatting/Typo* (zero QA impact)
   - *Threshold Delta* (numeric change: e.g. 300 $\rightarrow$ 250 cycles)
   - *Conditional Logic* (negation added: "do not", "shall not")
   - *Safety Critical* (error code, alarm threshold, contraindication change)
4. **Interactive Human-in-the-Loop Review Queue**: Expose an endpoint allowing QA leads to accept, reject, or adjust stale flags on legacy test cases.

