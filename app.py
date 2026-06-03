import os
import time
from pathlib import Path

import fitz
import streamlit as st
from anthropic import AuthenticationError
from dotenv import load_dotenv

from auth import get_api_key, login_wall, show_sidebar
from pdf_processor import detect_scanned, extract_blocks, reconstruct_pdf
from translator import BATCH_SIZE, translate_blocks, translate_blocks_free

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

st.set_page_config(page_title="PA Verstaler", page_icon="📖", layout="centered")

# ── Auth0 login + sidebar ──────────────────────────────────────────────────
login_wall()       # stopt de app als niet ingelogd
show_sidebar()     # gebruikersinfo + API-sleutelinvoer

st.title("PA Verstaler 📖")
st.caption("Vertaalt theologische PDF's van oud Bijbels Engels naar hedendaags Nederlands")

# ── API key (sessie of .env) ───────────────────────────────────────────────
api_key = get_api_key()

# ── File upload ────────────────────────────────────────────────────────────
uploaded = st.file_uploader("Kies een PDF om te vertalen", type=["pdf"])

if not uploaded:
    st.session_state.pop("last_file", None)
    st.stop()

file_id = f"{uploaded.name}_{uploaded.size}"

# Reset state wanneer een nieuw bestand wordt geüpload
if st.session_state.get("last_file") != file_id:
    st.session_state.last_file = file_id
    st.session_state.translation_done = False
    st.session_state.output_pdf_bytes = None
    st.session_state.blocks = None
    st.session_state.pdf_bytes = None

# PDF éénmalig parsen, resultaat cachen in session state
if st.session_state.get("blocks") is None:
    raw_bytes = uploaded.read()

    try:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as e:
        st.error(f"Kan PDF niet openen: {e}")
        st.stop()

    if doc.needs_pass:
        st.error("Deze PDF is beveiligd met een wachtwoord. Verwijder het wachtwoord eerst.")
        doc.close()
        st.stop()

    if detect_scanned(doc):
        st.error(
            "Deze PDF lijkt uit gescande afbeeldingen te bestaan. "
            "Alleen tekst-PDFs (digitaal aangemaakt) worden ondersteund."
        )
        doc.close()
        st.stop()

    with st.spinner("PDF analyseren..."):
        blocks = extract_blocks(doc)

    n_pages = len(doc)
    doc.close()

    translatable = [b for b in blocks if b.block_type not in ("header", "footer")]

    st.session_state.blocks = blocks
    st.session_state.pdf_bytes = raw_bytes
    st.session_state.n_pages = n_pages
    st.session_state.n_blocks = len(translatable)

# ── Document info ──────────────────────────────────────────────────────────
n_pages = st.session_state.n_pages
n_blocks = st.session_state.n_blocks
n_batches = max(1, (n_blocks + BATCH_SIZE - 1) // BATCH_SIZE)

col1, col2 = st.columns(2)
col1.metric("Pagina's", n_pages)
col2.metric("Tekstblokken", n_blocks)

st.divider()

# ── Vertaalmethode ─────────────────────────────────────────────────────────
default_idx = 1 if api_key else 0
methode = st.radio(
    "Vertaalmethode",
    options=["🆓 Gratis (Google Vertalen)", "⭐ Claude AI (betere kwaliteit)"],
    index=default_idx,
    help=(
        "**Gratis**: Google Vertalen — gratis, geen API-sleutel nodig. "
        "Begrijpt ook archaïsch Engels, maar minder nauwkeurig voor theologische terminologie.\n\n"
        "**Claude AI**: Beste kwaliteit — kent thee/thou/hath, bewaart theologische vaktermen "
        "(genade, heiligmaking, verbond…) en past de Statenvertaling-stijl toe. "
        "Vereist een Anthropic API-sleutel (invoeren via de sidebar)."
    ),
)
gratis = methode.startswith("🆓")

# Geschatte tijd
if gratis:
    est_sec = max(30, n_blocks // 2)
    est_str = f"~{est_sec // 60}m {est_sec % 60}s" if est_sec >= 60 else f"~{est_sec}s"
    st.caption(f"⏱ Geschatte tijd: {est_str}")
else:
    est_min = (n_batches * 8) // 60
    est_max = (n_batches * 15) // 60
    est_str = f"{est_min}–{est_max} min" if est_max > 0 else "< 1 min"
    st.caption(f"⏱ Geschatte tijd: {est_str}")
    if not api_key:
        st.warning(
            "⚠️ Voer je Anthropic API-sleutel in via de **sidebar** (links) om Claude AI te gebruiken."
        )

st.divider()

# ── Vertaling klaar: download ──────────────────────────────────────────────
if st.session_state.get("translation_done") and st.session_state.get("output_pdf_bytes"):
    elapsed = st.session_state.get("elapsed", 0)
    m, s = int(elapsed // 60), int(elapsed % 60)
    st.success(f"Vertaling voltooid in {m}m {s}s")

    out_name = uploaded.name.replace(".pdf", "_NL.pdf")
    st.download_button(
        label="⬇ Download vertaald PDF",
        data=st.session_state.output_pdf_bytes,
        file_name=out_name,
        mime="application/pdf",
        type="primary",
    )

    if st.button("↩ Vertaal een ander bestand"):
        for key in ("last_file", "translation_done", "output_pdf_bytes", "blocks", "pdf_bytes"):
            st.session_state.pop(key, None)
        st.rerun()

# ── Start vertaling ────────────────────────────────────────────────────────
else:
    start_disabled = (not gratis) and (not api_key)
    if st.button("▶ Start vertaling", type="primary", disabled=start_disabled):
        progress_bar = st.progress(0.0, text="Vertaling starten...")
        status_text = st.empty()

        blocks = st.session_state.blocks
        pdf_bytes = st.session_state.pdf_bytes
        start = time.time()

        def on_batch(done: int, total: int) -> None:
            elapsed = time.time() - start
            remaining = (elapsed / done) * (total - done) if done > 0 else 0
            progress_bar.progress(
                done / total,
                text=f"Batch {done} van {total} vertaald — nog ~{int(remaining)}s",
            )
            status_text.caption(
                f"Verwerkt: {min(done * BATCH_SIZE, n_blocks)} / {n_blocks} blokken"
            )

        try:
            if gratis:
                translations = translate_blocks_free(blocks, on_batch=on_batch)
            else:
                translations = translate_blocks(blocks, api_key, on_batch=on_batch)
        except AuthenticationError:
            st.error("API-sleutel is ongeldig. Controleer de sleutel in de sidebar.")
            st.stop()
        except Exception as e:
            st.error(f"Vertaling mislukt: {e}")
            st.stop()

        status_text.empty()
        progress_bar.progress(1.0, text="PDF opbouwen...")

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            output_bytes = reconstruct_pdf(doc, blocks, translations)
        finally:
            doc.close()

        st.session_state.output_pdf_bytes = output_bytes
        st.session_state.translation_done = True
        st.session_state.elapsed = time.time() - start
        st.rerun()
