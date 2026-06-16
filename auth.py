"""API-sleutel beheer voor PA Verstaler (lokale app)."""
import json
import os
import time
from pathlib import Path

import httpx
import streamlit as st

# Auth Vault — lokale proxy die de Anthropic API-sleutel beheert
AUTH_VAULT_URL = os.getenv("AUTH_VAULT_PROXY", "http://localhost:7845").rstrip("/")
AUTH_VAULT_MARKER = "auth-vault-proxy"
_AV_KEY_FILE = Path(__file__).resolve().parent / ".av_key"

# Claude Code sloeg zijn OAuth-token op in ~/.claude/.credentials.json
_CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"


def login_wall() -> None:
    """Geen login vereist voor lokale app."""
    return


def _read_oat_token() -> str | None:
    """Lees Claude Max OAuth token automatisch uit ~/.claude/.credentials.json."""
    try:
        data = json.loads(_CLAUDE_CREDS.read_text("utf-8"))
        oat = data.get("claudeAiOauth", {})
        token = oat.get("accessToken", "")
        expires_ms = oat.get("expiresAt", 0)
        if token.startswith("sk-") and expires_ms > time.time() * 1000:
            return token
    except Exception:
        pass
    return None


def _check_auth_vault() -> dict | None:
    """Detecteer of Auth Vault draait en of de Anthropic-kant is geconfigureerd."""
    try:
        r = httpx.get(f"{AUTH_VAULT_URL}/", timeout=0.8)
        if r.is_success:
            data = r.json()
            if "anthropic" in data:
                return data["anthropic"]
            return data.get("proxy")
    except Exception:
        pass
    return None


def _find_key_in_json(data, depth: int = 0) -> str | None:
    """Zoek recursief naar een Anthropic API-sleutel (sk-ant-...) of OAuth-token."""
    if depth > 6:
        return None
    if isinstance(data, str) and data.startswith("sk-"):
        return data
    if isinstance(data, dict):
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            tok = data["claudeAiOauth"].get("accessToken")
            if isinstance(tok, str) and tok.startswith("sk-"):
                return tok
        for field in (
            "ANTHROPIC_API_KEY", "anthropic_api_key", "api_key", "apiKey",
            "accessToken", "access_token",
            "key", "token", "secret", "claude_api_key", "claudeApiKey",
            "anthropicApiKey", "CLAUDE_API_KEY",
        ):
            val = data.get(field)
            if isinstance(val, str) and val.startswith("sk-"):
                return val
        for val in data.values():
            result = _find_key_in_json(val, depth + 1)
            if result:
                return result
    if isinstance(data, list):
        for item in data:
            result = _find_key_in_json(item, depth + 1)
            if result:
                return result
    return None


def _get_or_create_vault_key() -> str:
    """Haal op of maak een av_sk_ app-key aan voor PA Verstaler via Auth Vault."""
    if _AV_KEY_FILE.exists():
        key = _AV_KEY_FILE.read_text().strip()
        if key.startswith("sk-ant-av01-") or key.startswith("av_sk_"):
            return key

    try:
        r = httpx.post(
            f"{AUTH_VAULT_URL}/api/app-keys",
            json={"name": "pa-verstaler", "service": "anthropic"},
            timeout=5,
        )
        if r.is_success:
            data = r.json()
            key = data.get("key", "")
            if isinstance(key, str) and (key.startswith("sk-ant-av01-") or key.startswith("av_sk_")):
                _AV_KEY_FILE.write_text(key)
                return key
    except Exception:
        pass

    return AUTH_VAULT_MARKER


def get_api_key() -> str:
    """Geef API-sleutel terug.

    Volgorde:
    1. Auth Vault av_sk_ key (proxy op localhost:7845)
    2. Claude Code OAuth token uit ~/.claude/.credentials.json
    3. Sessie (handmatig ingevoerde sleutel)
    4. ANTHROPIC_API_KEY env var
    """
    if not st.session_state.get("bypass_auth_vault"):
        info = _check_auth_vault()
        is_ready = info and (info.get("ready", False) or info.get("configured", False))
        if is_ready:
            return _get_or_create_vault_key()

    if not st.session_state.get("bypass_oat"):
        oat = _read_oat_token()
        if oat:
            return oat

    return (
        st.session_state.get("session_api_key")
        or os.getenv("ANTHROPIC_API_KEY", "")
    )


def show_sidebar() -> None:
    """Toon API-sleutelinvoer in de sidebar."""
    with st.sidebar:
        if not os.getenv("ANTHROPIC_API_KEY"):
            st.subheader("🔑 Claude API-sleutel")
            _auth_vault_widget()
            _api_key_widget()


def _auth_vault_widget() -> None:
    """Toont Auth Vault-status als die draait."""
    info = _check_auth_vault()
    if info is None:
        return

    is_ready = info.get("ready", info.get("configured", False))
    if is_ready:
        source = info.get("source", "")
        st.success(f"🔌 **Auth Vault verbonden**\n\n{source or 'Bridge actief'}")
        st.caption("De vertaler vraagt automatisch een app-key op via Auth Vault.")
        if st.button("Sleutel handmatig invoeren", key="bypass_av", use_container_width=True):
            st.session_state["bypass_auth_vault"] = True
            st.rerun()
        st.divider()
    else:
        st.warning(
            "🔌 **Auth Vault draait**, maar de Anthropic bridge is niet actief.\n\n"
            "Log in via Auth Vault → **Login → Claude**."
        )
        st.divider()


def _api_key_widget() -> None:
    """JSON-upload of tekst-invoer voor de Anthropic API-sleutel."""
    if not st.session_state.get("bypass_oat") and not _check_auth_vault():
        oat = _read_oat_token()
        if oat:
            st.success("✅ **Claude Max — automatisch ingelogd**\n\nJe Claude Code-aanmelding wordt gebruikt.")
            if st.button("Andere sleutel gebruiken", key="bypass_oat_btn", use_container_width=True):
                st.session_state["bypass_oat"] = True
                st.rerun()
            return

    if st.session_state.get("session_api_key"):
        st.success("✅ Sleutel actief")
        if st.button("🗑 Verwijderen", key="remove_api_key", use_container_width=True):
            st.session_state.pop("session_api_key", None)
            st.rerun()
        return

    tab_json, tab_text = st.tabs(["📄 JSON bestand", "✏️ Tekst"])

    with tab_json:
        st.caption('Elk JSON met een `sk-ant-...` waarde werkt.')
        uploaded = st.file_uploader(
            "Upload credentials JSON",
            type=["json"],
            key="sidebar_json_upload",
            label_visibility="collapsed",
        )
        if uploaded:
            try:
                data = json.loads(uploaded.read())
                key = _find_key_in_json(data)
                if key:
                    st.session_state.session_api_key = key
                    st.rerun()
                else:
                    st.error(
                        "Geen API-sleutel gevonden in dit JSON bestand. "
                        "Gebruik het **Tekst** tabblad en plak de sleutel handmatig."
                    )
            except Exception as e:
                st.error(f"Ongeldig JSON: {e}")

    with tab_text:
        key_input = st.text_input(
            "API-sleutel",
            type="password",
            placeholder="sk-ant-api03-...",
            key="sidebar_key_text",
            label_visibility="collapsed",
        )
        if st.button("Opslaan", key="save_api_key", use_container_width=True):
            if key_input and key_input.startswith("sk-"):
                st.session_state.session_api_key = key_input
                st.rerun()
            else:
                st.error("Sleutel moet beginnen met `sk-`")
