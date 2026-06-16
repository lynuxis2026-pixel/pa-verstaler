import json
import os
import re
import time
from typing import Callable, Optional

from anthropic import Anthropic, RateLimitError, APITimeoutError

MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 25
MAX_OUTPUT_TOKENS = 8192

SYSTEM_PROMPT = """Je bent een expert vertaler gespecialiseerd in het vertalen van theologische teksten van oud Bijbels Engels naar hedendaags Nederlands.

ARCHAÏSCH ENGELS → MODERN NEDERLANDS:
- thee / thou → jij / u (u bij formele/religieuze context, jij bij informeel)
- thy / thine → jouw / uw
- hath / hast → heeft / hebt
- doth / dost → doet / doe
- saith → zegt
- art → bent
- wast → was
- wilt → wil / zult
- wouldest / shouldest → zou / zou moeten
- canst / couldest → kunt / kon
- verily → voorwaar
- behold → zie / ziet
- henceforth / hereafter → voortaan / hierna
- thereof / therein / thereby → daarvan / daarin / daardoor
- whereby / wherein / wherefore → waardoor / waarin / daarom
- whosoever / whatsoever → wie dan ook / wat dan ook
- nay → nee / integendeel
- yea → ja / zelfs
- peradventure → misschien
- aforetime → vroeger / voorheen
- forthwith → onmiddellijk / terstond

THEOLOGISCHE VAKTERMEN:
- grace → genade
- redemption → verlossing
- sanctification → heiligmaking
- atonement → verzoening
- covenant → verbond
- elect / election → uitverkorenen / verkiezing
- justification → rechtvaardiging
- righteousness → gerechtigheid
- salvation → zaligheid
- propitiation → verzoening / zoenoffer
- repentance → bekering
- regeneration → wedergeboorte
- predestination → predestinatie / voorbeschikking
- sovereignty → soevereiniteit
- omnipotence → almacht
- omniscience → alwetendheid
- omnipresence → alomtegenwoordigheid
- imputation → toerekening
- mortification → doding (van de zonde)
- perseverance of the saints → volharding van de heiligen
- total depravity → totale verdorvenheid
- effectual calling → krachtige roeping
- means of grace → genademiddelen

GODDELIJKE VOORNAAMWOORDEN (altijd hoofdletter als verwijzing naar God/Christus):
- He / Him / His → Hij / Hem / Zijn
- Thee / Thou (bij gebed tot God) → U / Gij

BIJZONDERE TERMEN:
- LORD (in kleine kapitalen) → HEERE
- God → God (onveranderd)
- Christ / Jesus → Christus / Jezus (onveranderd)

BIJBELCITATEN:
- Gebruik de Statenvertaling-stijl
- Houd de plechtige toon aan bij herkenbare Bijbelteksten

NIET VERTALEN:
- Eigennamen van personen en plaatsen
- Afkortingen van Bijbelboeken (Gen., Ex., Matt., Joh., etc.)
- ISBN-nummers, paginanummers, alleen-cijfer-tekens
- Tekst die al in het Nederlands staat → geef deze ongewijzigd terug

INSTRUCTIES:
- Bewaar de theologische nauwkeurigheid en diepgang van de brontekst
- Gebruik vloeiend, hedendaags Nederlands dat toch de plechtigheid van de tekst bewaard
- Geef ALTIJD een JSON array terug als respons, geen tekst eromheen
- Formaat: [{"id": "...", "translated": "..."}]
- Eén object per invoeritem, zelfde volgorde als invoer"""


def _parse_response(response_text: str, original_batch: list[dict]) -> list[dict]:
    text = response_text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Fallback: keep original text for this batch
    return [{"id": item["id"], "translated": item["text"]} for item in original_batch]


def _translate_batch(client: Anthropic, batch: list[dict]) -> list[dict]:
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
            )
            return _parse_response(response.content[0].text, batch)
        except (RateLimitError, APITimeoutError):
            if attempt < 2:
                time.sleep(60 * (attempt + 1))
            else:
                raise

    return [{"id": item["id"], "translated": item["text"]} for item in batch]


def translate_blocks(
    blocks: list,
    api_key: str,
    on_batch: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Translate a list of TextBlocks. Returns {block_id: translated_text}."""
    # Auth Vault: app-key (sk-ant-av01-... of legacy av_sk_) of OAuth token → proxy
    # Negeer een lege ANTHROPIC_AUTH_TOKEN env-var (anders stuurt SDK "Bearer " en faalt httpx)
    if os.environ.get("ANTHROPIC_AUTH_TOKEN") == "":
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

    if (
        api_key == "auth-vault-proxy"
        or api_key.startswith("sk-ant-av01-")
        or api_key.startswith("av_sk_")
    ):
        proxy = os.getenv("AUTH_VAULT_PROXY", "http://localhost:7845")
        client = Anthropic(api_key=api_key, auth_token=None, base_url=proxy)
    elif api_key.startswith("sk-ant-oat"):
        # OAuth token (Claude Max): check of Auth Vault draait én klaar is
        proxy = os.getenv("AUTH_VAULT_PROXY", "http://localhost:7845")
        vault_ready = False
        try:
            import httpx as _httpx
            r = _httpx.get(proxy + "/", timeout=0.5)
            if r.is_success:
                data = r.json()
                info = data.get("anthropic") or data.get("proxy") or {}
                vault_ready = bool(info.get("ready") or info.get("configured"))
        except Exception:
            pass

        if vault_ready:
            client = Anthropic(api_key=api_key, auth_token=None, base_url=proxy)
        else:
            # Auth Vault niet actief/klaar — stuur bearer token direct naar Anthropic
            client = Anthropic(auth_token=api_key)
    else:
        client = Anthropic(api_key=api_key, auth_token=None)

    translatable = [b for b in blocks if b.block_type not in ("header", "footer")]
    batches = [translatable[i : i + BATCH_SIZE] for i in range(0, len(translatable), BATCH_SIZE)]

    results = {}
    for i, batch in enumerate(batches):
        payload = [{"id": b.block_id, "text": b.text} for b in batch]
        translated = _translate_batch(client, payload)

        id_map = {item["id"]: item.get("translated") or item.get("text", "") for item in translated}
        for block in batch:
            results[block.block_id] = id_map.get(block.block_id) or block.text

        if on_batch:
            on_batch(i + 1, len(batches))

    return results


def translate_blocks_free(
    blocks: list,
    on_batch: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Vertaal via gratis Google Translate. Geeft {block_id: vertaalde_tekst} terug."""
    from deep_translator import GoogleTranslator

    gt = GoogleTranslator(source="en", target="nl")
    translatable = [b for b in blocks if b.block_type not in ("header", "footer")]
    n_batches = max(1, (len(translatable) + BATCH_SIZE - 1) // BATCH_SIZE)
    results = {}

    for i, block in enumerate(translatable):
        text = block.text.strip()
        if text:
            try:
                if len(text) > 4500:
                    parts = [text[j : j + 4500] for j in range(0, len(text), 4500)]
                    results[block.block_id] = " ".join(
                        gt.translate(p) or p for p in parts
                    )
                else:
                    results[block.block_id] = gt.translate(text) or text
            except Exception:
                results[block.block_id] = text
        else:
            results[block.block_id] = text

        time.sleep(0.05)  # zachte throttle voor Google Translate

        if on_batch and (i + 1) % BATCH_SIZE == 0:
            on_batch((i + 1) // BATCH_SIZE, n_batches)

    if on_batch:
        on_batch(n_batches, n_batches)

    return results
