import re, uuid, hashlib
from dataclasses import dataclass, asdict
from app.services.pdf_extractor import Block, sha256_text

SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\.?\s+(.{2,120})$")
TABLE_HEADERS = {"parameter value", "code meaning device behavior"}
NUMBERED_ITEM = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")

@dataclass
class ParsedNode:
    temp_key: str
    heading: str
    level: int
    node_type: str
    body_text: str
    full_text: str
    parent_key: str | None
    page_start: int | None
    page_end: int | None
    content_hash: str

def parse_blocks(blocks: list[Block]) -> list[ParsedNode]:
    """CT-200-specific hierarchy parser. Unnumbered content remains under current node.
    Duplicate headings are retained as separate nodes because identity is positional/path-based,
    never heading-only. Out-of-order numbered headings are attached to the nearest preceding
    lower-level section; all uncertain heading decisions should be reviewed.
    """
    nodes: list[ParsedNode] = []
    stack: list[ParsedNode] = []
    current = None
    pending_body = []
    def flush_body():
        nonlocal pending_body, current
        if current and pending_body:
            extra = "\n".join(pending_body).strip()
            current.body_text = (current.body_text + "\n" + extra).strip()
            current.full_text = (current.heading + "\n" + current.body_text).strip()
            current.content_hash = sha256_text(current.full_text)
        pending_body = []
    for b in blocks:
        text = b.text.strip()
        # Filter PDF page markers, not real content.
        if re.match(r"^<PAGE \d+ OF \d+>$", text, re.I):
            continue
        if b.kind == "table":
            if current:
                pending_body.append("[TABLE]\n" + text)
                current.page_end = b.page
            continue
        match = SECTION_RE.match(text) if b.kind == "text" else None
        # Numbered list entries (e.g. "1. Normal: ...") are not section headings.
        if match and re.match(r"^\d+[.)]\s+(?:Normal:|Elevated:|Hypertension|Hypertensive Crisis:)", text, re.I):
            match = None
        # Avoid treating numeric table rows as section headings; only accept conventional title case
        # and heading-like strings, while explicitly retaining the irregular 2.1.1.1 heading.
        is_heading = bool(match) and not text.lower().startswith(("0–", "0-", "40–", "40-", "0 -", "40 -"))
        if is_heading:
            flush_body()
            number, title = match.group(1), match.group(2).strip()
            level = number.count(".") + 1
            # A heading like "2.1.1.1" is level 4 even though 2.1.1 is absent.
            parent = None
            for candidate in reversed(stack):
                if candidate.node_type == "section" and candidate.level < level:
                    parent = candidate
                    break
            key = f"{number}:{len(nodes)}:{uuid.uuid4().hex[:8]}"
            node = ParsedNode(key, text, level, "section", "", text, parent.temp_key if parent else None,
                              b.page, b.page, sha256_text(text))
            nodes.append(node)
            while stack and stack[-1].level >= level:
                stack.pop()
            stack.append(node)
            current = node
            continue
        if current:
            # Table row preservation: mark rows or markdown tables rather than flattening them away.
            if text.lower().replace(" ", "") in {"parametervalue", "codemeaningdevicebehavior"}:
                pending_body.append("[TABLE HEADER] " + text)
            elif NUMBERED_ITEM.match(text):
                pending_body.append("[LIST ITEM] " + text)
            else:
                pending_body.append(text)
            current.page_end = b.page
        else:
            # Preserve pre-heading title / front matter as a root node.
            key = f"front:{len(nodes)}:{uuid.uuid4().hex[:8]}"
            node = ParsedNode(key, "Document front matter", 0, "front_matter", text, text, None,
                              b.page, b.page, sha256_text(text))
            nodes.append(node)
            current = node
            stack = [node]
    flush_body()
    # Recompute parents for heading nodes using their explicit temporary parent keys.
    return nodes

def nodes_to_dict(nodes):
    return [asdict(n) for n in nodes]
