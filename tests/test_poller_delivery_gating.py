from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Character, CorpSetting
from app.services.delivery_policy import has_delivery_channel


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _character(**overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    defaults = {
        "character_id": 1,
        "character_name": "Tester",
        "corporation_id": 99,
        "refresh_token_encrypted": None,
        "scopes": "",
        "monitoring_enabled": True,
        "monitoring_enabled_at": now,
        "personal_webhook_url": None,
        "personal_mention_text": "",
        "use_corp_webhook": False,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Character(**defaults)


class _Settings:
    env = "prod"
    discord_test_webhook_url = ""
    eve_mail_fallback_enabled = True


def test_has_delivery_channel_false_when_no_webhook_and_no_mail_scope():
    db = _session()
    character = _character(refresh_token_encrypted="encrypted-refresh")
    db.add(character)
    db.commit()

    assert has_delivery_channel(db, character=character, settings=_Settings()) is False


def test_has_delivery_channel_true_for_existing_corp_webhook():
    db = _session()
    character = _character(use_corp_webhook=True)
    db.add(character)
    db.add(
        CorpSetting(
            corporation_id=99,
            webhook_url="https://discord.com/api/webhooks/corp",
            mention_text="@here",
            allowed_roles='["Director"]',
            updated_by_character_id=1,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()

    assert has_delivery_channel(db, character=character, settings=_Settings()) is True


def test_has_delivery_channel_true_for_mail_fallback_scope():
    db = _session()
    character = _character(
        refresh_token_encrypted="encrypted-refresh",
        scopes="esi-characters.read_notifications.v1 esi-mail.send_mail.v1",
    )
    db.add(character)
    db.commit()

    assert has_delivery_channel(db, character=character, settings=_Settings()) is True
