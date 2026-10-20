"""Property instructions and the outbox: "Let this property with Hello New Home" (plan, Phase 3 C2 and C3).

Revision ID: 0003_instructions
Revises: 0002_api_clients
Create Date: 2026-10-20

instructions holds a property a landlord has asked Hello New Home to let, sent server to server by
Property Predator, Key Properties or the public site: who introduced it, the landlord's contact
details with the consent they gave, the property, where it routed (area and partner, by postcode),
its status with a history, and the ids Ordris handed back once the instruction was sent there.
outbox holds the server-to-server messages the app owes another system (today: the instruction sent
to Ordris on "instructed"), with their attempts and last error, so a failed send is retried rather
than lost. Everything here runs unchanged on SQLite and Postgres: plain create_table and
create_index; the downgrade drops both tables.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_instructions"
down_revision: Union[str, None] = "0002_api_clients"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instructions",
        sa.Column("id", sa.Integer(), nullable=False),
        # HNH-000123, derived from the id once the row exists; NULL only between the insert and that update.
        sa.Column("ref", sa.String(length=20), nullable=True),
        sa.Column("external_ref", sa.String(length=200), nullable=False),
        sa.Column("origin_system", sa.String(length=10), nullable=False),
        sa.Column("origin_url", sa.String(length=500), nullable=False),
        sa.Column("introducer_person_key", sa.String(length=200), nullable=False),
        sa.Column("introducer_pp_account_id", sa.String(length=60), nullable=False),
        sa.Column("introducer_kp_user_id", sa.String(length=60), nullable=False),
        sa.Column("landlord_name", sa.String(length=120), nullable=False),
        sa.Column("landlord_email", sa.String(length=200), nullable=False),
        sa.Column("landlord_phone", sa.String(length=40), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("consent_text_version", sa.String(length=40), nullable=False),
        sa.Column("address", sa.String(length=300), nullable=False),
        sa.Column("postcode", sa.String(length=16), nullable=False),
        sa.Column("outward", sa.String(length=8), nullable=False),
        sa.Column("bedrooms", sa.Integer(), nullable=True),
        sa.Column("property_type", sa.String(length=40), nullable=False),
        sa.Column("furnished", sa.String(length=20), nullable=False),
        sa.Column("rent_expectation", sa.Integer(), nullable=True),
        sa.Column("available_from", sa.Date(), nullable=True),
        sa.Column("service_line", sa.String(length=20), nullable=False),
        sa.Column("area_id", sa.Integer(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_history", sa.JSON(), nullable=False),
        sa.Column("ordris_landlord_client_id", sa.String(length=60), nullable=False),
        sa.Column("ordris_property_id", sa.String(length=60), nullable=False),
        sa.Column("ordris_tenancy_id", sa.String(length=60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["area_id"], ["areas.id"]),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_instructions_ref", "instructions", ["ref"], unique=True)
    op.create_index("ix_instructions_external_ref", "instructions", ["external_ref"], unique=True)
    op.create_index("ix_instructions_created_at", "instructions", ["created_at"], unique=False)
    op.create_index("ix_instructions_status", "instructions", ["status"], unique=False)
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_state", "outbox", ["state"], unique=False)
    op.create_index("ix_outbox_target", "outbox", ["target"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_outbox_target", table_name="outbox")
    op.drop_index("ix_outbox_state", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_instructions_status", table_name="instructions")
    op.drop_index("ix_instructions_created_at", table_name="instructions")
    op.drop_index("ix_instructions_external_ref", table_name="instructions")
    op.drop_index("ix_instructions_ref", table_name="instructions")
    op.drop_table("instructions")
