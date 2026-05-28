from collections import Counter
from dataclasses import dataclass

import fitz  # PyMuPDF

# PDF_REDACT constants (images=0 and graphics=0 = leave untouched)
_REDACT_LEAVE = 0


@dataclass
class TextBlock:
    page_num: int
    block_id: str
    text: str
    bbox: tuple
    font: str
    fontsize: float
    flags: int
    block_type: str   # "heading", "paragraph", "footnote", "header", "footer"
    alignment: int    # 0=left, 1=center, 2=right


def detect_scanned(doc: fitz.Document, sample_pages: int = 5) -> bool:
    total = min(sample_pages, len(doc))
    if total == 0:
        return True
    avg = sum(len(doc[i].get_text("text").strip()) for i in range(total)) / total
    return avg < 50


def _get_body_fontsize(doc: fitz.Document) -> float:
    sizes = []
    for page_num in range(min(3, len(doc))):
        page = doc[page_num]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        sizes.append(round(span["size"], 1))
    if not sizes:
        return 11.0
    return Counter(sizes).most_common(1)[0][0]


def _classify_block(y0: float, y1: float, fontsize: float, body_size: float, page_height: float) -> str:
    if y0 < page_height * 0.08:
        return "header"
    if y1 > page_height * 0.92:
        return "footer"
    if fontsize > body_size * 1.3:
        return "heading"
    if fontsize < body_size * 0.82 and y0 > page_height * 0.70:
        return "footnote"
    return "paragraph"


def _detect_alignment(bbox: tuple, page_width: float, block_type: str) -> int:
    """Detect text alignment from block position on the page.
    Returns 0=left, 1=center, 2=right.
    """
    x0, _, x1, _ = bbox
    block_width = x1 - x0
    block_center = (x0 + x1) / 2
    page_center = page_width / 2

    # Center-align headings that are narrow and visually centered on the page
    if block_type == "heading":
        if block_width < page_width * 0.75 and abs(block_center - page_center) < page_width * 0.12:
            return 1  # center

    # Right-align very narrow blocks near the right edge (e.g., dates, references)
    if block_width < page_width * 0.30 and x1 > page_width * 0.70:
        return 2  # right

    return 0  # left (default for body text)


def extract_blocks(doc: fitz.Document) -> list[TextBlock]:
    body_size = _get_body_fontsize(doc)
    blocks = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_height = page.rect.height
        page_width = page.rect.width

        for block_idx, block in enumerate(page.get_text("dict")["blocks"]):
            if block.get("type") != 0:
                continue

            text_parts = []
            first_span = None
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if t:
                        text_parts.append(t)
                        if first_span is None:
                            first_span = span

            text = " ".join(text_parts).strip()
            if not text or not first_span:
                continue

            bbox = tuple(block["bbox"])
            fontsize = first_span.get("size", body_size)
            font = first_span.get("font", "Times-Roman")
            flags = first_span.get("flags", 0)
            block_type = _classify_block(bbox[1], bbox[3], fontsize, body_size, page_height)
            alignment = _detect_alignment(bbox, page_width, block_type)
            block_id = f"p{page_num}_b{block_idx}"

            blocks.append(
                TextBlock(
                    page_num=page_num,
                    block_id=block_id,
                    text=text,
                    bbox=bbox,
                    font=font,
                    fontsize=fontsize,
                    flags=flags,
                    block_type=block_type,
                    alignment=alignment,
                )
            )

    return blocks


def _map_font(font_name: str, flags: int) -> str:
    name = font_name.lower()
    is_bold = bool(flags & (1 << 4))
    is_italic = bool(flags & 1)

    sans_keywords = ["helvetica", "arial", "sans", "gothic", "verdana", "calibri", "tahoma"]
    is_sans = any(k in name for k in sans_keywords)

    if is_sans:
        if is_bold:
            return "Helvetica-Bold"
        if is_italic:
            return "Helvetica-Oblique"
        return "Helvetica"
    else:
        if is_bold and is_italic:
            return "Times-BoldItalic"
        if is_bold:
            return "Times-Bold"
        if is_italic:
            return "Times-Italic"
        return "Times-Roman"


def reconstruct_pdf(
    original_doc: fitz.Document,
    blocks: list[TextBlock],
    translations: dict,
) -> bytes:
    """Rebuild the PDF with translated text.

    Uses PyMuPDF redaction (not just white rectangles) so the original
    English text is *truly removed* from the PDF — the result is a clean,
    printable Dutch document with no hidden content underneath.
    """
    new_doc = fitz.open()
    new_doc.insert_pdf(original_doc)

    for page_num in range(len(new_doc)):
        page = new_doc[page_num]
        page_blocks = [b for b in blocks if b.page_num == page_num]

        # Collect blocks that actually changed
        to_replace = []
        for block in page_blocks:
            translated = translations.get(block.block_id, block.text)
            if translated and translated != block.text:
                to_replace.append((block, translated))

        if not to_replace:
            continue

        # ── Pass 1: mark all areas for redaction ──────────────────────────
        for block, _ in to_replace:
            # fill=(1,1,1) = white background after redaction
            page.add_redact_annot(fitz.Rect(block.bbox), fill=(1, 1, 1))

        # ── Pass 2: apply redactions — original text is permanently removed ─
        # images=0 and graphics=0 → leave images and vector drawings untouched
        page.apply_redactions(images=_REDACT_LEAVE, graphics=_REDACT_LEAVE)

        # ── Pass 3: insert translated text in the now-clean white areas ────
        for block, translated in to_replace:
            bbox = fitz.Rect(block.bbox)
            fontname = _map_font(block.font, block.flags)
            fontsize = block.fontsize

            # Reduce font size if Dutch text is longer and overflows the bbox
            while fontsize >= 7:
                result = page.insert_textbox(
                    bbox,
                    translated,
                    fontname=fontname,
                    fontsize=fontsize,
                    color=(0, 0, 0),
                    align=block.alignment,   # preserves heading centering etc.
                    overlay=True,
                )
                if result >= 0:
                    break
                fontsize -= 1.5

    output = new_doc.tobytes(deflate=True)
    new_doc.close()
    return output
