"""Web page routes."""

from datetime import UTC, datetime, timedelta
from html import escape
import json

from cryptography.fernet import InvalidToken
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from app.auth.scopes import CORP_ROLES_SCOPE
from app.config import get_settings, is_primary_telemetry_node, telemetry_collector_base_url
from app.db.models import Character, CorpSetting, Delivery, EsiState, Notification
from app.db.session import SessionLocal
from app.delivery.mentions import (
    MENTION_CHANNEL,
    MENTION_EVERYONE,
    MENTION_HERE,
    MENTION_NONE,
    MENTION_ROLE,
    MENTION_USER,
    mention_form_values,
)
from app.delivery.resolver import choose_destination
from app.esi.client import fetch_character_roles, refresh_access_token
from app.security.csrf import ensure_csrf_session_id, issue_csrf_token
from app.security.crypto import decrypt_refresh_token, encrypt_refresh_token
from app.telemetry.events import (
    get_collector_install_rows,
    get_telemetry_consent_state,
)

router = APIRouter()


MONITORING_SCOPE = "esi-characters.read_notifications.v1"
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


def _csrf_token_for_request(request: Request) -> str:
    session_id = ensure_csrf_session_id(request.session)
    return issue_csrf_token(session_id)


def _render_user_chip(character: dict | None, csrf_token: str | None = None) -> str:
    if not character or not character.get("character_id") or not character.get("character_name"):
        return ""

    character_id = int(character["character_id"])
    character_name = escape(str(character["character_name"]))
    is_admin = bool(character.get("is_admin"))
    monitoring_enabled = bool(character.get("monitoring_enabled"))
    portrait_url = f"https://images.evetech.net/characters/{character_id}/portrait?size=128"
    admin_badge = '<span class="admin-badge">Admin</span>' if is_admin else ""
    monitoring_badge = (
        '<span class="monitoring-badge">Monitoring Enabled</span>'
        if monitoring_enabled
        else '<span class="monitoring-badge off">Monitoring Disabled</span>'
    )
    admin_link = '<a class="admin-link" href="/admin">Admin Dashboard</a>' if is_admin else ""
    deauth_form = ""
    if csrf_token:
        deauth_form = f"""
        <form method="post" action="/auth/deauth" class="deauth-form"
              onsubmit="return confirm('Deauthorize this character and clear local settings? You will be logged out.');">
          <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
          <button type="submit" class="deauth-button">Deauth</button>
        </form>
        """
    admin_row = ""
    if is_admin:
        admin_row = f"""
        <div class="chip-row chip-row-admin">
          {admin_badge}
          {admin_link}
        </div>
        """

    return f"""
    <div class="user-chip">
      <img src="{portrait_url}" alt="{character_name} portrait" />
      <div class="user-chip-body">
        <div class="label">Logged in as</div>
        <div class="name">{character_name}</div>
        <div class="chip-row">
          {monitoring_badge}
          <a class="logout-link" href="/auth/logout">Log Out</a>
          {deauth_form}
        </div>
        {admin_row}
      </div>
    </div>
    """


def _effective_delivery_snapshot(character_row: Character, corp_setting: CorpSetting | None) -> dict:
    settings = get_settings()
    destination = choose_destination(
        character_id=character_row.character_id,
        corporation_id=character_row.corporation_id,
        use_corp_webhook=bool(character_row.use_corp_webhook),
        personal_webhook_url=character_row.personal_webhook_url,
        personal_mention_text=character_row.personal_mention_text,
        corp_webhook_url=corp_setting.webhook_url if corp_setting else None,
        corp_mention_text=corp_setting.mention_text if corp_setting else None,
        default_mention=settings.discord_default_mention,
        dev_fallback_webhook_url=(
            settings.discord_test_webhook_url if settings.env.lower() == "dev" else None
        ),
    )
    if destination is None:
        if settings.eve_mail_fallback_enabled:
            return {
                "method": "EVE Mail Fallback",
                "details": "No Discord webhook selected; alerts are sent to your character mailbox.",
                "kind": "mail",
            }
        return {
            "method": "Not Configured",
            "details": "No webhook selected and mail fallback is disabled.",
            "kind": "none",
        }

    if destination.destination_key.startswith("corp:"):
        mention = destination.mention_text or "none"
        return {
            "method": "Discord (Corp Webhook)",
            "details": f"Corp webhook enabled. Mention: {mention}.",
            "kind": "discord",
        }
    if destination.destination_key.startswith("character:"):
        mention = destination.mention_text or "none"
        return {
            "method": "Discord (Personal Webhook)",
            "details": f"Personal webhook enabled. Mention: {mention}.",
            "kind": "discord",
        }
    if destination.destination_key == "dev:test-webhook":
        mention = destination.mention_text or "none"
        return {
            "method": "Discord (Dev Test Webhook)",
            "details": f"Using DISCORD_TEST_WEBHOOK_URL. Mention: {mention}.",
            "kind": "discord",
        }
    return {
        "method": "Discord",
        "details": "Webhook configured.",
        "kind": "discord",
    }


def _render_home(
    character: dict | None,
    auth_error: str | None,
    home_notice: str | None,
    delivery_snapshot: dict | None,
    csrf_token: str,
    telemetry_prompt: bool,
) -> str:
    action_button = '<a class="sso-button" href="/auth/login">Sign In with EVE Online SSO</a>'
    secondary_action = ""
    delivery_block = ""
    telemetry_prompt_block = ""
    user_block = _render_user_chip(character, csrf_token=csrf_token)
    description_html = "Mercenary Den monitoring and alerting for EVE Online."
    if user_block:
        monitoring_enabled = bool((character or {}).get("monitoring_enabled"))
        if monitoring_enabled:
            action_button = '<span class="signed-in-pill">Monitoring Is Enabled</span>'
            secondary_action = f"""
            <form method="post" action="/auth/monitoring/disable" class="secondary-action">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
              <button type="submit" class="secondary-button">Disable Monitoring</button>
            </form>
            """
            snapshot = delivery_snapshot or {
                "method": "Unknown",
                "details": "Unable to determine delivery settings.",
                "kind": "none",
            }
            kind_class = "ok" if snapshot["kind"] in {"discord", "mail"} else "warn"
            delivery_block = f"""
            <div class="delivery-card {kind_class}">
              <div class="delivery-label">Alert Delivery</div>
              <div class="delivery-method">{escape(str(snapshot["method"]))}</div>
              <div class="delivery-details">{escape(str(snapshot["details"]))}</div>
              <a class="delivery-link" href="/alerts">Configure Alert Delivery</a>
            </div>
            """
        else:
            action_button = (
                '<a class="sso-button" href="/auth/monitoring/start">'
                "Enable Monitoring"
                "</a>"
            )
    else:
        description_html = (
            "Mercenary Den monitoring and alerting for EVE Online.<br />"
            "Authenticate with EVE SSO to enable character-linked workflows."
        )

    error_html = ""
    if auth_error:
        error_html = f'<div class="error">{escape(auth_error)}</div>'
    notice_html = ""
    if home_notice:
        notice_html = f'<div class="notice">{escape(home_notice)}</div>'
    if telemetry_prompt:
        telemetry_prompt_block = f"""
        <div class="telemetry-consent">
          <div class="telemetry-title">Telemetry Consent Required</div>
          <div class="telemetry-text">
            Condottiere uses minimal, anonymous telemetry to help measure project adoption and support
            EVE Partnership progress. We only send this install UUID plus monitored character count,
            never character IDs, corporation IDs, notification text, or tokens.
          </div>
          <form method="post" action="/settings/telemetry/consent" class="telemetry-actions">
            <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
            <button type="submit" name="decision" value="allow" class="telemetry-allow">Allow Telemetry</button>
            <button type="submit" name="decision" value="decline" class="telemetry-decline">Decline</button>
          </form>
        </div>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Condottiere</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #000000;
      --fg: #f5f5f5;
      --muted: #b4b4b4;
      --accent: #2460ff;
      --accent-hover: #1549d6;
      --danger-bg: #2d0d0d;
      --danger-border: #8d2f2f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top, #121212, var(--bg) 55%);
      color: var(--fg);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      position: relative;
    }}
    main {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(2.4rem, 6vw, 4.8rem);
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    p {{
      margin: 0 auto 36px;
      max-width: 760px;
      color: var(--muted);
      font-size: clamp(1rem, 2.2vw, 1.2rem);
      line-height: 1.55;
    }}
    .sso-button {{
      display: inline-block;
      text-decoration: none;
      color: #ffffff;
      background: var(--accent);
      padding: 18px 34px;
      border-radius: 14px;
      font-weight: 700;
      font-size: clamp(1rem, 2.4vw, 1.3rem);
      box-shadow: 0 10px 35px rgba(36, 96, 255, 0.35);
    }}
    .sso-button:hover {{
      background: var(--accent-hover);
    }}
    .footer {{
      position: fixed;
      bottom: 18px;
      left: 0;
      width: 100%;
      text-align: center;
      color: var(--muted);
      font-size: 0.9rem;
      letter-spacing: 0.04em;
    }}
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .user-chip .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .user-chip .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .logout-link {{
      display: inline-block;
      color: #d2dcff;
      text-decoration: none;
      font-size: 0.82rem;
      border: 1px solid #3a4f9a;
      border-radius: 8px;
      padding: 3px 8px;
    }}
    .admin-link {{
      display: inline-block;
      margin-right: 6px;
      color: #b2ffc9;
      text-decoration: none;
      font-size: 0.82rem;
      border: 1px solid #2a8254;
      border-radius: 8px;
      padding: 3px 8px;
    }}
    .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .admin-badge {{
      display: inline-block;
      margin-right: 6px;
      margin-bottom: 5px;
      color: #111111;
      background: #ffd56a;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
    }}
    .monitoring-badge {{
      display: inline-block;
      margin-right: 6px;
      margin-bottom: 5px;
      color: #101010;
      background: #8ef5b6;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
    }}
    .monitoring-badge.off {{
      color: #ffffff;
      background: #8d2f2f;
    }}
    .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .deauth-form {{
      display: inline-block;
      margin-left: 6px;
    }}
    .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      border-radius: 8px;
      padding: 3px 8px;
      font-size: 0.82rem;
      cursor: pointer;
    }}
    .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    .deauth-form {{
      display: inline-block;
      margin-left: 6px;
    }}
    .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      border-radius: 8px;
      padding: 3px 8px;
      font-size: 0.82rem;
      cursor: pointer;
    }}
    .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    .deauth-form {{
      display: inline-block;
      margin-left: 6px;
    }}
    .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      border-radius: 8px;
      padding: 3px 8px;
      font-size: 0.82rem;
      cursor: pointer;
    }}
    .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    .signed-in-pill {{
      display: inline-block;
      color: #d5e0ff;
      background: rgba(28, 53, 131, 0.35);
      border: 1px solid #3550a8;
      border-radius: 999px;
      padding: 10px 16px;
      font-size: 0.95rem;
      font-weight: 600;
    }}
    .secondary-action {{
      margin-top: 12px;
    }}
    .secondary-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      border-radius: 10px;
      padding: 8px 12px;
      font-size: 0.88rem;
      cursor: pointer;
    }}
    .secondary-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    .delivery-card {{
      margin: 14px auto 0;
      max-width: 760px;
      padding: 12px 14px;
      border-radius: 12px;
      text-align: left;
      border: 1px solid #3a3a3a;
      background: #121212;
    }}
    .delivery-card.ok {{
      border-color: #2b7f52;
      background: #0f1f18;
    }}
    .delivery-card.warn {{
      border-color: #8d6b2f;
      background: #2b230f;
    }}
    .delivery-label {{
      color: #adb8d8;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      margin-bottom: 4px;
    }}
    .delivery-method {{
      font-size: 1.03rem;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .delivery-details {{
      color: #bfc7dd;
      font-size: 0.9rem;
    }}
    .delivery-link {{
      margin-top: 8px;
      display: inline-block;
      text-decoration: none;
      color: #d2dcff;
      border: 1px solid #3a4f9a;
      border-radius: 8px;
      padding: 5px 8px;
      font-size: 0.84rem;
    }}
    .delivery-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .error {{
      margin: 0 auto 18px;
      padding: 10px 12px;
      max-width: 760px;
      border: 1px solid var(--danger-border);
      background: var(--danger-bg);
      border-radius: 10px;
      color: #f0c9c9;
      font-size: 0.95rem;
    }}
    .notice {{
      margin: 0 auto 18px;
      padding: 10px 12px;
      max-width: 760px;
      border: 1px solid #2b7f52;
      background: #0f1f18;
      border-radius: 10px;
      color: #c6ffdd;
      font-size: 0.95rem;
    }}
    .telemetry-consent {{
      margin: 16px auto 0;
      max-width: 760px;
      text-align: left;
      border: 1px solid #8d6b2f;
      background: #2b230f;
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .telemetry-title {{
      font-size: 0.95rem;
      font-weight: 700;
      margin-bottom: 6px;
      color: #ffe2a2;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .telemetry-text {{
      color: #f3dfb5;
      font-size: 0.9rem;
      line-height: 1.45;
      margin-bottom: 10px;
    }}
    .telemetry-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .telemetry-actions button {{
      border-radius: 8px;
      padding: 7px 10px;
      font-size: 0.84rem;
      cursor: pointer;
    }}
    .telemetry-allow {{
      border: 1px solid #2b7f52;
      background: #124229;
      color: #d8ffe8;
    }}
    .telemetry-allow:hover {{
      background: #1a5a37;
      color: #ffffff;
    }}
    .telemetry-decline {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
    }}
    .telemetry-decline:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    /* Unified user chip styling across pages */
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
      z-index: 10;
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .user-chip .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .user-chip .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .user-chip .chip-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
    }}
    .user-chip .chip-row-admin {{
      margin-top: 6px;
    }}
    .user-chip .chip-row > * {{
      margin: 0;
      vertical-align: middle;
    }}
    .user-chip .monitoring-badge,
    .user-chip .admin-badge,
    .user-chip .admin-link,
    .user-chip .logout-link,
    .user-chip .deauth-button {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 0.78rem;
      line-height: 1.2;
      text-decoration: none;
      justify-content: center;
      min-height: 24px;
    }}
    .user-chip .monitoring-badge {{
      color: #101010;
      background: #8ef5b6;
      border: 1px solid #4ca870;
      font-weight: 700;
    }}
    .user-chip .monitoring-badge.off {{
      color: #ffffff;
      background: #8d2f2f;
      border: 1px solid #8d2f2f;
    }}
    .user-chip .admin-badge {{
      color: #111111;
      background: #ffd56a;
      border: 1px solid #cfaa48;
      font-weight: 700;
    }}
    .user-chip .admin-link {{
      color: #b2ffc9;
      border: 1px solid #2a8254;
    }}
    .user-chip .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .user-chip .logout-link {{
      color: #d2dcff;
      border: 1px solid #3a4f9a;
    }}
    .user-chip .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .user-chip .deauth-form {{
      display: inline-flex;
      align-items: center;
      margin: 0;
      line-height: 1;
    }}
    .user-chip .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      cursor: pointer;
    }}
    .user-chip .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
  </style>
</head>
<body>
  {user_block}
  <main>
    <section>
      {error_html}
      {notice_html}
      <h1>Condottiere</h1>
      <p>
        {description_html}
      </p>
      {action_button}
      {secondary_action}
      {delivery_block}
      {telemetry_prompt_block}
    </section>
  </main>
  <div class="footer">Made by Eadrom Vintarus</div>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    character = request.session.get("character")
    auth_error = request.session.pop("auth_error", None)
    home_notice = request.session.pop("home_notice", None)
    csrf_token = _csrf_token_for_request(request)
    delivery_snapshot = None
    telemetry_prompt = False
    if character and character.get("character_id"):
        try:
            character_id = int(character["character_id"])
        except (TypeError, ValueError):
            character_id = 0
        if character_id:
            with SessionLocal() as db:
                character_row = db.get(Character, character_id)
                if character_row is not None:
                    corp_setting = db.get(CorpSetting, character_row.corporation_id)
                    delivery_snapshot = _effective_delivery_snapshot(character_row, corp_setting)
                    character["monitoring_enabled"] = bool(character_row.monitoring_enabled)
                    request.session["character"] = character
    if (
        telemetry_collector_base_url()
        and not is_primary_telemetry_node()
        and character
        and bool(character.get("is_admin"))
    ):
        telemetry_prompt = get_telemetry_consent_state() == "undecided"
    return HTMLResponse(
        content=_render_home(
            character=character,
            auth_error=auth_error,
            home_notice=home_notice,
            delivery_snapshot=delivery_snapshot,
            csrf_token=csrf_token,
            telemetry_prompt=telemetry_prompt,
        )
    )


def _yn(value: bool) -> str:
    return "Yes" if value else "No"


def _parse_scopes(scopes_blob: str | None) -> set[str]:
    if not scopes_blob:
        return set()
    return {scope for scope in scopes_blob.split() if scope}


def _fmt_datetime(value) -> str:
    if value is None:
        return "-"
    return escape(str(value))


def _load_allowed_roles_set(roles_blob: str | None) -> set[str]:
    if not roles_blob:
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    try:
        parsed = json.loads(roles_blob)
    except ValueError:
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    if not isinstance(parsed, list):
        return set(DEFAULT_ALLOWED_CORP_ROLES)
    valid = set(MANAGEABLE_CORP_ROLES)
    roles = {str(role).strip() for role in parsed if str(role).strip() in valid}
    return roles or set(DEFAULT_ALLOWED_CORP_ROLES)


def _allowed_roles_display(roles: set[str]) -> str:
    if not roles:
        return ", ".join(sorted(DEFAULT_ALLOWED_CORP_ROLES))
    return ", ".join(sorted(roles))


def _fetch_live_corp_roles_for_ui(character_row: Character) -> tuple[set[str], str | None]:
    scopes = _parse_scopes(character_row.scopes)
    if CORP_ROLES_SCOPE not in scopes:
        return set(), "Corp role scope missing."

    encrypted_refresh = (character_row.refresh_token_encrypted or "").strip()
    if not encrypted_refresh:
        return set(), "Missing refresh token for corp role verification."

    try:
        refresh_token = decrypt_refresh_token(encrypted_refresh)
    except InvalidToken:
        return set(), "Invalid encrypted token; re-auth may be required."

    try:
        token_data = refresh_access_token(refresh_token)
        access_token = str(token_data.get("access_token", "")).strip()
        if not access_token:
            return set(), "Token refresh returned no access token."

        rotated_refresh = str(token_data.get("refresh_token", "")).strip()
        if rotated_refresh:
            character_row.refresh_token_encrypted = encrypt_refresh_token(rotated_refresh)

        roles = set(fetch_character_roles(character_row.character_id, access_token))
    except httpx.HTTPError as exc:
        return set(), f"Corp role verification failed: {exc}"

    return roles, None


def _render_alerts_page(
    *,
    session_character: dict,
    character_row: Character,
    corp_setting: CorpSetting | None,
    allowed_roles: set[str],
    current_corp_roles: set[str],
    corp_scope_granted: bool,
    corp_roles_error: str | None,
    notice: str | None,
    error: str | None,
    csrf_token: str,
) -> str:
    user_block = _render_user_chip(session_character, csrf_token=csrf_token)
    is_admin = bool(session_character.get("is_admin"))
    corp_exists = corp_setting is not None
    manageable_roles = set(MANAGEABLE_CORP_ROLES)
    current_corp_roles = {role for role in current_corp_roles if role in manageable_roles}
    corp_is_director = "Director" in current_corp_roles
    corp_can_edit = bool(corp_is_director or allowed_roles.intersection(current_corp_roles))

    if character_row.use_corp_webhook:
        selected_mode = "corp_webhook"
    elif character_row.personal_webhook_url:
        selected_mode = "personal_webhook"
    elif corp_exists:
        selected_mode = "corp_webhook"
    else:
        selected_mode = "eve_mail"

    personal_webhook = character_row.personal_webhook_url or ""
    personal_mention = character_row.personal_mention_text or ""
    personal_mention_form = mention_form_values(personal_mention)
    corp_webhook = corp_setting.webhook_url if corp_setting else ""
    corp_mention = corp_setting.mention_text if corp_setting else ""
    corp_mention_form = mention_form_values(corp_mention)
    corp_roles = _allowed_roles_display(allowed_roles)
    current_roles_display = ", ".join(sorted(current_corp_roles)) if current_corp_roles else "-"
    corp_updated = _fmt_datetime(corp_setting.updated_at if corp_setting else None)
    effective_snapshot = _effective_delivery_snapshot(character_row, corp_setting)

    notice_html = ""
    if notice:
        notice_html = f'<div class="flash notice">{escape(notice)}</div>'
    error_html = ""
    if error:
        error_html = f'<div class="flash error">{escape(error)}</div>'

    corp_scope_badge = (
        "<span class='pill good'>Corp role scope granted</span>"
        if corp_scope_granted
        else "<span class='pill bad'>Corp role scope missing</span>"
    )
    corp_roles_error_html = (
        f"<div class='flash error'>{escape(corp_roles_error)}</div>" if corp_roles_error else ""
    )
    director_badge = (
        "<span class='pill good'>Director privileges</span>"
        if corp_is_director
        else "<span class='pill bad'>Not Director</span>"
    )
    if corp_is_director:
        roles_editor_items = []
        for role_name in MANAGEABLE_CORP_ROLES:
            checked = "checked" if role_name in allowed_roles else ""
            roles_editor_items.append(
                "<label class='role-item'>"
                f"<input type='checkbox' name='corp_allowed_roles' value='{escape(role_name)}' {checked} />"
                f"{escape(role_name)}"
                "</label>"
            )
        roles_editor_html = (
            "<div>"
            "<label>Allowed Roles (Director can modify)</label>"
            "<div class='roles-grid'>"
            + "".join(roles_editor_items)
            + "</div></div>"
        )
    else:
        roles_editor_html = (
            "<div class='muted'>Allowed roles are managed by Directors only.</div>"
        )

    if not corp_scope_granted:
        corp_management_html = """
      <div class="flash error">Corp role scope is missing. Authorize first to manage corporation webhook settings.</div>
      <div class="button-row">
        <a href="/auth/corp-webhook/start" class="action-link">Authorize Corp Role Scope</a>
      </div>
        """
    elif not corp_can_edit:
        corp_management_html = f"""
      {corp_roles_error_html}
      <div class="flash error">
        Your current corp roles do not allow editing this corporation webhook.
        Needed: {escape(corp_roles)}.
      </div>
      <div class="muted">Your current corp roles: <strong>{escape(current_roles_display)}</strong></div>
      <div class="button-row">
        <a href="/auth/corp-webhook/start" class="action-link">Re-authorize Corp Role Scope</a>
      </div>
        """
    else:
        corp_management_html = f"""
      {corp_roles_error_html}
      <div class="button-row" style="margin-bottom:10px;">
        <a href="/auth/corp-webhook/start" class="action-link">Re-authorize Corp Role Scope</a>
      </div>
      <form method="post" action="/settings/corp/webhook">
        <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
        <div class="row">
          <div>
            <label for="corp_webhook_url">Corporation Webhook URL</label>
            <input id="corp_webhook_url" name="corp_webhook_url" type="url" value="{escape(corp_webhook)}" placeholder="https://discord.com/api/webhooks/..." />
          </div>
          <div>
            <label>Corporation Mention (optional)</label>
            <div class="roles-grid">
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_NONE}" {"checked" if corp_mention_form["mode"] == MENTION_NONE else ""} />
                None
              </label>
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_HERE}" {"checked" if corp_mention_form["mode"] == MENTION_HERE else ""} />
                @here
              </label>
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_EVERYONE}" {"checked" if corp_mention_form["mode"] == MENTION_EVERYONE else ""} />
                @everyone
              </label>
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_USER}" {"checked" if corp_mention_form["mode"] == MENTION_USER else ""} />
                @&lt;userID&gt;
                <input type="text" name="corp_mention_user_id" value="{escape(corp_mention_form["user_id"])}" placeholder="1234567890" />
              </label>
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_ROLE}" {"checked" if corp_mention_form["mode"] == MENTION_ROLE else ""} />
                @&lt;roleID&gt;
                <input type="text" name="corp_mention_role_id" value="{escape(corp_mention_form["role_id"])}" placeholder="1234567890" />
              </label>
              <label class="role-item">
                <input type="radio" name="corp_mention_mode" value="{MENTION_CHANNEL}" {"checked" if corp_mention_form["mode"] == MENTION_CHANNEL else ""} />
                #&lt;channelID&gt;
                <input type="text" name="corp_mention_channel_id" value="{escape(corp_mention_form["channel_id"])}" placeholder="1234567890" />
              </label>
            </div>
          </div>
          {roles_editor_html}
        </div>
        <div class="button-row">
          <button type="submit">Save Corporation Webhook</button>
        </div>
      </form>
      <form method="post" action="/settings/corp/webhook/delete" style="margin-top:8px;">
        <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
        <button type="submit" class="danger">Delete Corporation Webhook</button>
      </form>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Condottiere Alert Delivery</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #080808;
      --panel: #121212;
      --panel-2: #171717;
      --border: #2f2f2f;
      --text: #f2f2f2;
      --muted: #adadad;
      --accent: #2d74ff;
      --good: #2a8254;
      --danger: #8d2f2f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top, #141414, var(--bg) 60%);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
      z-index: 10;
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .user-chip .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .user-chip .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .admin-badge {{
      display: inline-block;
      margin-right: 6px;
      margin-bottom: 5px;
      color: #111111;
      background: #ffd56a;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
    }}
    .monitoring-badge {{
      display: inline-block;
      margin-right: 6px;
      margin-bottom: 5px;
      color: #101010;
      background: #8ef5b6;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
    }}
    .monitoring-badge.off {{
      color: #ffffff;
      background: #8d2f2f;
    }}
    .admin-link, .logout-link {{
      display: inline-block;
      text-decoration: none;
      font-size: 0.82rem;
      border-radius: 8px;
      padding: 3px 8px;
    }}
    .admin-link {{
      margin-right: 6px;
      color: #b2ffc9;
      border: 1px solid #2a8254;
    }}
    .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .logout-link {{
      color: #d2dcff;
      border: 1px solid #3a4f9a;
    }}
    .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .wrap {{
      max-width: 980px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.9rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .muted {{
      margin-bottom: 18px;
      color: var(--muted);
    }}
    .panel {{
      margin-bottom: 14px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
    }}
    .panel h2 {{
      margin: 0 0 10px;
      font-size: 1.05rem;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin-bottom: 10px;
    }}
    label {{
      font-size: 0.86rem;
      color: var(--muted);
      display: block;
      margin-bottom: 4px;
    }}
    input[type="text"], input[type="url"] {{
      width: 100%;
      border: 1px solid #404040;
      background: #0e0e0e;
      color: var(--text);
      border-radius: 8px;
      padding: 9px 10px;
      font-size: 0.93rem;
    }}
    .radio-group {{
      display: grid;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .radio-item {{
      border: 1px solid #323232;
      border-radius: 10px;
      padding: 8px 10px;
      background: var(--panel-2);
    }}
    .radio-item input {{
      margin-right: 6px;
    }}
    button {{
      border: 1px solid #3f57a9;
      background: #19317c;
      color: #e6ecff;
      border-radius: 9px;
      padding: 8px 12px;
      font-size: 0.9rem;
      cursor: pointer;
    }}
    button:hover {{
      background: #29479f;
      color: #ffffff;
    }}
    .button-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .action-link {{
      display: inline-block;
      border: 1px solid #3f57a9;
      background: #19317c;
      color: #e6ecff;
      border-radius: 9px;
      padding: 8px 12px;
      font-size: 0.9rem;
      text-decoration: none;
    }}
    .action-link:hover {{
      background: #29479f;
      color: #ffffff;
    }}
    .danger {{
      border-color: #8a3131;
      background: #391414;
      color: #ffe0e0;
    }}
    .danger:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
    .pill {{
      display: inline-block;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 0.78rem;
      border: 1px solid #404040;
      margin-left: 6px;
    }}
    .pill.good {{
      border-color: #2a8254;
      background: #103321;
      color: #b7ffd2;
    }}
    .pill.bad {{
      border-color: #8d2f2f;
      background: #2a1212;
      color: #ffd0d0;
    }}
    .flash {{
      margin-bottom: 12px;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 0.92rem;
      border: 1px solid var(--border);
    }}
    .flash.notice {{
      border-color: #2b7f52;
      background: #0f1f18;
      color: #c6ffdd;
    }}
    .flash.error {{
      border-color: var(--danger);
      background: #2d0d0d;
      color: #ffd8d8;
    }}
    .snapshot {{
      font-size: 0.92rem;
      color: #d7deef;
      line-height: 1.45;
    }}
    .roles-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 6px;
      margin-top: 8px;
      margin-bottom: 10px;
    }}
    .role-item {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border-radius: 8px;
      border: 1px solid #2f2f2f;
      background: #101010;
      font-size: 0.86rem;
    }}
    .links {{
      margin-top: 14px;
    }}
    .links a {{
      color: #d2dcff;
      text-decoration: none;
      border: 1px solid #3a4f9a;
      border-radius: 8px;
      padding: 6px 9px;
      margin-right: 8px;
      display: inline-block;
      margin-bottom: 8px;
    }}
    .links a:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    /* Unified user chip styling across pages */
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
      z-index: 10;
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .user-chip .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .user-chip .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .user-chip .chip-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
    }}
    .user-chip .chip-row-admin {{
      margin-top: 6px;
    }}
    .user-chip .chip-row > * {{
      margin: 0;
      vertical-align: middle;
    }}
    .user-chip .monitoring-badge,
    .user-chip .admin-badge,
    .user-chip .admin-link,
    .user-chip .logout-link,
    .user-chip .deauth-button {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 0.78rem;
      line-height: 1.2;
      text-decoration: none;
      justify-content: center;
      min-height: 24px;
    }}
    .user-chip .monitoring-badge {{
      color: #101010;
      background: #8ef5b6;
      border: 1px solid #4ca870;
      font-weight: 700;
    }}
    .user-chip .monitoring-badge.off {{
      color: #ffffff;
      background: #8d2f2f;
      border: 1px solid #8d2f2f;
    }}
    .user-chip .admin-badge {{
      color: #111111;
      background: #ffd56a;
      border: 1px solid #cfaa48;
      font-weight: 700;
    }}
    .user-chip .admin-link {{
      color: #b2ffc9;
      border: 1px solid #2a8254;
    }}
    .user-chip .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .user-chip .logout-link {{
      color: #d2dcff;
      border: 1px solid #3a4f9a;
    }}
    .user-chip .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .user-chip .deauth-form {{
      display: inline-flex;
      align-items: center;
      margin: 0;
      line-height: 1;
    }}
    .user-chip .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      cursor: pointer;
    }}
    .user-chip .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
  </style>
</head>
<body>
  {user_block}
  <div class="wrap">
    <h1>Alert Delivery Settings</h1>
    <div class="muted">Configure how Condottiere sends Mercenary Den alerts for your character.</div>
    {notice_html}
    {error_html}
    <div class="panel">
      <h2>Current Effective Delivery</h2>
      <div class="snapshot"><strong>{escape(str(effective_snapshot["method"]))}</strong><br />{escape(str(effective_snapshot["details"]))}</div>
    </div>

    <div class="panel">
      <h2>Personal Delivery Preference</h2>
      <form method="post" action="/settings/me/delivery">
        <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
        <div class="radio-group">
          <label class="radio-item">
            <input type="radio" name="delivery_mode" value="eve_mail" {"checked" if selected_mode == "eve_mail" else ""} />
            Use EVE Mail fallback (no Discord webhook)
          </label>
          <label class="radio-item">
            <input type="radio" name="delivery_mode" value="personal_webhook" {"checked" if selected_mode == "personal_webhook" else ""} />
            Use personal Discord webhook
          </label>
          <label class="radio-item">
            <input type="radio" name="delivery_mode" value="corp_webhook" {"checked" if selected_mode == "corp_webhook" else ""} {"disabled" if not corp_exists else ""} />
            Use corporation webhook {"<span class='pill good'>Configured</span>" if corp_exists else "<span class='pill bad'>Not Configured</span>"}
          </label>
        </div>
        <div class="row">
          <div>
            <label for="personal_webhook_url">Personal Discord Webhook URL</label>
            <input id="personal_webhook_url" name="personal_webhook_url" type="url" value="{escape(personal_webhook)}" placeholder="https://discord.com/api/webhooks/..." />
          </div>
          <div>
            <label>Personal Mention (optional)</label>
            <div class="roles-grid">
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_NONE}" {"checked" if personal_mention_form["mode"] == MENTION_NONE else ""} />
                None
              </label>
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_HERE}" {"checked" if personal_mention_form["mode"] == MENTION_HERE else ""} />
                @here
              </label>
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_EVERYONE}" {"checked" if personal_mention_form["mode"] == MENTION_EVERYONE else ""} />
                @everyone
              </label>
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_USER}" {"checked" if personal_mention_form["mode"] == MENTION_USER else ""} />
                @&lt;userID&gt;
                <input type="text" name="personal_mention_user_id" value="{escape(personal_mention_form["user_id"])}" placeholder="1234567890" />
              </label>
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_ROLE}" {"checked" if personal_mention_form["mode"] == MENTION_ROLE else ""} />
                @&lt;roleID&gt;
                <input type="text" name="personal_mention_role_id" value="{escape(personal_mention_form["role_id"])}" placeholder="1234567890" />
              </label>
              <label class="role-item">
                <input type="radio" name="personal_mention_mode" value="{MENTION_CHANNEL}" {"checked" if personal_mention_form["mode"] == MENTION_CHANNEL else ""} />
                #&lt;channelID&gt;
                <input type="text" name="personal_mention_channel_id" value="{escape(personal_mention_form["channel_id"])}" placeholder="1234567890" />
              </label>
            </div>
          </div>
        </div>
        <button type="submit">Save Personal Delivery Settings</button>
      </form>
    </div>

    <div class="panel">
      <h2>Corporation Webhook</h2>
      <div class="muted">
        Scope status:
        {corp_scope_badge}.
        {director_badge}
        Required roles: <strong>{escape(corp_roles)}</strong>.
        Last updated: <strong>{corp_updated}</strong>.
        Your current corp roles: <strong>{escape(current_roles_display)}</strong>.
      </div>
      {corp_management_html}
    </div>

    <div class="links">
      <a href="/">Back to Home</a>
      {'<a href="/admin">Admin Dashboard</a>' if is_admin else ''}
    </div>
  </div>
  <script>
    (function () {{
      function wireMentionAutoSelect(prefix) {{
        const mappings = [
          {{ inputName: prefix + "_mention_user_id", mode: "user" }},
          {{ inputName: prefix + "_mention_role_id", mode: "role" }},
          {{ inputName: prefix + "_mention_channel_id", mode: "channel" }},
        ];
        const radioName = prefix + "_mention_mode";

        mappings.forEach(function (mapping) {{
          const input = document.querySelector('input[name="' + mapping.inputName + '"]');
          if (!input) return;
          input.addEventListener("input", function () {{
            if (!input.value || !input.value.trim()) return;
            const target = document.querySelector(
              'input[name="' + radioName + '"][value="' + mapping.mode + '"]'
            );
            if (target) target.checked = true;
          }});
        }});
      }}

      wireMentionAutoSelect("personal");
      wireMentionAutoSelect("corp");
    }})();
  </script>
</body>
</html>
"""


def _render_admin(
    character: dict,
    characters: list[Character],
    esi_state_by_character_id: dict[int, EsiState],
    queue_stats: dict[str, int],
    telemetry_rows: list[dict] | None,
    csrf_token: str,
) -> str:
    user_block = _render_user_chip(character, csrf_token=csrf_token)
    rows = []
    monitoring_scope_count = 0
    monitoring_enabled_count = 0
    monitoring_ready_count = 0
    for row in characters:
        esi_state = esi_state_by_character_id.get(row.character_id)
        scopes = _parse_scopes(row.scopes)
        has_monitoring_scope = MONITORING_SCOPE in scopes
        monitoring_enabled = bool(row.monitoring_enabled)
        has_refresh_token = bool(row.refresh_token_encrypted)
        has_etag = bool(esi_state and esi_state.notif_etag)
        last_polled_at = _fmt_datetime(esi_state.last_polled_at if esi_state else None)
        notif_expires_at = _fmt_datetime(esi_state.notif_expires_at if esi_state else None)
        last_error = escape(esi_state.last_error) if esi_state and esi_state.last_error else "-"
        if has_monitoring_scope:
            monitoring_scope_count += 1
        if monitoring_enabled:
            monitoring_enabled_count += 1
        if monitoring_enabled and has_refresh_token:
            monitoring_ready_count += 1
        rows.append(
            f"""
            <tr>
              <td>{escape(row.character_name)}</td>
              <td>{row.character_id}</td>
              <td>{row.corporation_id}</td>
              <td>{_yn(bool(row.is_active))}</td>
              <td>{_yn(monitoring_enabled)}</td>
              <td>{_yn(has_monitoring_scope)}</td>
              <td>{_yn(has_refresh_token)}</td>
              <td>{_yn(has_etag)}</td>
              <td>{_yn(bool(row.personal_webhook_url))}</td>
              <td>{_yn(bool(row.use_corp_webhook))}</td>
              <td>{_fmt_datetime(row.updated_at)}</td>
              <td>{last_polled_at}</td>
              <td>{notif_expires_at}</td>
              <td>{last_error}</td>
            </tr>
            """
        )

    table_body = "".join(rows) if rows else '<tr><td colspan="14">No registered characters yet.</td></tr>'
    total = len(characters)
    active_count = sum(1 for row in characters if row.is_active)
    telemetry_html = ""
    if telemetry_rows is not None:
        rows = telemetry_rows or []
        telemetry_table_rows = []
        for row in rows:
            last_seen = _fmt_datetime(row.get("last_received_at"))
            latest_count = int(row.get("latest_monitored_character_count", 0))
            avg_30d = int(round(float(row.get("avg_30d_monitored_character_count", 0.0))))
            samples_30d = int(row.get("samples_30d", 0))
            telemetry_table_rows.append(
                f"""
                <tr>
                  <td>{escape(str(row.get("install_id", "-")))}</td>
                  <td>{last_seen}</td>
                  <td>{latest_count}</td>
                  <td>{avg_30d:.2f}</td>
                  <td>{samples_30d}</td>
                </tr>
                """
            )
        telemetry_table_body = (
            "".join(telemetry_table_rows)
            if telemetry_table_rows
            else '<tr><td colspan="5">No telemetry heartbeats received yet.</td></tr>'
        )
        telemetry_html = f"""
    <div class="section-title">Telemetry (Opt-in)</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Install UUID</th>
            <th>Last Received</th>
            <th>Latest Count</th>
            <th>30d Avg</th>
            <th>30d Samples</th>
          </tr>
        </thead>
        <tbody>
          {telemetry_table_body}
        </tbody>
      </table>
    </div>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Condottiere Admin</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #070707;
      --panel: #121212;
      --panel-2: #181818;
      --border: #2f2f2f;
      --fg: #f2f2f2;
      --muted: #a8a8a8;
      --good: #7fe6a6;
      --warn: #ffe08b;
      --accent: #2e74ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top, #111111, var(--bg) 60%);
      color: var(--fg);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .muted {{
      color: var(--muted);
      margin-bottom: 22px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .section-title {{
      margin: 12px 0 10px;
      font-size: 0.88rem;
      letter-spacing: 0.08em;
      color: var(--muted);
      text-transform: uppercase;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 0.8rem;
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .stat-value {{
      font-size: 1.25rem;
      font-weight: 700;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--panel);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }}
    th {{
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      background: var(--panel-2);
      position: sticky;
      top: 0;
    }}
    tr:hover td {{
      background: #1a1a1a;
    }}
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
      z-index: 10;
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .admin-badge {{
      display: inline-block;
      margin-right: 6px;
      margin-bottom: 5px;
      color: #111111;
      background: #ffd56a;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
    }}
    .admin-link, .logout-link {{
      display: inline-block;
      text-decoration: none;
      font-size: 0.82rem;
      border-radius: 8px;
      padding: 3px 8px;
    }}
    .admin-link {{
      margin-right: 6px;
      color: #b2ffc9;
      border: 1px solid #2a8254;
    }}
    .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .logout-link {{
      color: #d2dcff;
      border: 1px solid #3a4f9a;
    }}
    .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .home-link {{
      display: inline-block;
      margin-top: 8px;
      text-decoration: none;
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 7px 10px;
    }}
    .home-link:hover {{
      background: #1d1d1d;
    }}
    /* Unified user chip styling across pages */
    .user-chip {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: rgba(22, 22, 22, 0.95);
      border: 1px solid #2e2e2e;
      border-radius: 12px;
      backdrop-filter: blur(2px);
      z-index: 10;
    }}
    .user-chip img {{
      width: 52px;
      height: 52px;
      border-radius: 10px;
      border: 1px solid #3a3a3a;
      object-fit: cover;
    }}
    .user-chip .label {{
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 2px;
    }}
    .user-chip .name {{
      font-weight: 700;
      font-size: 0.94rem;
      margin-bottom: 4px;
    }}
    .user-chip .chip-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
    }}
    .user-chip .chip-row-admin {{
      margin-top: 6px;
    }}
    .user-chip .chip-row > * {{
      margin: 0;
      vertical-align: middle;
    }}
    .user-chip .monitoring-badge,
    .user-chip .admin-badge,
    .user-chip .admin-link,
    .user-chip .logout-link,
    .user-chip .deauth-button {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 0.78rem;
      line-height: 1.2;
      text-decoration: none;
      justify-content: center;
      min-height: 24px;
    }}
    .user-chip .monitoring-badge {{
      color: #101010;
      background: #8ef5b6;
      border: 1px solid #4ca870;
      font-weight: 700;
    }}
    .user-chip .monitoring-badge.off {{
      color: #ffffff;
      background: #8d2f2f;
      border: 1px solid #8d2f2f;
    }}
    .user-chip .admin-badge {{
      color: #111111;
      background: #ffd56a;
      border: 1px solid #cfaa48;
      font-weight: 700;
    }}
    .user-chip .admin-link {{
      color: #b2ffc9;
      border: 1px solid #2a8254;
    }}
    .user-chip .admin-link:hover {{
      background: #1f4d35;
      color: #ffffff;
    }}
    .user-chip .logout-link {{
      color: #d2dcff;
      border: 1px solid #3a4f9a;
    }}
    .user-chip .logout-link:hover {{
      background: #1a2750;
      color: #ffffff;
    }}
    .user-chip .deauth-form {{
      display: inline-flex;
      align-items: center;
      margin: 0;
      line-height: 1;
    }}
    .user-chip .deauth-button {{
      border: 1px solid #8a3131;
      background: #391414;
      color: #ffe0e0;
      cursor: pointer;
    }}
    .user-chip .deauth-button:hover {{
      background: #5b1f1f;
      color: #ffffff;
    }}
  </style>
</head>
<body>
  {user_block}
  <div class="wrap">
    <h1>Admin Dashboard</h1>
    <div class="muted">Registered characters and monitoring readiness.</div>
    <div class="section-title">Notification Queue</div>
    <div class="stats">
      <div class="stat">
        <div class="stat-label">Pending</div>
        <div class="stat-value">{queue_stats["pending"]}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Sent Total</div>
        <div class="stat-value">{queue_stats["sent_total"]}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Sent (48h)</div>
        <div class="stat-value">{queue_stats["sent_48h"]}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Failed</div>
        <div class="stat-value">{queue_stats["failed"]}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Relevant Notifs</div>
        <div class="stat-value">{queue_stats["notif_total"]}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Relevant Notifs (48h)</div>
        <div class="stat-value">{queue_stats["notif_48h"]}</div>
      </div>
    </div>
    {telemetry_html}
    <div class="section-title">Characters</div>
    <div class="stats">
      <div class="stat">
        <div class="stat-label">Total Characters</div>
        <div class="stat-value">{total}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Active Characters</div>
        <div class="stat-value">{active_count}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Monitoring Enabled</div>
        <div class="stat-value">{monitoring_enabled_count}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Monitoring Ready</div>
        <div class="stat-value">{monitoring_ready_count}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Monitoring Scope Granted</div>
        <div class="stat-value">{monitoring_scope_count}</div>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Character</th>
            <th>Character ID</th>
            <th>Corp ID</th>
            <th>Active</th>
            <th>Monitoring Enabled</th>
            <th>Monitoring Scope</th>
            <th>Has Refresh Token</th>
            <th>Has ETag</th>
            <th>Personal Webhook</th>
            <th>Use Corp Webhook</th>
            <th>Updated</th>
            <th>Last Polled</th>
            <th>Cache Expires</th>
            <th>Last Poll Error</th>
          </tr>
        </thead>
        <tbody>
          {table_body}
        </tbody>
      </table>
    </div>
    <a class="home-link" href="/">Back to Home</a>
  </div>
</body>
</html>
"""


@router.get("/alerts", response_class=HTMLResponse)
def alerts_settings_page(
    request: Request,
    notice: str | None = None,
    error: str | None = None,
):
    csrf_token = _csrf_token_for_request(request)
    session_character = request.session.get("character")
    if not session_character or not session_character.get("character_id"):
        request.session["auth_error"] = "Please log in before configuring alerts."
        return RedirectResponse(url="/", status_code=302)

    try:
        character_id = int(session_character["character_id"])
    except (TypeError, ValueError):
        request.session["auth_error"] = "Invalid session character state. Please log in again."
        return RedirectResponse(url="/", status_code=302)

    with SessionLocal() as db:
        character_row = db.get(Character, character_id)
        if character_row is None:
            request.session["auth_error"] = "Character record not found. Please log in again."
            return RedirectResponse(url="/", status_code=302)
        corp_setting = db.get(CorpSetting, character_row.corporation_id)
        allowed_roles = _load_allowed_roles_set(corp_setting.allowed_roles if corp_setting else None)
        current_corp_roles, corp_roles_error = _fetch_live_corp_roles_for_ui(character_row)
        corp_scope_granted = CORP_ROLES_SCOPE in _parse_scopes(character_row.scopes)
        if character_row.monitoring_enabled != bool(session_character.get("monitoring_enabled")):
            session_character["monitoring_enabled"] = bool(character_row.monitoring_enabled)
            request.session["character"] = session_character
        db.commit()
        return HTMLResponse(
            content=_render_alerts_page(
                session_character=session_character,
                character_row=character_row,
                corp_setting=corp_setting,
                allowed_roles=allowed_roles,
                current_corp_roles=current_corp_roles,
                corp_scope_granted=corp_scope_granted,
                corp_roles_error=corp_roles_error,
                notice=notice,
                error=error,
                csrf_token=csrf_token,
            )
        )


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    character = request.session.get("character")
    if not character or not character.get("character_id"):
        request.session["auth_error"] = "Please log in before accessing the admin page."
        return RedirectResponse(url="/", status_code=302)
    if not character.get("is_admin"):
        return HTMLResponse(
            content="<h1>403 Forbidden</h1><p>Admin access required.</p>",
            status_code=403,
        )

    csrf_token = _csrf_token_for_request(request)
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff_48h = now - timedelta(hours=48)

    telemetry_summary = None
    telemetry_rows = None
    with SessionLocal() as db:
        characters = db.execute(select(Character).order_by(Character.updated_at.desc())).scalars().all()
        esi_states = db.execute(select(EsiState)).scalars().all()
        queue_stats = {
            "pending": int(
                db.execute(
                    select(func.count())
                    .select_from(Delivery)
                    .where(Delivery.status == "pending")
                ).scalar_one()
            ),
            "sent_total": int(
                db.execute(
                    select(func.count())
                    .select_from(Delivery)
                    .where(Delivery.status == "sent")
                ).scalar_one()
            ),
            "sent_48h": int(
                db.execute(
                    select(func.count())
                    .select_from(Delivery)
                    .where(Delivery.status == "sent", Delivery.updated_at >= cutoff_48h)
                ).scalar_one()
            ),
            "failed": int(
                db.execute(
                    select(func.count())
                    .select_from(Delivery)
                    .where(Delivery.status == "failed")
                ).scalar_one()
            ),
            "notif_total": int(db.execute(select(func.count()).select_from(Notification)).scalar_one()),
            "notif_48h": int(
                db.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.timestamp >= cutoff_48h)
                ).scalar_one()
            ),
        }
    if is_primary_telemetry_node():
        telemetry_rows = get_collector_install_rows(window_days=30)
    esi_state_by_character_id = {row.character_id: row for row in esi_states}
    return HTMLResponse(
        content=_render_admin(
            character=character,
            characters=characters,
            esi_state_by_character_id=esi_state_by_character_id,
            queue_stats=queue_stats,
            telemetry_rows=telemetry_rows,
            csrf_token=csrf_token,
        )
    )
