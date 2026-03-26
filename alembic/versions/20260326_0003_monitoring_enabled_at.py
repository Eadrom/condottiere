"""Track monitoring enable time for robust anti-spam cutoff

Revision ID: 20260326_0003
Revises: 20260316_0002
Create Date: 2026-03-26 03:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260326_0003"
down_revision: Union[str, Sequence[str], None] = "20260316_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("monitoring_enabled_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        """
        UPDATE characters
        SET monitoring_enabled_at = COALESCE(updated_at, created_at)
        WHERE monitoring_enabled = TRUE AND monitoring_enabled_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("characters", "monitoring_enabled_at")
