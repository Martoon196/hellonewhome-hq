"""Commercial lettings: the partners.commercial_enabled flag (plan, Phase 5).

Revision ID: 0004_commercial
Revises: 0003_instructions
Create Date: 2026-10-20

A commercial appraisal lead or a commercial property instruction is routed to head office unless the
area's partner has been cleared to take commercial instructions; this flag is that clearance, set by
head office on the partner page. It is the only column the commercial service line needs: the lead
kind (leads.kind) and the service line (instructions.service_line) are strings already. The column is
NOT NULL with a server default of false, so every existing partner row reads as "not enabled" and the
ALTER needs no table rebuild on either engine; the downgrade drops it through batch mode, which SQLite
needs and Postgres passes straight through.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_commercial"
down_revision: Union[str, None] = "0003_instructions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("partners", sa.Column("commercial_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("partners") as batch_op:
        batch_op.drop_column("commercial_enabled")
