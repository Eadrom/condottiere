"""Track reinforced Merc Den timers for warnings and summaries

Revision ID: 20261009_0005
Revises: 20261009_0004
Create Date: 2026-10-09 01:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261009_0005"
down_revision: Union[str, Sequence[str], None] = "20261009_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "den_timers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("character_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_id", sa.BigInteger(), nullable=False),
        sa.Column("solar_system_id", sa.BigInteger(), nullable=True),
        sa.Column("planet_id", sa.BigInteger(), nullable=True),
        sa.Column("exits_at", sa.DateTime(), nullable=False),
        sa.Column("warned_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("character_id", "notification_id", name="uq_den_timers_character_notif"),
    )
    op.create_index("ix_den_timers_exits_at", "den_timers", ["exits_at"])


def downgrade() -> None:
    op.drop_index("ix_den_timers_exits_at", table_name="den_timers")
    op.drop_table("den_timers")
