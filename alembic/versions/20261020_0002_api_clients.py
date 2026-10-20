"""Partner API: the api_clients table and the two franchise columns on partners.

Revision ID: 0002_api_clients
Revises: 0001_baseline
Create Date: 2026-10-20

api_clients holds the server-to-server callers of /api/partner (Group HQ first): the SHA-256 of each
secret, its scopes and whether it is still active. partners gains franchise_fee_waived and
franchise_note, both nullable, which Group HQ records when it seats a mentee; nothing else reads or
writes them. Everything here runs unchanged on SQLite and Postgres: create_table, create_index and
add_column of a nullable column need no table rebuild, and the downgrade drops the columns through
batch mode, which SQLite needs and Postgres passes straight through.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_api_clients"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_clients_name", "api_clients", ["name"], unique=True)
    op.add_column("partners", sa.Column("franchise_fee_waived", sa.Boolean(), nullable=True))
    op.add_column("partners", sa.Column("franchise_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("partners") as batch_op:
        batch_op.drop_column("franchise_note")
        batch_op.drop_column("franchise_fee_waived")
    op.drop_index("ix_api_clients_name", table_name="api_clients")
    op.drop_table("api_clients")
