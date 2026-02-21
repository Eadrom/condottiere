"""Initial Condottiere schema

Revision ID: 20260221_0001
Revises:
Create Date: 2026-02-21 22:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260221_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "characters",
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("character_name", sa.String(length=255), nullable=False),
        sa.Column("corporation_id", sa.Integer(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("personal_webhook_url", sa.Text(), nullable=True),
        sa.Column("personal_mention_text", sa.String(length=255), nullable=False),
        sa.Column("use_corp_webhook", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("character_id"),
    )

    op.create_table(
        "corp_settings",
        sa.Column("corporation_id", sa.Integer(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("mention_text", sa.String(length=255), nullable=False),
        sa.Column("allowed_roles", sa.Text(), nullable=False),
        sa.Column("updated_by_character_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("corporation_id"),
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("destination_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id",
            "notification_id",
            "destination_key",
            name="uq_deliveries_character_notif_destination",
        ),
    )

    op.create_table(
        "esi_state",
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("notif_etag", sa.String(length=255), nullable=True),
        sa.Column("notif_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("character_id"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id",
            "notification_id",
            name="uq_notifications_character_notif",
        ),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("esi_state")
    op.drop_table("deliveries")
    op.drop_table("corp_settings")
    op.drop_table("characters")
    op.drop_table("app_state")
