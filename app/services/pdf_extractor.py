from dataclasses import dataclass, asdict
import hashlib
import io
import re
import unicodedata
import pymupdf

@dataclass
class Block:
    text: str
    page: int
    kind: str = "text"
    bbox: tuple | None = None

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def normalize_text(text: str) -> str:
    """Normalize unicode ligatures (fi, ff, fl) and non-breaking hyphens to standard characters."""
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", " - ")

def extract_pdf(path: str) -> tuple[list[Block], str]:
    """Extract text and tables page-by-page.
    Uses layout-aware extraction with PyMuPDF to extract text blocks and table geometry,
    normalizing ligatures and formatting tables into Markdown.
    Pages with insufficient text trigger an OCR fallback via Tesseract.
    """
    doc = pymupdf.open(path)
    blocks: list[Block] = []
    all_text: list[str] = []

    for page_no, page in enumerate(doc, start=1):
        raw_text = page.get_text("text", sort=True).strip()

        # OCR fallback for scanned or low-text pages
        if len(raw_text) < 40:
            try:
                import pytesseract
                from PIL import Image
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_text = pytesseract.image_to_string(image).strip()
                ocr_text = normalize_text(ocr_text)
                if ocr_text:
                    all_text.append(ocr_text)
                    for line in ocr_text.splitlines():
                        line = re.sub(r"\s+", " ", line).strip()
                        if line:
                            blocks.append(Block(line, page_no, kind="text"))
                continue
            except Exception:
                pass

        # Layout-aware extraction: detect tables and their bounding boxes
        tables = page.find_tables().tables
        tab_bboxes = [t.bbox for t in tables]

        page_items: list[tuple[float, str, str, tuple | None]] = []

        # 1. Extract non-table text blocks
        raw_blocks = page.get_text("blocks")
        for b in raw_blocks:
            x0, y0, x1, y1, text, block_no, block_type = b
            if block_type != 0:
                continue  # skip raster images or non-text blocks
            # Exclude blocks that fall inside any detected table's bounding box
            in_table = any(y0 >= t[1] - 2 and y1 <= t[3] + 2 for t in tab_bboxes)
            if not in_table:
                clean_text = normalize_text(text).strip()
                if clean_text:
                    page_items.append((y0, "text", clean_text, (x0, y0, x1, y1)))

        # 2. Extract tables as structured Markdown blocks
        for t in tables:
            md = normalize_text(t.to_markdown()).strip()
            page_items.append((t.bbox[1], "table", md, t.bbox))

        # Sort all items on the page by vertical reading position
        page_items.sort(key=lambda item: item[0])

        for _, kind, content, bbox in page_items:
            all_text.append(content)
            if kind == "table":
                blocks.append(Block(content, page_no, kind="table", bbox=bbox))
            else:
                for line in content.splitlines():
                    line = re.sub(r"\s+", " ", line).strip()
                    if line:
                        blocks.append(Block(line, page_no, kind="text", bbox=bbox))

    return blocks, "\n".join(all_text)

