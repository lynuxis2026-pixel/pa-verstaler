"""Auth0 login-bescherming en API-sleutel beheer voor PA Verstaler."""
import json
import os
from pathlib import Path

import httpx
import streamlit as st

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET", "")
APP_URL = os.getenv("APP_URL", "http://localhost:8501").rstrip("/")

# Auth Vault — lokale proxy die de Anthropic API-sleutel beheert
AUTH_VAULT_URL = os.getenv("AUTH_VAULT_PROXY", "http://localhost:7845").rstrip("/")
AUTH_VAULT_MARKER = "auth-vault-proxy"  # fallback marker als geen av_sk_ beschikbaar
_AV_KEY_FILE = Path(__file__).resolve().parent / ".av_key"  # gecachete app-key


def _check_auth_vault() -> dict | None:
    """Detecteer of Auth Vault draait en of er een API-sleutel is geconfigureerd.

    Returns:
        dict met {'configured': bool, 'maskedKey': str} of None als niet bereikbaar.
    """
    try:
        r = httpx.get(f"{AUTH_VAULT_URL}/", timeout=0.8)
        if r.is_success:
            return r.json().get("proxy")
    except Exception:
        pass
    return None


# ── OAuth helpers ──────────────────────────────────────────────────────────

def _exchange_code(code: str) -> dict | None:
    """Wissel Auth0 autorisatiecode in voor een access token."""
    try:
        resp = httpx.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": AUTH0_CLIENT_ID,
                "client_secret": AUTH0_CLIENT_SECRET,
                "code": code,
                "redirect_uri": APP_URL,
            },
            timeout=15,
        )
        return resp.json() if resp.is_success else None
    except Exception:
        return None


def _get_userinfo(access_token: str) -> dict | None:
    """Haal gebruikersinfo op via Auth0 /userinfo."""
    try:
        resp = httpx.get(
            f"https://{AUTH0_DOMAIN}/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return resp.json() if resp.is_success else None
    except Exception:
        return None


# ── Login wall ─────────────────────────────────────────────────────────────

def login_wall() -> None:
    """
    Toont een loginpagina als de gebruiker niet is ingelogd.
    Slaat de app stil (st.stop) totdat de login is afgerond.
    Als AUTH0_DOMAIN niet is ingesteld → geen login vereist (lokale dev).
    """
    if not AUTH0_DOMAIN:
        return  # lokale ontwikkeling: overslaan

    # Verwerk OAuth callback (?code=...)
    params = st.query_params
    if "code" in params and not st.session_state.get("authenticated"):
        token_data = _exchange_code(params["code"])
        if token_data and "access_token" in token_data:
            userinfo = _get_userinfo(token_data["access_token"]) or {}
            st.session_state.authenticated = True
            st.session_state.user_name = userinfo.get("name", "")
            st.session_state.user_email = userinfo.get("email", "")
            st.query_params.clear()
            st.rerun()
        else:
            st.query_params.clear()
            st.error("Inloggen mislukt — probeer opnieuw.")

    if st.session_state.get("authenticated"):
        return

    # Login pagina tonen
    st.markdown("## 📖 PA Verstaler")
    st.write("Log in om de vertaaldienst te gebruiken.")

    auth_url = (
        f"https://{AUTH0_DOMAIN}/authorize"
        f"?client_id={AUTH0_CLIENT_ID}"
        f"&redirect_uri={APP_URL}"
        f"&response_type=code"
        f"&scope=openid+profile+email"
    )
    st.link_button("🔐 Inloggen", auth_url, type="primary")
    st.stop()


# ── Sidebar: gebruiker + API-sleutel ──────────────────────────────────────

def show_sidebar() -> None:
    """Toon ingelogde gebruiker en API-sleutelinvoer in de sidebar."""
    with st.sidebar:
        # Ingelogde gebruiker + uitlogknop
        if AUTH0_DOMAIN and st.session_state.get("authenticated"):
            label = (
                st.session_state.get("user_name")
                or st.session_state.get("user_email")
                or "Ingelogd"
            )
            st.caption(f"👤 **{label}**")
            logout_url = (
                f"https://{AUTH0_DOMAIN}/v2/logout"
                f"?client_id={AUTH0_CLIENT_ID}"
                f"&returnTo={APP_URL}"
            )
            st.link_button("Uitloggen", logout_url, use_container_width=True)
            st.divider()

        # API-sleutel sectie — alleen zichtbaar als er geen server-sleutel is
        if not os.getenv("ANTHROPIC_API_KEY"):
            st.subheader("🔑 Claude API-sleutel")
            _auth_vault_widget()  # toont Auth Vault status als die draait
            _api_key_widget()


def _auth_vault_widget() -> None:
    """Toont Auth Vault-status. Als Auth Vault draait én een sleutel heeft, gebruik die."""
    info = _check_auth_vault()
    if info is None:
        return  # Auth Vault niet bereikbaar — verberg helemaal

    if info.get("configured"):
        masked = info.get("maskedKey", "")
        st.success(f"🔌 **Auth Vault verbonden**\n\nSleutel: `{masked}`")
        st.caption("De vertaler gebruikt automatisch de sleutel uit Auth Vault.")
        if st.button("Sleutel handmatig invoeren in plaats daarvan", key="bypass_av", use_container_width=True):
            st.session_state["bypass_auth_vault"] = True
            st.rerun()
        st.divider()
    else:
        st.warning(
            "🔌 **Auth Vault draait**, maar er is nog geen API-sleutel ingesteld.\n\n"
            "Klik in Auth Vault op **🔑 API Key** om je `sk-ant-api03-...` op te slaan."
        )
        st.divider()


def _find_key_in_json(data, depth: int = 0) -> str | None:
    """Zoek recursief naar een Anthropic API-sleutel (sk-ant-...) of OAuth-token."""
    if depth > 6:
        return None
    if isinstance(data, str) and data.startswith("sk-"):
        return data
    if isinstance(data, dict):
        # Auth Vault credentials JSON — neem expliciet de access token
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            tok = data["claudeAiOauth"].get("accessToken")
            if isinstance(tok, str) and tok.startswith("sk-"):
                return tok
        # Bekende veldnamen eerst
        for field in (
            "ANTHROPIC_API_KEY", "anthropic_api_key", "api_key", "apiKey",
            "accessToken", "access_token",
            "key", "token", "secret", "claude_api_key", "claudeApiKey",
            "anthropicApiKey", "CLAUDE_API_KEY",
        ):
            val = data.get(field)
            if isinstance(val, str) and val.startswith("sk-"):
                return val
        # Dan recursief door alle waarden
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


def _api_key_widget() -> None:
    """JSON-upload of tekst-invoer voor de Anthropic API-sleutel."""
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


# ── Auth Vault app-key beheer ──────────────────────────────────────────────

def _get_or_create_vault_key() -> str:
    """Haal op of maak een av_sk_ app-key aan voor PA Verstaler via Auth Vault.

    De sleutel wordt lokaal gecacht in .av_key zodat er niet bij elke start
    een nieuwe key aangemaakt wordt.
    """
    # 1. Lokaal gecacht
    if _AV_KEY_FILE.exists():
        key = _AV_KEY_FILE.read_text().strip()
        if key.startswith("av_sk_"):
            return key

    # 2. Maak een nieuwe app-key aan via Auth Vault
    try:
        r = httpx.post(
            f"{AUTH_VAULT_URL}/api/app-keys",
            json={"name": "pa-verstaler"},
            timeout=5,
        )
        if r.is_success:
            data = r.json()
            # Zoek de av_sk_ waarde in de response (ongeacht veldnaam)
            key = None
            for field in ("key", "apiKey", "api_key", "value", "secret", "token"):
                val = data.get(field, "")
                if isinstance(val, str) and val.startswith("av_sk_"):
                    key = val
                    break
            if not key:
                for val in data.values():
                    if isinstance(val, str) and val.startswith("av_sk_"):
                        key = val
                        break
            if key:
                _AV_KEY_FILE.write_text(key)
                return key
    except Exception:
        pass

    # 3. Fallback: marker — proxy injecteert OAuth token ook zonder app-key
    return AUTH_VAULT_MARKER


# ── API key getter ─────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Geef API-sleutel terug.

    Volgorde:
    1. Auth Vault av_sk_ key (proxy op localhost:7845) — tenzij bypassed
    2. Sessie (handmatig ingevoerde sleutel)
    3. ANTHROPIC_API_KEY env var
    """
    if not st.session_state.get("bypass_auth_vault"):
        info = _check_auth_vault()
        if info and info.get("configured"):
            return _get_or_create_vault_key()

    return (
        st.session_state.get("session_api_key")
        or os.getenv("ANTHROPIC_API_KEY", "")
    )
