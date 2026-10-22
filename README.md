# Tri9T AI Engineering Assignment — CT-200 Medical Device Manual Pipeline

A robust, production-grade backend service built with **FastAPI**, **SQLAlchemy + SQLite**, **Pydantic**, and **PyMuPDF** that parses technical medical device manuals into structured, versioned hierarchies, manages version-pinned requirements selections, generates verifiable QA test cases using LLMs, and flags stale traceability when source documents change across revisions.

> **Engineering Note:** This system is specifically designed to handle real-world irregularities present in the CardioTrack CT-200 Technical & User Manuals (v1 & v2)—including skipped subsection numbering, non-monotonic section ordering, duplicate heading names, layout-embedded tables, Unicode ligatures, cross-page text breaks, and source text contradictions.

---

## Key Features

1. **Layout-Aware PDF Extraction & OCR Fallback**:
   - Uses PyMuPDF to extract text blocks and geometric tables via `page.find_tables()`.
   - Converts tables into structured Markdown tables rather than fragmented lines.
   - Filters out raw text overlapping table bounding boxes to prevent duplicate or jumbled text.
   - Normalizes typographic ligatures (`ﬁ`, `ﬀ`, `ﬂ`) and non-breaking hyphens (`\u2011`) into clean standard text.
   - Triggers automated Tesseract OCR fallback at 2x resolution if page text yield is below 40 characters.

2. **Structural Hierarchy Reconstruction**:
   - Handles skipped subsection depth (e.g. `2.1.1.1` under `2.1` without intermediate `2.1.1`).
   - Resolves non-monotonic section orders (e.g. `3.4 Auto Shutoff` preceding `3.3 Result Display and Classification`) without misparenting.
   - Disambiguates duplicate headings (e.g. `4.2 Error Codes` vs `7.1 Error Codes`) with distinct parentage and stable logical IDs.
   - Preserves numbered clinical classification lists (Section 3.3) as list items without confusing them with section headings.
   - Joins multi-page paragraphs seamlessly across page boundaries.

3. **Document Versioning & Diff Detection**:
   - Maintains immutable document versions in SQLite (`Document`, `DocumentVersion`, `Node`).
   - Matches logical nodes across versions via hierarchical paths (`parent_logical_id / normalized_heading` + occurrence index).
   - Computes SHA-256 content hashes to detect unchanged, modified, and added nodes.
   - Surfaces node-level diff summaries (similarity ratio, length deltas, and excerpt diffs) via `/nodes/{node_id}/changes`.

4. **Version-Pinned Selections**:
   - Creates named selections (`Selection`, `SelectionItem`) that capture immutable snapshots of the exact node IDs, version numbers, text, and content hashes at the time of selection creation.
   - Guaranteed never to mutate even when newer document versions are ingested.

5. **LLM QA Test-Case Generation**:
   - Generates 3–5 concrete, executable QA test cases with risk ratings, preconditions, steps, expected results, and exact source node traceability.
   - Validates model output strictly against Pydantic schemas (`GeneratedTestCase`).
   - Implements bounded retries on malformed or non-compliant output.
   - Includes a deterministic, transparent local fallback when offline or unconfigured, explicitly labeling provider origin.
   - Enforces an idempotency policy based on selection content fingerprints, with a `force=true` bypass.

6. **Staleness & Impact Detection API**:
   - Evaluates whether previously generated test cases still reflect the current document state at retrieval time.
   - Reports status (`current`, `stale`, `source_removed`, `source_missing`), changed boolean, old hash, and current hash.

---

## Architecture & Tech Stack

- **API Framework**: FastAPI
- **Data Validation**: Pydantic v2
- **Relational Storage**: SQLite via SQLAlchemy 2.0 (documents, versions, nodes, selections)
- **Document Store**: File-based JSON store (`generated/generations.json`) for LLM outputs
- **PDF Extraction**: PyMuPDF (`pymupdf`) with layout table extraction
- **OCR Engine**: Tesseract OCR via `pytesseract` (fallback for scanned pages)
- **Test Runner**: Pytest (20 automated unit & integration tests)

---

## Getting Started

### 1. Prerequisites
Python 3.11+ is recommended.

```bash
# Clone the repository
git clone <repo-url>
cd tri9t_ct200_ai_engineering

# Create and activate virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS / Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Configuration
Copy `.env.example` to `.env` (optional):
```bash
# Windows:
copy .env.example .env
# macOS / Linux:
cp .env.example .env
```

If you have a Google Gemini API key:
```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
```
*Note: If no API key is provided, the system seamlessly uses a deterministic local fallback with complete traceability snapshots and clear provider labeling.*

### 3. Running the Server
```bash
uvicorn app.main:app --reload
```
Interactive Swagger API documentation is available at: **http://127.0.0.1:8000/docs**

---

## End-to-End Verification Demo

To see the complete workflow executed in seconds (ingestion -> browse -> selection -> generation -> re-ingestion -> diffs -> staleness detection -> idempotency), run:

```bash
python scripts/demo_version_flow.py
```
*This script automatically runs in standalone mode if the live server is not active, or connects to `http://127.0.0.1:8000` if the server is running.*

---

## Triggering the v1 → v2 Flow via curl

### 1. Ingest Manual v1
```bash
curl -X POST http://127.0.0.1:8000/documents/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "data/ct200_manual_v1.pdf", "document_name": "CT-200 Manual"}'
```

### 2. List Sections in v1
```bash
curl -X GET "http://127.0.0.1:8000/documents/sections?version=1"
```

### 3. Search for Battery Specification
```bash
curl -X GET "http://127.0.0.1:8000/nodes?q=battery&version=1"
```
*(Copy the `id` of node `2.1.1.1 Battery Life Under Typical Use`)*

### 4. Create a Version-Pinned Selection
```bash
curl -X POST http://127.0.0.1:8000/selections \
  -H "Content-Type: application/json" \
  -d '{"name": "Battery QA Suite", "node_ids": ["<NODE_ID>"]}'
```
*(Copy the returned `selection_id`)*

### 5. Generate QA Test Cases
```bash
curl -X POST "http://127.0.0.1:8000/selections/<SELECTION_ID>/generate"
```

### 6. Verify Initial Staleness (Status: `current`)
```bash
curl -X GET "http://127.0.0.1:8000/generations?selection_id=<SELECTION_ID>"
```

### 7. Re-Ingest Modified Manual v2
```bash
curl -X POST http://127.0.0.1:8000/documents/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "data/ct200_manual_v2.pdf", "document_name": "CT-200 Manual"}'
```

### 8. View Node Diff Summary
```bash
curl -X GET "http://127.0.0.1:8000/nodes/<NODE_ID>/changes"
```

### 9. Verify Stale Traceability Flag (Status: `stale`)
```bash
curl -X GET "http://127.0.0.1:8000/generations?selection_id=<SELECTION_ID>"
```
*The response will now show `"is_stale": true`, `"status": "stale"`, along with both the old content hash and the current content hash.*

---

## Running the Automated Test Suite

The test suite covers parser edge cases, versioning logic, Pydantic schema validation, selection immutability, staleness detection, and end-to-end API endpoints:

```bash
python -m pytest -v
```
*(All 20 tests pass with zero warnings)*

---

## API Summary

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/health` | `GET` | Health check endpoint |
| `/documents/ingest` | `POST` | Ingest and parse PDF manual (idempotent by file SHA-256) |
| `/documents/sections` | `GET` | Retrieve top-level sections (accepts `version` query param) |
| `/nodes/{node_id}` | `GET` | Retrieve node with children, body text, and content hash |
| `/nodes` | `GET` | Search headings and body text (`q`, `version`, `document_name`) |
| `/nodes/{node_id}/changes` | `GET` | Compare node across versions; returns diff summary |
| `/selections` | `POST` | Create version-pinned selection with immutable text snapshots |
| `/selections/{id}` | `GET` | Retrieve selection snapshot |
| `/selections/{id}/generate` | `POST` | Generate QA test cases (`force=true` bypasses idempotency) |
| `/generations` | `GET` | Retrieve generations with real-time staleness impact evaluation |

---

## Approach Document & Decision Log

For detailed technical justifications, architectural tradeoffs, failure modes, and answers to the mandatory Decision Log questions, see [`docs/approach.md`](docs/approach.md).

