"""Authentication and SSO routes."""

import httpx
from urllib.parse import urlencode, urlparse
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from app.auth.sso import (
    ALLOWED_FLOWS,
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_character_identity,
    generate_code_challenge,
    generate_code_verifier,
    generate_oauth_state,
)
from app.config import get_settings
from app.db.models import Character, Delivery, EsiState, Notification
from app.db.session import SessionLocal
from app.security.csrf import ensure_csrf_session_id, validate_csrf_token
from app.services.character_store import (
    disable_character_monitoring,
    upsert_character_from_identity,
)

router = APIRouter()


def _normalized_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _canonical_login_redirect(base_url: str, flow: str) -> str:
    query = urlencode({"flow": flow})
    return f"{base_url.rstrip('/')}/auth/login?{query}"


def _parse_scopes(scopes_blob: str | None) -> list[str]:
    if not scopes_blob:
        return []
    return [scope for scope in scopes_blob.split() if scope]


async def _validate_post_csrf(request: Request) -> bool:
    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    session_id = ensure_csrf_session_id(request.session)
    return validate_csrf_token(session_id, csrf_token)


@router.get("/login")
def login(
    request: Request,
    flow: str = Query(default="base", pattern="^(base|monitoring|corp_webhook)$"),
):
    """Start SSO flow."""
    settings = get_settings()
    if not settings.eve_client_id or not settings.eve_client_secret:
        raise HTTPException(
            status_code=500,
            detail="Missing EVE SSO credentials. Set EVE_CLIENT_ID and EVE_CLIENT_SECRET.",
        )
    if flow not in ALLOWED_FLOWS:
        raise HTTPException(status_code=400, detail="Unsupported auth flow")

    configured_base = _normalized_base_url(settings.eve_redirect_base)
    request_base = _normalized_base_url(str(request.base_url))
    if request_base != configured_base:
        # Keep session host consistent with redirect_uri host (localhost vs 127.0.0.1).
        return RedirectResponse(
            url=_canonical_login_redirect(configured_base, flow),
            status_code=302,
        )

    state = generate_oauth_state()
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)

    request.session["oauth"] = {
        "state": state,
        "code_verifier": code_verifier,
        "flow": flow,
    }
    existing_scopes: list[str] = []
    session_character = request.session.get("character") or {}
    session_character_id = session_character.get("character_id")
    if session_character_id:
        try:
            with SessionLocal() as db:
                row = db.get(Character, int(session_character_id))
            if row is not None:
                existing_scopes = _parse_scopes(row.scopes)
        except (SQLAlchemyError, ValueError):
            existing_scopes = []

    authorize_url = build_authorize_url(
        flow=flow,
        state=state,
        code_challenge=code_challenge,
        existing_scopes=existing_scopes,
    )
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get("/monitoring/start")
def start_monitoring():
    """Start monitoring scope flow."""
    return RedirectResponse(url="/auth/login?flow=monitoring", status_code=302)


@router.get("/corp-webhook/start")
def start_corp_webhook_auth():
    """Start corp role scope flow."""
    return RedirectResponse(url="/auth/login?flow=corp_webhook", status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Handle OAuth callback for all flows."""
    if error:
        request.session["auth_error"] = f"SSO error: {error}"
        return RedirectResponse(url="/", status_code=302)

    oauth_context = request.session.get("oauth") or {}
    expected_state = oauth_context.get("state")
    code_verifier = oauth_context.get("code_verifier")
    flow = oauth_context.get("flow", "base")

    if not expected_state or not code_verifier:
        request.session["auth_error"] = (
            "Missing login context. Start login from the same host as "
            f"EVE_REDIRECT_BASE ({get_settings().eve_redirect_base})."
        )
        return RedirectResponse(url="/", status_code=302)
    if not state or state != expected_state:
        request.session["auth_error"] = "Invalid OAuth state. Start login again."
        return RedirectResponse(url="/", status_code=302)
    if not code:
        request.session["auth_error"] = "Missing OAuth code. Start login again."
        return RedirectResponse(url="/", status_code=302)

    try:
        token_data = await exchange_code_for_tokens(code=code, code_verifier=code_verifier)
        identity = await fetch_character_identity(access_token=token_data["access_token"])
    except KeyError:
        request.session["auth_error"] = "Token response missing access token."
        return RedirectResponse(url="/", status_code=302)
    except httpx.HTTPError as exc:
        request.session["auth_error"] = f"SSO request failed: {exc}"
        return RedirectResponse(url="/", status_code=302)

    refresh_token = token_data.get("refresh_token")
    if flow in {"monitoring", "corp_webhook"} and not refresh_token:
        request.session["auth_error"] = (
            "Authorization did not return a refresh token. Start the flow again."
        )
        return RedirectResponse(url="/", status_code=302)

    try:
        with SessionLocal() as db:
            character = upsert_character_from_identity(
                db,
                character_id=identity["character_id"],
                character_name=identity["character_name"],
                corporation_id=identity["corporation_id"],
                scopes=identity["scopes"],
                enable_monitoring=flow == "monitoring",
                refresh_token=refresh_token if flow in {"monitoring", "corp_webhook"} else None,
            )
    except (SQLAlchemyError, ValueError):
        request.session["auth_error"] = "Failed to persist character login data."
        return RedirectResponse(url="/", status_code=302)

    settings = get_settings()
    is_admin = identity["character_id"] in settings.admin_character_ids
    request.session.pop("oauth", None)
    request.session.pop("auth_error", None)
    request.session["character"] = {
        "character_id": identity["character_id"],
        "character_name": identity["character_name"],
        "corporation_id": identity["corporation_id"],
        "is_admin": is_admin,
        "monitoring_enabled": bool(character.monitoring_enabled),
        "scopes": _parse_scopes(character.scopes),
    }
    return RedirectResponse(url="/", status_code=302)


@router.post("/deauth")
async def deauth(request: Request):
    """Deauthorize character locally, clear stored state, and log out."""
    if not await _validate_post_csrf(request):
        request.session["auth_error"] = "Invalid CSRF token. Refresh and try again."
        return RedirectResponse(url="/", status_code=302)

    session_character = request.session.get("character") or {}
    character_id = session_character.get("character_id")

    if character_id:
        try:
            character_id_int = int(character_id)
        except (TypeError, ValueError):
            character_id_int = None
        if character_id_int:
            try:
                with SessionLocal() as db:
                    db.execute(
                        delete(Delivery).where(Delivery.character_id == character_id_int)
                    )
                    db.execute(
                        delete(Notification).where(Notification.character_id == character_id_int)
                    )
                    db.execute(
                        delete(EsiState).where(EsiState.character_id == character_id_int)
                    )
                    db.execute(
                        delete(Character).where(Character.character_id == character_id_int)
                    )
                    db.commit()
            except SQLAlchemyError:
                request.session["auth_error"] = "Failed to deauthorize character. Try again."
                return RedirectResponse(url="/", status_code=302)

    request.session.pop("character", None)
    request.session.pop("oauth", None)
    request.session.pop("auth_error", None)
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
def logout(request: Request):
    request.session.pop("character", None)
    request.session.pop("oauth", None)
    request.session.pop("auth_error", None)
    return RedirectResponse(url="/", status_code=302)


@router.post("/monitoring/disable")
async def disable_monitoring(request: Request):
    if not await _validate_post_csrf(request):
        request.session["auth_error"] = "Invalid CSRF token. Refresh and try again."
        return RedirectResponse(url="/", status_code=302)

    character = request.session.get("character") or {}
    character_id = character.get("character_id")
    if not character_id:
        request.session["auth_error"] = "Log in first to manage monitoring."
        return RedirectResponse(url="/", status_code=302)

    try:
        with SessionLocal() as db:
            disabled = disable_character_monitoring(db, character_id=int(character_id))
    except SQLAlchemyError:
        request.session["auth_error"] = "Failed to disable monitoring."
        return RedirectResponse(url="/", status_code=302)

    if not disabled:
        request.session["auth_error"] = "Character record was not found."
        return RedirectResponse(url="/", status_code=302)

    character["monitoring_enabled"] = False
    request.session["character"] = character
    return RedirectResponse(url="/", status_code=302)
