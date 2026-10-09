"""Add per-character and per-corporation location alert filters

Revision ID: 20261009_0004
Revises: 20260326_0003
Create Date: 2026-10-09 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261009_0004"
down_revision: Union[str, Sequence[str], None] = "20260326_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("characters", "corp_settings"):
        op.add_column(
            table,
            sa.Column("alert_filter", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in ("corp_settings", "characters"):
        op.drop_column(table, "alert_filter")
