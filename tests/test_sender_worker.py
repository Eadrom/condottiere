from datetime import UTC, datetime, timedelta

from app.db.models import Character, Notification
from app.services.delivery_policy import notification_is_stale, notification_predates_monitoring_window


def test_notification_is_stale_for_old_alerts():
    now = datetime.now(UTC).replace(tzinfo=None)
    notification = Notification(
        id=1,
        character_id=1,
        notification_id=123,
        type="MercenaryDenAttacked",
        timestamp=now - timedelta(days=120),
        raw_text="",
    )

    assert notification_is_stale(notification, now=now) is True


def test_notification_is_not_stale_for_recent_alerts():
    now = datetime.now(UTC).replace(tzinfo=None)
    notification = Notification(
        id=1,
        character_id=1,
        notification_id=123,
        type="MercenaryDenAttacked",
        timestamp=now - timedelta(minutes=30),
        raw_text="",
    )

    assert notification_is_stale(notification, now=now) is False


class _ProdSettings:
    env = "prod"


def test_notification_predates_monitoring_window_when_before_enable_cutoff():
    now = datetime.now(UTC).replace(tzinfo=None)
    character = Character(
        character_id=1,
        character_name="Tester",
        corporation_id=99,
        refresh_token_encrypted=None,
        scopes="",
        monitoring_enabled=True,
        monitoring_enabled_at=now,
        personal_webhook_url=None,
        personal_mention_text="",
        use_corp_webhook=False,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    notification = Notification(
        id=1,
        character_id=1,
        notification_id=123,
        type="MercenaryDenAttacked",
        timestamp=now - timedelta(minutes=11),
        raw_text="",
    )

    assert (
        notification_predates_monitoring_window(
            notification,
            character=character,
            settings=_ProdSettings(),
        )
        is True
    )
