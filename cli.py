#!/usr/bin/env python3
"""PA Verstaler CLI — gebruik: pa-verstaler boek.pdf [uitvoer.pdf]"""

import argparse
import os
import sys
import time
from pathlib import Path

import fitz
from dotenv import load_dotenv

from pdf_processor import detect_scanned, extract_blocks, reconstruct_pdf
from translator import BATCH_SIZE, translate_blocks


def main() -> None:
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

    parser = argparse.ArgumentParser(
        prog="pa-verstaler",
        description="Vertaalt een theologische PDF van oud Bijbels Engels naar hedendaags Nederlands.",
        epilog=(
            "Voorbeelden:\n"
            "  pa-verstaler boek.pdf\n"
            "  pa-verstaler boek.pdf vertaald.pdf\n"
            "  pa-verstaler C:/Boeken/puritan_grace.pdf\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("invoer", metavar="INVOER.pdf", help="Pad naar de invoer-PDF")
    parser.add_argument(
        "uitvoer",
        metavar="UITVOER.pdf",
        nargs="?",
        help="Pad naar de uitvoer-PDF (standaard: INVOER_NL.pdf naast het origineel)",
    )

    args = parser.parse_args()

    input_path = Path(args.invoer).resolve()
    if not input_path.exists():
        _err(f"Bestand niet gevonden: {input_path}")

    if input_path.suffix.lower() != ".pdf":
        _err(f"Bestand is geen PDF: {input_path.name}")

    output_path = (
        Path(args.uitvoer).resolve()
        if args.uitvoer
        else input_path.parent / (input_path.stem + "_NL.pdf")
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        _err(
            "ANTHROPIC_API_KEY niet gevonden.\n"
            "  Maak een .env bestand aan in dezelfde map als pa-verstaler:\n"
            "  ANTHROPIC_API_KEY=sk-ant-api03-..."
        )

    # ── Header ────────────────────────────────────────────────────────────
    print()
    print("  📖  PA Verstaler")
    print(f"      Invoer : {input_path.name}")
    print(f"      Uitvoer: {output_path}")
    print()

    # ── Open & valideer PDF ───────────────────────────────────────────────
    try:
        doc = fitz.open(str(input_path))
    except Exception as e:
        _err(f"Kan PDF niet openen: {e}")

    if doc.needs_pass:
        _err("PDF is beveiligd met een wachtwoord.")

    if detect_scanned(doc):
        _err("PDF lijkt gescand (afbeeldingen). Alleen tekst-PDFs worden ondersteund.")

    # ── Extraheer tekstblokken ────────────────────────────────────────────
    print("  🔍 Analyseren...", end="", flush=True)
    blocks = extract_blocks(doc)
    translatable = [b for b in blocks if b.block_type not in ("header", "footer")]
    n_batches = max(1, (len(translatable) + BATCH_SIZE - 1) // BATCH_SIZE)
    est = n_batches * 10  # seconden
    est_str = f"{est // 60}m {est % 60}s" if est >= 60 else f"{est}s"

    print(
        f"\r  🔍 {len(doc)} pagina's · {len(translatable)} tekstblokken"
        f" · geschat ~{est_str}"
    )
    print()

    # ── Vertaal ───────────────────────────────────────────────────────────
    start = time.time()

    def on_batch(done: int, total: int) -> None:
        elapsed = time.time() - start
        remaining = int((elapsed / done) * (total - done)) if done > 0 else 0
        filled = int(done / total * 28)
        bar = "█" * filled + "░" * (28 - filled)
        print(
            f"\r  🌍 [{bar}] {done}/{total}  ~{remaining}s resterend   ",
            end="",
            flush=True,
        )

    print(f"  🌍 Vertalen...")
    try:
        translations = translate_blocks(blocks, api_key, on_batch=on_batch)
    except Exception as e:
        print()
        _err(f"Vertaling mislukt: {e}")

    t_vert = time.time() - start
    print(f"\r  ✓  Vertaling klaar in {t_vert:.0f}s{' ' * 35}")
    print()

    # ── Bouw PDF opnieuw op ───────────────────────────────────────────────
    print("  📄 PDF opbouwen...", end="", flush=True)
    try:
        output_bytes = reconstruct_pdf(doc, blocks, translations)
    finally:
        doc.close()

    output_path.write_bytes(output_bytes)
    total = time.time() - start
    size_kb = len(output_bytes) // 1024

    print(f"\r  📄 PDF klaar · {size_kb} KB{' ' * 20}")
    print()
    print(f"  ✅ Klaar in {total / 60:.1f} minuten")
    print(f"     → {output_path}")
    print()


def _err(msg: str) -> None:
    print(f"\n  ✗  {msg}\n", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
