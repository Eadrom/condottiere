"""Database engine/session scaffold."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.base import Base


_settings = get_settings()

_connect_args = {}
_engine_kwargs = {"future": True}
if _settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
else:
    # Recover cleanly from restarted/terminated Postgres connections.
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    **_engine_kwargs,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_database() -> None:
    """Initialize local dev database helpers.

    Production and non-dev schema changes are handled via Alembic migrations.
    """
    # Import models so SQLAlchemy has table metadata available.
    from app.db import models  # noqa: F401

    if _settings.env.lower() == "dev" and _settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
        _ensure_local_schema_compatibility()
        _maybe_reset_dev_notification_history()


def _ensure_local_schema_compatibility() -> None:
    """Apply lightweight local schema upgrades for SQLite dev environments."""
    if not _settings.database_url.startswith("sqlite"):
        return

    with engine.begin() as conn:
        table_info = conn.exec_driver_sql("PRAGMA table_info(characters)").fetchall()
        existing_columns = {row[1] for row in table_info}
        if "monitoring_enabled" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE characters "
                "ADD COLUMN monitoring_enabled BOOLEAN NOT NULL DEFAULT 0"
            )
        if "personal_mention_text" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE characters "
                "ADD COLUMN personal_mention_text VARCHAR(255) NOT NULL DEFAULT ''"
            )


def _maybe_reset_dev_notification_history() -> None:
    """In dev, clear pull history so end-to-end notification tests can rerun easily."""
    if _settings.env.lower() != "dev":
        return

    from sqlalchemy import delete

    from app.db import models

    with engine.begin() as conn:
        conn.execute(delete(models.Delivery))
        conn.execute(delete(models.Notification))
        conn.execute(delete(models.EsiState))


def get_db_session():
    """Yield DB session for request or task.

    Pseudocode:
    1. Create session
    2. Yield to caller
    3. Commit/rollback based on outcome
    4. Close session
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
