"""Widen EVE ID columns to BIGINT

Revision ID: 20260316_0002
Revises: 20260221_0001
Create Date: 2026-03-16 22:55:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260316_0002"
down_revision: Union[str, Sequence[str], None] = "20260221_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # characters
    op.alter_column(
        "characters",
        "character_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="character_id::bigint",
    )
    op.alter_column(
        "characters",
        "corporation_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="corporation_id::bigint",
    )

    # corp_settings
    op.alter_column(
        "corp_settings",
        "corporation_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="corporation_id::bigint",
    )
    op.alter_column(
        "corp_settings",
        "updated_by_character_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="updated_by_character_id::bigint",
    )

    # esi_state
    op.alter_column(
        "esi_state",
        "character_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="character_id::bigint",
    )

    # notifications
    op.alter_column(
        "notifications",
        "character_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="character_id::bigint",
    )
    op.alter_column(
        "notifications",
        "notification_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="notification_id::bigint",
    )

    # deliveries
    op.alter_column(
        "deliveries",
        "character_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="character_id::bigint",
    )
    op.alter_column(
        "deliveries",
        "notification_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="notification_id::bigint",
    )


def downgrade() -> None:
    # deliveries
    op.alter_column(
        "deliveries",
        "notification_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="notification_id::integer",
    )
    op.alter_column(
        "deliveries",
        "character_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="character_id::integer",
    )

    # notifications
    op.alter_column(
        "notifications",
        "notification_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="notification_id::integer",
    )
    op.alter_column(
        "notifications",
        "character_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="character_id::integer",
    )

    # esi_state
    op.alter_column(
        "esi_state",
        "character_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="character_id::integer",
    )

    # corp_settings
    op.alter_column(
        "corp_settings",
        "updated_by_character_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="updated_by_character_id::integer",
    )
    op.alter_column(
        "corp_settings",
        "corporation_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="corporation_id::integer",
    )

    # characters
    op.alter_column(
        "characters",
        "corporation_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="corporation_id::integer",
    )
    op.alter_column(
        "characters",
        "character_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="character_id::integer",
    )
