"""EVE SSO helpers."""

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

from app.auth.scopes import BASE_SCOPES, CORP_WEBHOOK_SCOPES, MONITORING_SCOPES
from app.config import get_settings


ALLOWED_FLOWS = {"base", "monitoring", "corp_webhook"}


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def generate_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def get_callback_url() -> str:
    settings = get_settings()
    return f"{settings.eve_redirect_base.rstrip('/')}/auth/callback"


def scopes_for_flow(flow: str, existing_scopes: list[str] | None = None) -> list[str]:
    if flow not in ALLOWED_FLOWS:
        raise ValueError(f"Unsupported flow: {flow}")

    settings = get_settings()
    scopes = set(settings.eve_default_scopes) or set(BASE_SCOPES)
    scopes.update(scope for scope in (existing_scopes or []) if scope)
    if flow == "monitoring":
        scopes.update(MONITORING_SCOPES)
    elif flow == "corp_webhook":
        scopes.update(CORP_WEBHOOK_SCOPES)
    return sorted(scopes)


def build_authorize_url(
    flow: str,
    state: str,
    code_challenge: str,
    existing_scopes: list[str] | None = None,
) -> str:
    settings = get_settings()
    params = {
        "response_type": "code",
        "redirect_uri": get_callback_url(),
        "client_id": settings.eve_client_id,
        "scope": " ".join(scopes_for_flow(flow, existing_scopes=existing_scopes)),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.eve_authorize_url}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    settings = get_settings()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": get_callback_url(),
    }
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.eve_token_url,
            data=data,
            headers=headers,
            auth=(settings.eve_client_id, settings.eve_client_secret),
        )
    response.raise_for_status()
    return response.json()


async def fetch_character_identity(access_token: str) -> dict:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(settings.eve_verify_url, headers=headers)
    response.raise_for_status()

    payload = response.json()
    raw_scopes = payload.get("Scopes", "")
    character_id = int(payload["CharacterID"])

    corporation_id = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        corp_response = await client.get(
            f"{settings.eve_esi_base_url.rstrip('/')}/characters/{character_id}/",
            params={"datasource": "tranquility"},
        )
    if corp_response.is_success:
        corporation_id = int(corp_response.json().get("corporation_id", 0))

    return {
        "character_id": character_id,
        "character_name": payload["CharacterName"],
        "corporation_id": corporation_id,
        "character_owner_hash": payload.get("CharacterOwnerHash"),
        "scopes": [scope for scope in raw_scopes.split() if scope],
    }
