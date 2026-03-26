"""Character store behavior tests."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Character
from app.db.base import Base
from app.db.models import CorpSetting
from app.services.character_store import disable_character_monitoring, upsert_character_from_identity


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def test_new_character_defaults_to_corp_webhook_when_corp_setting_exists():
    with _session() as db:
        db.add(
            CorpSetting(
                corporation_id=9001,
                webhook_url="https://discord.com/api/webhooks/corp",
                mention_text="",
                allowed_roles='["Director"]',
                updated_by_character_id=42,
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db.commit()

        character = upsert_character_from_identity(
            db,
            character_id=111,
            character_name="Pilot One",
            corporation_id=9001,
            scopes=["publicData"],
            enable_monitoring=True,
            refresh_token="refresh-token",
        )

        assert character.use_corp_webhook is True
        assert character.monitoring_enabled_at is not None


def test_new_character_defaults_to_mail_when_corp_setting_missing():
    with _session() as db:
        character = upsert_character_from_identity(
            db,
            character_id=222,
            character_name="Pilot Two",
            corporation_id=9002,
            scopes=["publicData"],
            enable_monitoring=True,
            refresh_token="refresh-token",
        )

        assert character.use_corp_webhook is False
        assert character.monitoring_enabled_at is not None


def test_reenable_monitoring_sets_new_monitoring_enabled_at():
    with _session() as db:
        original = upsert_character_from_identity(
            db,
            character_id=333,
            character_name="Pilot Three",
            corporation_id=9003,
            scopes=["publicData"],
            enable_monitoring=True,
            refresh_token="refresh-token",
        )
        first_enabled_at = original.monitoring_enabled_at
        assert first_enabled_at is not None

        disabled = disable_character_monitoring(db, character_id=333)
        assert disabled is True
        row = db.get(Character, 333)
        assert row is not None
        assert row.monitoring_enabled is False
        assert row.monitoring_enabled_at is None

        reenabled = upsert_character_from_identity(
            db,
            character_id=333,
            character_name="Pilot Three",
            corporation_id=9003,
            scopes=["publicData"],
            enable_monitoring=True,
            refresh_token="refresh-token",
        )
        assert reenabled.monitoring_enabled_at is not None
        assert reenabled.monitoring_enabled_at >= first_enabled_at
