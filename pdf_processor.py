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


def _calc_para_scale(blocks: list[TextBlock], translations: dict) -> float:
    """Bereken een globale schaalfactor voor alinea-tekst.

    Nederlands is gemiddeld 15–20% langer dan Engels. Door alle alinea's
    uniform voor te schalen krijg je consistente lettergroottes in het
    hele document — geen mix van 12pt en 8pt meer.

    Retourneert een factor tussen 0.87 en 1.0.
    """
    ratios = []
    for block in blocks:
        if block.block_type not in ("paragraph", "footnote"):
            continue
        orig = block.text.strip()
        trans = translations.get(block.block_id, "").strip()
        if len(orig) > 40 and trans and trans != orig:
            ratios.append(len(trans) / len(orig))
    if not ratios:
        return 1.0
    avg = sum(ratios) / len(ratios)
    # Omgekeerd evenredig: 18% langer → start op 1/1.18 ≈ 0.847 → min 0.87
    return max(round(1.0 / avg, 3), 0.87)


def reconstruct_pdf(
    original_doc: fitz.Document,
    blocks: list[TextBlock],
    translations: dict,
) -> bytes:
    """Rebuild the PDF with translated text.

    Drie-staps aanpak per pagina:
    1. Redacteer originele Engelse tekst (permanent verwijderd, geen wit vakje)
    2. Bereken globale schaalfactor voor consistente lettergroottes
    3. Voeg Nederlandse tekst in op exact dezelfde positie

    Resultaat: drukklaar boek — zelfde opmaak, zelfde layout, zelfde afbeeldingen.
    """
    new_doc = fitz.open()
    new_doc.insert_pdf(original_doc)

    # ── Globale schaal voor consistente lettergroottes ─────────────────────
    # Voorkomt dat block A 12pt heeft en block B 8pt door overflow-reductie.
    para_scale = _calc_para_scale(blocks, translations)

    for page_num in range(len(new_doc)):
        page = new_doc[page_num]
        page_blocks = [b for b in blocks if b.page_num == page_num]

        # Alleen blokken die echt veranderd zijn
        to_replace = []
        for block in page_blocks:
            translated = translations.get(block.block_id, block.text)
            if translated and translated != block.text:
                to_replace.append((block, translated))

        if not to_replace:
            continue

        # ── Pass 1: markeer gebieden voor redactie ────────────────────────
        for block, _ in to_replace:
            page.add_redact_annot(fitz.Rect(block.bbox), fill=(1, 1, 1))

        # ── Pass 2: verwijder originele tekst permanent ───────────────────
        page.apply_redactions(images=_REDACT_LEAVE, graphics=_REDACT_LEAVE)

        # ── Pass 3: voeg vertaling in met consistente lettergrootte ───────
        for block, translated in to_replace:
            bbox = fitz.Rect(block.bbox)
            fontname = _map_font(block.font, block.flags)

            # Koppen: originele grootte bewaren (titels mogen opvallen)
            # Alinea's en voetnoten: uniform voorgescaald voor consistentie
            if block.block_type in ("paragraph", "footnote"):
                fontsize = round(block.fontsize * para_scale, 1)
                min_size = max(block.fontsize * 0.82, 8.0)
            else:
                fontsize = block.fontsize
                min_size = max(block.fontsize * 0.85, 9.0)

            # Fijn-granulaire overflow-reductie (0.5pt stappen)
            while fontsize >= min_size:
                result = page.insert_textbox(
                    bbox,
                    translated,
                    fontname=fontname,
                    fontsize=fontsize,
                    color=(0, 0, 0),
                    align=block.alignment,
                    overlay=True,
                )
                if result >= 0:
                    break
                fontsize -= 0.5

    output = new_doc.tobytes(deflate=True)
    new_doc.close()
    return output
