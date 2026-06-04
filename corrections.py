"""Persistente terminologie-correcties voor PA Verstaler.

Correcties worden opgeslagen in corrections.json naast de app.
Ze worden automatisch toegepast na elke vertaling.
Op Render (read-only filesystem) is opslaan stil gefaald — gebruik dan export/import.
"""
import json
from pathlib import Path

_FILE = Path(__file__).resolve().parent / "corrections.json"


def load() -> dict:
    """Laad {zoek: vervang} dict. Leeg dict als bestand niet bestaat of fout."""
    if not _FILE.exists():
        return {}
    try:
        return json.loads(_FILE.read_text("utf-8")).get("nl_vervangingen", {})
    except Exception:
        return {}


def save(corrections: dict) -> None:
    """Sla {zoek: vervang} dict op naar corrections.json (stil falen op Render)."""
    try:
        _FILE.write_text(
            json.dumps({"nl_vervangingen": corrections}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def apply(translations: dict, corrections: dict) -> dict:
    """Pas alle correcties toe op een {block_id: tekst} dict."""
    if not corrections:
        return translations
    out = {}
    for bid, text in translations.items():
        for find, replace in corrections.items():
            if find in text:
                text = text.replace(find, replace)
        out[bid] = text
    return out


def to_json(corrections: dict) -> str:
    """Geef correcties terug als JSON string voor export."""
    return json.dumps({"nl_vervangingen": corrections}, ensure_ascii=False, indent=2)


def from_json(raw: str) -> dict:
    """Importeer correcties vanuit JSON string. Geeft {} terug bij fout."""
    try:
        return json.loads(raw).get("nl_vervangingen", {})
    except Exception:
        return {}
