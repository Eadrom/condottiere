"""Application settings loader."""

from dataclasses import dataclass
from functools import lru_cache
import os
from urllib.parse import urlparse

EVE_DEFAULT_SCOPES = ("publicData",)
EVE_AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
EVE_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
EVE_VERIFY_URL = "https://login.eveonline.com/oauth/verify"
EVE_ESI_BASE_URL = "https://esi.evetech.net/latest"
EVE_ESI_DATASOURCE = "tranquility"

# Telemetry is intentionally code-level (not env-configurable) to reduce spoofing.
# Project owner can change this constant in source for primary collector migration.
TELEMETRY_COLLECTOR_BASE_URL = "https://condottiere.example.com"
TELEMETRY_EMIT_INTERVAL_SECONDS = 86400


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _parse_int_list(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    parsed = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(int(part))
        except ValueError:
            continue
    return tuple(parsed)


def _parse_optional_text(value: str | None) -> str:
    raw = (value or "").strip()
    if raw in {"TODO", "UPDATE_ME", "CHANGE_ME"}:
        return ""
    return raw


def _normalized_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


@dataclass(frozen=True)
class Settings:
    # Runtime and environment
    env: str
    database_url: str

    # OAuth / SSO
    eve_client_id: str
    eve_client_secret: str
    eve_redirect_base: str
    eve_default_scopes: tuple[str, ...]
    eve_authorize_url: str
    eve_token_url: str
    eve_verify_url: str
    eve_esi_base_url: str
    eve_esi_datasource: str
    eve_user_agent: str

    # Security
    session_secret: str
    csrf_secret: str
    fernet_key: str
    admin_character_ids: tuple[int, ...]

    # Polling and sending
    discord_default_mention: str
    discord_test_webhook_url: str
    discord_min_seconds_per_destination: float
    eve_mail_fallback_enabled: bool
    eve_mail_subject_prefix: str

    # Optional hidden telemetry collector gate
    telemetry_primary_node: bool

@lru_cache
def get_settings() -> Settings:
    return Settings(
        env=os.getenv("ENV", "dev"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./condottiere.db"),
        eve_client_id=os.getenv("EVE_CLIENT_ID", ""),
        eve_client_secret=os.getenv("EVE_CLIENT_SECRET", ""),
        eve_redirect_base=os.getenv("EVE_REDIRECT_BASE", "http://localhost:8000"),
        eve_default_scopes=EVE_DEFAULT_SCOPES,
        eve_authorize_url=EVE_AUTHORIZE_URL,
        eve_token_url=EVE_TOKEN_URL,
        eve_verify_url=EVE_VERIFY_URL,
        eve_esi_base_url=EVE_ESI_BASE_URL,
        eve_esi_datasource=EVE_ESI_DATASOURCE,
        eve_user_agent=os.getenv(
            "EVE_USER_AGENT", "condottiere/1.0 (maintainer: Eadrom Vintarus)"
        ),
        session_secret=os.getenv("SESSION_SECRET", "dev-session-secret-change-me"),
        csrf_secret=os.getenv("CSRF_SECRET", "dev-csrf-secret-change-me"),
        fernet_key=os.getenv("FERNET_KEY", "UPDATE_ME"),
        admin_character_ids=_parse_int_list(os.getenv("ADMIN_CHARACTER_IDS")),
        discord_default_mention=os.getenv("DISCORD_DEFAULT_MENTION", "").strip(),
        discord_test_webhook_url=_parse_optional_text(
            os.getenv("DISCORD_TEST_WEBHOOK_URL", "")
        ),
        discord_min_seconds_per_destination=_parse_float(
            os.getenv("DISCORD_MIN_SECONDS_PER_DESTINATION"), 1.0
        ),
        eve_mail_fallback_enabled=_parse_bool(
            os.getenv("EVE_MAIL_FALLBACK_ENABLED"), True
        ),
        eve_mail_subject_prefix=os.getenv(
            "EVE_MAIL_SUBJECT_PREFIX", "Condottiere Alert"
        ).strip()
        or "Condottiere Alert",
        telemetry_primary_node=_parse_bool(os.getenv("TELEMETRY_PRIMARY_NODE"), False),
    )


def telemetry_collector_base_url() -> str:
    """Return normalized hardcoded telemetry collector base URL."""
    return _normalized_base_url(TELEMETRY_COLLECTOR_BASE_URL)


def is_primary_telemetry_node() -> bool:
    """Primary collector ingest is enabled only when explicitly toggled on."""
    return bool(get_settings().telemetry_primary_node)
