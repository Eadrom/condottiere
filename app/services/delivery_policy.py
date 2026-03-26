from datetime import datetime, timedelta

from app.auth.scopes import MAIL_SEND_SCOPE
from app.db.models import Character, CorpSetting, Notification

INITIAL_PROD_ALERT_LOOKBACK_MINUTES = 10
MAX_DELIVERY_AGE_HOURS = 24


def parse_scopes(scopes_blob: str | None) -> set[str]:
    if not scopes_blob:
        return set()
    return {scope for scope in scopes_blob.split() if scope}


def has_delivery_channel(db, *, character: Character, settings) -> bool:
    if character.use_corp_webhook:
        corp_setting = db.get(CorpSetting, character.corporation_id)
        if corp_setting is not None and (corp_setting.webhook_url or "").strip():
            return True

    if (character.personal_webhook_url or "").strip():
        return True

    if settings.env.lower() == "dev" and (settings.discord_test_webhook_url or "").strip():
        return True

    return bool(
        settings.eve_mail_fallback_enabled
        and (character.refresh_token_encrypted or "").strip()
        and MAIL_SEND_SCOPE in parse_scopes(character.scopes)
    )


def monitoring_enable_cutoff(character: Character, *, settings) -> datetime | None:
    if settings.env.lower() != "prod":
        return None
    if character.monitoring_enabled_at is None:
        return None
    return character.monitoring_enabled_at - timedelta(
        minutes=INITIAL_PROD_ALERT_LOOKBACK_MINUTES
    )


def notification_predates_monitoring_window(
    notification: Notification,
    *,
    character: Character,
    settings,
) -> bool:
    cutoff = monitoring_enable_cutoff(character, settings=settings)
    if cutoff is None:
        return False
    return notification.timestamp < cutoff


def notification_is_stale(
    notification: Notification,
    *,
    now: datetime,
    max_age_hours: int = MAX_DELIVERY_AGE_HOURS,
) -> bool:
    return notification.timestamp < now - timedelta(hours=max(max_age_hours, 1))
