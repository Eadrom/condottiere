"""Core ORM schema scaffold from Design.md section 8."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Character(Base):
    __tablename__ = "characters"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    character_name: Mapped[str] = mapped_column(String(255))
    corporation_id: Mapped[int] = mapped_column(BigInteger)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    personal_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    personal_mention_text: Mapped[str] = mapped_column(String(255), default="")
    use_corp_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class CorpSetting(Base):
    __tablename__ = "corp_settings"

    corporation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    webhook_url: Mapped[str] = mapped_column(Text)
    mention_text: Mapped[str] = mapped_column(String(255), default="")
    allowed_roles: Mapped[str] = mapped_column(Text, default='["Director"]')
    updated_by_character_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "notification_id",
            name="uq_notifications_character_notif",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    character_id: Mapped[int] = mapped_column(BigInteger)
    notification_id: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    raw_text: Mapped[str] = mapped_column(Text)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "notification_id",
            "destination_key",
            name="uq_deliveries_character_notif_destination",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    character_id: Mapped[int] = mapped_column(BigInteger)
    notification_id: Mapped[int] = mapped_column(BigInteger)
    destination_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class EsiState(Base):
    __tablename__ = "esi_state"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    notif_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notif_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
