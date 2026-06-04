import os
import time
from pathlib import Path

import fitz
import pandas as pd
import streamlit as st
from anthropic import AuthenticationError
from dotenv import load_dotenv

from auth import get_api_key, login_wall, show_sidebar
from pdf_processor import detect_scanned, extract_blocks, reconstruct_pdf
from translator import BATCH_SIZE, translate_blocks, translate_blocks_free

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

st.set_page_config(page_title="PA Verstaler", page_icon="📖", layout="centered")

# ── Auth0 login + sidebar ──────────────────────────────────────────────────
login_wall()
show_sidebar()

st.title("PA Verstaler 📖")
st.caption("Vertaalt theologische PDF's van oud Bijbels Engels naar hedendaags Nederlands")

# ── API key ────────────────────────────────────────────────────────────────
api_key = get_api_key()

# ── File upload ────────────────────────────────────────────────────────────
uploaded = st.file_uploader("Kies een PDF om te vertalen", type=["pdf"])

if not uploaded:
    st.session_state.pop("last_file", None)
    st.stop()

file_id = f"{uploaded.name}_{uploaded.size}"

if st.session_state.get("last_file") != file_id:
    for k in ("last_file", "translation_done", "output_pdf_bytes",
              "blocks", "pdf_bytes", "translations"):
        st.session_state.pop(k, None)
    st.session_state.last_file = file_id

# ── PDF parsen (éénmalig, gecacht) ─────────────────────────────────────────
if st.session_state.get("blocks") is None:
    raw_bytes = uploaded.read()

    try:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as e:
        st.error(f"Kan PDF niet openen: {e}")
        st.stop()

    if doc.needs_pass:
        st.error("Deze PDF is beveiligd met een wachtwoord.")
        doc.close()
        st.stop()

    if detect_scanned(doc):
        st.error("Gescande PDF — alleen tekst-PDFs worden ondersteund.")
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
        "**Gratis**: Google Vertalen — gratis, geen API-sleutel nodig.\n\n"
        "**Claude AI**: Beste kwaliteit — kent thee/thou/hath, bewaart theologische "
        "vaktermen en Statenvertaling-stijl. Vereist een API-sleutel (sidebar)."
    ),
)
gratis = methode.startswith("🆓")

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
        st.warning("⚠️ Voer je API-sleutel in via de **sidebar** om Claude AI te gebruiken.")

st.divider()

# ── Vertaling klaar: download + nakijken ───────────────────────────────────
if st.session_state.get("translation_done") and st.session_state.get("output_pdf_bytes"):
    elapsed = st.session_state.get("elapsed", 0)
    m, s = int(elapsed // 60), int(elapsed % 60)
    st.success(f"✅ Vertaling voltooid in {m}m {s}s")

    out_name = uploaded.name.replace(".pdf", "_NL.pdf")
    st.download_button(
        label="⬇ Download vertaald PDF",
        data=st.session_state.output_pdf_bytes,
        file_name=out_name,
        mime="application/pdf",
        type="primary",
    )

    # ── Taalcheck / correcties ─────────────────────────────────────────────
    st.divider()
    with st.expander("✏️ Taalcheck — vertaling nakijken en corrigeren", expanded=False):
        _show_review(uploaded.name)

    if st.button("↩ Vertaal een ander bestand"):
        for k in ("last_file", "translation_done", "output_pdf_bytes",
                  "blocks", "pdf_bytes", "translations"):
            st.session_state.pop(k, None)
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
                text=f"Batch {done} van {total} — nog ~{int(remaining)}s",
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

        # Sla vertalingen op in sessie (voor taalcheck / correcties)
        st.session_state.translations = translations

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


# ── Taalcheck functie ──────────────────────────────────────────────────────

def _show_review(filename: str) -> None:
    """Toon de taalcheck-editor: zoek/vervang + blok-voor-blok nakijken."""
    blocks = st.session_state.get("blocks", [])
    translations: dict = dict(st.session_state.get("translations", {}))

    if not translations:
        st.info("Geen vertalingen beschikbaar.")
        return

    tab_replace, tab_edit = st.tabs(["🔄 Zoeken & vervangen", "📝 Blok nakijken"])

    # ── Tab 1: Globale zoek/vervang ────────────────────────────────────────
    with tab_replace:
        st.caption(
            "Vervang een woord of zin **overal** in het document — "
            "handig voor consistent theologisch taalgebruik."
        )
        col_a, col_b = st.columns(2)
        with col_a:
            find_txt = st.text_input(
                "Zoeken", placeholder="bijv. heiligmaking", key="rv_find"
            )
        with col_b:
            repl_txt = st.text_input(
                "Vervangen door", placeholder="bijv. heiliging", key="rv_repl"
            )

        # Toon hoeveel keer het woord voorkomt
        if find_txt:
            hits = sum(1 for v in translations.values() if find_txt in v)
            st.caption(f"💬 Komt voor in **{hits}** blokken")

        if st.button("Vervang alles en hergeneer PDF", type="primary",
                     disabled=not find_txt, key="rv_go"):
            count = 0
            for bid in translations:
                if find_txt in translations[bid]:
                    translations[bid] = translations[bid].replace(find_txt, repl_txt)
                    count += 1
            st.session_state.translations = translations
            _regenerate_pdf(blocks, translations)
            st.success(f"✅ {count} blokken bijgewerkt — PDF is hergenereerd")
            st.rerun()

    # ── Tab 2: Blok-voor-blok editor ──────────────────────────────────────
    with tab_edit:
        st.caption(
            "Bekijk de vertaling blok voor blok. "
            "Klik een cel in de **Nederlands**-kolom om te bewerken."
        )

        zoek = st.text_input(
            "🔍 Filter op tekst",
            placeholder="zoek in Engels of Nederlands…",
            key="rv_filter",
        )

        translatable = [
            b for b in blocks
            if b.block_type not in ("header", "footer")
            and translations.get(b.block_id, b.text) != b.text
        ]

        if zoek:
            lz = zoek.lower()
            translatable = [
                b for b in translatable
                if lz in b.text.lower()
                or lz in translations.get(b.block_id, "").lower()
            ]

        if not translatable:
            st.info("Geen vertaalde blokken gevonden.")
            return

        page_size = 30
        n_pgs = max(1, (len(translatable) + page_size - 1) // page_size)

        col_pg, col_tot = st.columns([3, 1])
        with col_pg:
            cur_pg = st.number_input(
                f"Pagina (1 – {n_pgs})", 1, n_pgs, 1, key="rv_pg"
            ) - 1
        with col_tot:
            st.metric("Blokken", len(translatable))

        chunk = translatable[cur_pg * page_size : (cur_pg + 1) * page_size]

        rows = [
            {
                "_id": b.block_id,
                "Pag.": b.page_num + 1,
                "Engels": b.text[:130] + ("…" if len(b.text) > 130 else ""),
                "Nederlands": translations.get(b.block_id, b.text),
            }
            for b in chunk
        ]
        df = pd.DataFrame(rows)

        edited = st.data_editor(
            df,
            column_config={
                "_id": None,
                "Pag.": st.column_config.NumberColumn(width="small", disabled=True),
                "Engels": st.column_config.TextColumn(
                    "Engels (origineel)", width="medium", disabled=True
                ),
                "Nederlands": st.column_config.TextColumn(
                    "Nederlands (klik om te bewerken)", width="large"
                ),
            },
            use_container_width=True,
            height=480,
            key=f"rv_tbl_{cur_pg}_{zoek}",
        )

        if st.button(
            "💾 Wijzigingen opslaan en PDF hergeneren",
            type="primary",
            key="rv_save",
        ):
            for _, row in edited.iterrows():
                translations[row["_id"]] = row["Nederlands"]
            st.session_state.translations = translations
            _regenerate_pdf(blocks, translations)
            st.success("✅ PDF bijgewerkt — download opnieuw hierboven")
            st.rerun()


def _regenerate_pdf(blocks: list, translations: dict) -> None:
    """Hergeneer de PDF met (gecorrigeerde) vertalingen."""
    pdf_bytes = st.session_state.pdf_bytes
    with st.spinner("PDF hergeneren…"):
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            new_bytes = reconstruct_pdf(doc, blocks, translations)
        finally:
            doc.close()
    st.session_state.output_pdf_bytes = new_bytes
