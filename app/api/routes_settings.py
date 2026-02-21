"""Settings routes for personal and corporation delivery configuration."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from urllib.parse import quote_plus

from cryptography.fernet import InvalidToken
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from app.auth.scopes import CORP_ROLES_SCOPE
from app.db.models import Character, CorpSetting
from app.db.session import SessionLocal
from app.delivery.mentions import (
    MENTION_CHANNEL,
    MENTION_NONE,
    MENTION_ROLE,
    MENTION_USER,
    build_mention_text,
)
from app.esi.client import fetch_character_roles, refresh_access_token
from app.security.csrf import ensure_csrf_session_id, validate_csrf_token
from app.security.crypto import decrypt_refresh_token, encrypt_refresh_token
from app.telemetry.events import set_telemetry_consent

router = APIRouter()

MANAGEABLE_CORP_ROLES = (
    "Accountant",
    "Auditor",
    "Brand_Manager",
    "Communications_Officer",
    "Config_Equipment",
    "Config_Starbase_Equipment",
    "Contract_Manager",
    "Diplomat",
    "Director",
    "Factory_Manager",
    "Fitting_Manager",
    "Junior_Accountant",
    "Personnel_Manager",
    "Project_Manager",
    "Security_Officer",
    "Skill_Plan_Manager",
    "Starbase_Defense_Operator",
    "Starbase_Fuel_Technician",
    "Station_Manager",
    "Trader",
)
DEFAULT_ALLOWED_CORP_ROLES = {
    "Communications_Officer",
    "Director",
    "Security_Officer",
    "Station_Manager",
}


def _redirect_with_notice(message: str, *, error: bool = False) -> RedirectResponse:
    encoded = quote_plus(message)
    return RedirectResponse(
        url=f"/alerts?{'error' if error else 'notice'}={encoded}",
        status_code=302,
    )


def _redirect_home(request: Request, message: str, *, error: bool = False) -> RedirectResponse:
    if error:
        request.session["auth_error"] = message
    else:
        request.session["home_notice"] = message
    return RedirectResponse(url="/", status_code=302)


def _parse_scopes(scopes_blob: str | None) -> set[str]:
    if not scopes_blob:
        return set()
    return {scope for scope in scopes_blob.split() if scope}


def _normalize_webhook_url(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not value.startswith("https://"):
        raise ValueError("Webhook URL must start with https://")
    if "/api/webhooks/" not in value:
        raise ValueError("Webhook URL must be a Discord webhook URL")
    return value


def _parse_mention_from_form(form, *, prefix: str) -> str:
    mode = str(form.get(f"{prefix}_mention_mode", MENTION_NONE)).strip().lower()
    if mode == MENTION_USER:
        mention_id = str(form.get(f"{prefix}_mention_user_id", ""))
    elif mode == MENTION_ROLE:
        mention_id = str(form.get(f"{prefix}_mention_role_id", ""))
    elif mode == MENTION_CHANNEL:
        mention_id = str(form.get(f"{prefix}_mention_channel_id", ""))
    else:
        mention_id = ""

    return build_mention_text(mode, mention_id)


def _sanitize_allowed_roles(raw_roles: set[str]) -> set[str]:
    valid_roles = set(MANAGEABLE_CORP_ROLES)
    filtered = {role for role in raw_roles if role in valid_roles}
    return filtered or set(DEFAULT_ALLOWED_CORP_ROLES)


def _parse_allowed_roles_from_form(form) -> set[str] | None:
    selected = {
        str(value).strip()
        for value in form.getlist("corp_allowed_roles")
        if str(value).strip()
    }
    if not selected:
        return None
    return _sanitize_allowed_roles(selected)


def _load_allowed_roles(corp_setting: CorpSetting | None) -> set[str]:
    if corp_setting is None or not corp_setting.allowed_roles:
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    try:
        parsed = json.loads(corp_setting.allowed_roles)
    except ValueError:
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    if not isinstance(parsed, list):
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    roles = {str(role).strip() for role in parsed if str(role).strip()}
    return _sanitize_allowed_roles(roles)


def _require_logged_in_character_id(request: Request) -> int | None:
    session_character = request.session.get("character") or {}
    character_id = session_character.get("character_id")
    if not character_id:
        return None
    try:
        return int(character_id)
    except (TypeError, ValueError):
        return None


def _validate_form_csrf(request: Request, form) -> bool:
    csrf_token = str(form.get("csrf_token", ""))
    session_id = ensure_csrf_session_id(request.session)
    return validate_csrf_token(session_id, csrf_token)


def _fetch_live_corp_roles(character: Character) -> tuple[set[str] | None, str | None]:
    scopes = _parse_scopes(character.scopes)
    if CORP_ROLES_SCOPE not in scopes:
        return (
            None,
            "Corp webhook update requires corp roles scope. Run corp authorization first.",
        )
    encrypted_refresh = (character.refresh_token_encrypted or "").strip()
    if not encrypted_refresh:
        return (
            None,
            "Missing refresh token for corp role verification. Re-enable monitoring and corp auth.",
        )

    try:
        refresh_token = decrypt_refresh_token(encrypted_refresh)
    except InvalidToken:
        return (
            None,
            "Invalid encrypted token. Check FERNET_KEY/SESSION_SECRET and re-auth.",
        )

    try:
        token_data = refresh_access_token(refresh_token)
        access_token = str(token_data.get("access_token", "")).strip()
        if not access_token:
            return None, "SSO token refresh returned no access token."
        rotated_refresh = str(token_data.get("refresh_token", "")).strip()
        if rotated_refresh:
            character.refresh_token_encrypted = encrypt_refresh_token(rotated_refresh)
        roles = set(fetch_character_roles(character.character_id, access_token))
    except httpx.HTTPError as exc:
        return None, f"Corp role verification request failed: {exc}"

    return roles, None


def _can_edit_corp_webhook(
    *,
    roles: set[str],
    allowed_roles: set[str],
) -> bool:
    return bool("Director" in roles or allowed_roles.intersection(roles))


@router.get("/me")
def get_my_settings(request: Request):
    """Return current character and corp delivery settings."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        return {"error": "not_authenticated"}

    with SessionLocal() as db:
        character = db.get(Character, character_id)
        if character is None:
            return {"error": "character_not_found"}
        corp_setting = db.get(CorpSetting, character.corporation_id)
        return {
            "character_id": character.character_id,
            "monitoring_enabled": bool(character.monitoring_enabled),
            "personal_webhook_url": character.personal_webhook_url,
            "personal_mention_text": character.personal_mention_text,
            "use_corp_webhook": bool(character.use_corp_webhook),
            "corp_webhook_exists": bool(corp_setting),
            "corp_mention_text": corp_setting.mention_text if corp_setting else "",
            "corp_allowed_roles": sorted(_load_allowed_roles(corp_setting)),
            "corp_manageable_roles": list(MANAGEABLE_CORP_ROLES),
        }


@router.post("/me/webhook")
async def set_personal_webhook(request: Request):
    """Set personal webhook URL and mention text."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        request.session["auth_error"] = "Log in first to configure alerts."
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_with_notice("Invalid CSRF token. Refresh and try again.", error=True)
    raw_url = str(form.get("personal_webhook_url", ""))
    try:
        mention_text = _parse_mention_from_form(form, prefix="personal")
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)

    try:
        webhook_url = _normalize_webhook_url(raw_url)
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)

    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        with SessionLocal() as db:
            character = db.get(Character, character_id)
            if character is None:
                return _redirect_with_notice("Character record not found.", error=True)
            character.personal_webhook_url = webhook_url
            character.personal_mention_text = mention_text
            if webhook_url:
                character.use_corp_webhook = False
            character.updated_at = now
            db.commit()
    except SQLAlchemyError:
        return _redirect_with_notice("Failed to save personal webhook settings.", error=True)

    return _redirect_with_notice("Personal webhook settings saved.")


@router.post("/me/use-corp")
async def set_use_corp_webhook(request: Request):
    """Toggle use_corp_webhook preference."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        request.session["auth_error"] = "Log in first to configure alerts."
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_with_notice("Invalid CSRF token. Refresh and try again.", error=True)
    raw_value = str(form.get("use_corp_webhook", "")).strip().lower()
    use_corp_webhook = raw_value in {"1", "true", "yes", "on"}
    now = datetime.now(UTC).replace(tzinfo=None)

    try:
        with SessionLocal() as db:
            character = db.get(Character, character_id)
            if character is None:
                return _redirect_with_notice("Character record not found.", error=True)
            if use_corp_webhook:
                corp_setting = db.get(CorpSetting, character.corporation_id)
                if corp_setting is None:
                    return _redirect_with_notice(
                        "No corporation webhook is configured yet.",
                        error=True,
                    )
            character.use_corp_webhook = use_corp_webhook
            character.updated_at = now
            db.commit()
    except SQLAlchemyError:
        return _redirect_with_notice("Failed to update corp webhook preference.", error=True)

    if use_corp_webhook:
        return _redirect_with_notice("Using corporation webhook for alerts.")
    return _redirect_with_notice("Corporation webhook preference disabled.")


@router.post("/me/delivery")
async def set_delivery_mode(request: Request):
    """Set preferred delivery mode."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        request.session["auth_error"] = "Log in first to configure alerts."
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_with_notice("Invalid CSRF token. Refresh and try again.", error=True)
    delivery_mode = str(form.get("delivery_mode", "eve_mail")).strip().lower()
    raw_personal_webhook = str(form.get("personal_webhook_url", ""))
    try:
        personal_mention_text = _parse_mention_from_form(form, prefix="personal")
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)

    if delivery_mode not in {"eve_mail", "personal_webhook", "corp_webhook"}:
        return _redirect_with_notice("Unsupported delivery mode.", error=True)

    try:
        provided_webhook_url = _normalize_webhook_url(raw_personal_webhook)
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)

    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        with SessionLocal() as db:
            character = db.get(Character, character_id)
            if character is None:
                return _redirect_with_notice("Character record not found.", error=True)

            if delivery_mode == "eve_mail":
                character.personal_webhook_url = None
                character.personal_mention_text = ""
                character.use_corp_webhook = False
                notice = "Delivery mode set to EVE Mail fallback."
            elif delivery_mode == "personal_webhook":
                if not provided_webhook_url:
                    return _redirect_with_notice(
                        "Personal webhook URL is required for personal delivery mode.",
                        error=True,
                    )
                character.personal_webhook_url = provided_webhook_url
                character.personal_mention_text = personal_mention_text
                character.use_corp_webhook = False
                notice = "Delivery mode set to personal Discord webhook."
            else:
                corp_setting = db.get(CorpSetting, character.corporation_id)
                if corp_setting is None:
                    return _redirect_with_notice(
                        "No corporation webhook is configured yet.",
                        error=True,
                    )
                if provided_webhook_url:
                    character.personal_webhook_url = provided_webhook_url
                    character.personal_mention_text = personal_mention_text
                character.use_corp_webhook = True
                notice = "Delivery mode set to corporation webhook."

            character.updated_at = now
            db.commit()
    except SQLAlchemyError:
        return _redirect_with_notice("Failed to save delivery settings.", error=True)

    return _redirect_with_notice(notice)


@router.post("/corp/webhook")
async def set_corp_webhook(request: Request):
    """Set corporation webhook configuration after role authorization."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        request.session["auth_error"] = "Log in first to configure alerts."
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_with_notice("Invalid CSRF token. Refresh and try again.", error=True)
    raw_webhook_url = str(form.get("corp_webhook_url", ""))
    try:
        mention_text = _parse_mention_from_form(form, prefix="corp")
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)
    submitted_allowed_roles = _parse_allowed_roles_from_form(form)

    try:
        webhook_url = _normalize_webhook_url(raw_webhook_url)
    except ValueError as exc:
        return _redirect_with_notice(str(exc), error=True)
    if not webhook_url:
        return _redirect_with_notice("Corporation webhook URL is required.", error=True)

    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        with SessionLocal() as db:
            character = db.get(Character, character_id)
            if character is None:
                return _redirect_with_notice("Character record not found.", error=True)

            corp_setting = db.get(CorpSetting, character.corporation_id)
            allowed_roles = _load_allowed_roles(corp_setting)
            roles, error = _fetch_live_corp_roles(character)
            if roles is None:
                db.rollback()
                return _redirect_with_notice(error or "Not authorized.", error=True)
            is_director = "Director" in roles
            if not _can_edit_corp_webhook(roles=roles, allowed_roles=allowed_roles):
                db.rollback()
                return _redirect_with_notice(
                    "You do not have a corporation role permitted to edit webhook settings.",
                    error=True,
                )
            effective_allowed_roles = allowed_roles
            if is_director and submitted_allowed_roles is not None:
                effective_allowed_roles = submitted_allowed_roles
            if corp_setting is None:
                corp_setting = CorpSetting(
                    corporation_id=character.corporation_id,
                    webhook_url=webhook_url,
                    mention_text=mention_text,
                    allowed_roles=json.dumps(sorted(effective_allowed_roles)),
                    updated_by_character_id=character.character_id,
                    updated_at=now,
                )
                db.add(corp_setting)
            else:
                corp_setting.webhook_url = webhook_url
                corp_setting.mention_text = mention_text
                corp_setting.allowed_roles = json.dumps(sorted(effective_allowed_roles))
                corp_setting.updated_by_character_id = character.character_id
                corp_setting.updated_at = now

            character.use_corp_webhook = True
            character.updated_at = now
            db.commit()
    except SQLAlchemyError:
        return _redirect_with_notice("Failed to save corporation webhook.", error=True)

    return _redirect_with_notice("Corporation webhook configuration saved.")


@router.post("/corp/webhook/delete")
async def delete_corp_webhook(request: Request):
    """Remove corporation webhook configuration after role authorization."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        request.session["auth_error"] = "Log in first to configure alerts."
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_with_notice("Invalid CSRF token. Refresh and try again.", error=True)

    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        with SessionLocal() as db:
            character = db.get(Character, character_id)
            if character is None:
                return _redirect_with_notice("Character record not found.", error=True)

            corp_setting = db.get(CorpSetting, character.corporation_id)
            if corp_setting is None:
                return _redirect_with_notice("No corporation webhook is configured.", error=True)

            allowed_roles = _load_allowed_roles(corp_setting)
            roles, error = _fetch_live_corp_roles(character)
            if roles is None:
                db.rollback()
                return _redirect_with_notice(error or "Not authorized.", error=True)
            if not _can_edit_corp_webhook(roles=roles, allowed_roles=allowed_roles):
                db.rollback()
                return _redirect_with_notice(
                    "You do not have a corporation role permitted to edit webhook settings.",
                    error=True,
                )

            db.delete(corp_setting)
            character.use_corp_webhook = False
            character.updated_at = now
            db.commit()
    except SQLAlchemyError:
        return _redirect_with_notice("Failed to delete corporation webhook.", error=True)

    return _redirect_with_notice("Corporation webhook removed.")


@router.post("/telemetry/consent")
async def set_telemetry_consent_choice(request: Request):
    """Persist one-time telemetry consent choice (admin only)."""
    character_id = _require_logged_in_character_id(request)
    if character_id is None:
        return _redirect_home(request, "Log in first to manage telemetry consent.", error=True)

    session_character = request.session.get("character") or {}
    if not bool(session_character.get("is_admin")):
        return _redirect_home(request, "Only admins can manage telemetry consent.", error=True)

    form = await request.form()
    if not _validate_form_csrf(request, form):
        return _redirect_home(request, "Invalid CSRF token. Refresh and try again.", error=True)

    decision = str(form.get("decision", "")).strip().lower()
    if decision not in {"allow", "decline"}:
        return _redirect_home(request, "Choose allow or decline for telemetry consent.", error=True)

    granted = decision == "allow"
    set_telemetry_consent(granted)
    if granted:
        return _redirect_home(
            request,
            "Telemetry consent saved: enabled for this install.",
            error=False,
        )
    return _redirect_home(
        request,
        "Telemetry consent saved: declined for this install.",
        error=False,
    )
