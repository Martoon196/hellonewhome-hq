"""Services area by area: per-partner service switches and the service a lead is for.

Revision ID: 0005_partner_services
Revises: 0004_commercial
Create Date: 2026-10-22

The founder offers management area by area, depending on the mentee or franchisee who holds the area.
Four switches sit beside partners.commercial_enabled (which stays the switch for commercial letting):
residential full management, HMO management, short-let management and commercial management. A lead or an
instruction for a service the area's partner does not offer goes to head office (services.routing).

Today's behaviour is kept exactly: every existing partner took residential management, HMOs and short lets,
so those three columns are NOT NULL with a server default of true; commercial management is NOT NULL with a
server default of false and is then set equal to commercial_enabled on every existing row, so a partner
cleared for commercial instructions keeps every commercial enquiry. leads.service records which service an
enquiry is for, so the partner portal can leave out the ones the partner does not offer. Every existing lead is
given its service here by the same rule the app applies at intake (services.routing.lead_service, copied below
as it stood at this revision so the migration never changes with the app): a short-let appraisal, a commercial
appraisal (management when the property is already let) and a rent review by what the landlord asked for.
The rest stay "", which any partner can take. The rule reads the lead's JSON payload, so it runs in Python
rather than in SQL, which differs between SQLite and Postgres. Adding columns with server defaults needs no
table rebuild on either engine; the downgrade drops them through batch mode, which SQLite needs and Postgres
passes straight through.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_partner_services"
down_revision: Union[str, None] = "0004_commercial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PARTNER_SWITCHES = ("residential_management_enabled", "hmo_management_enabled", "shortlet_management_enabled")

# services.routing.lead_service at this revision: the rent review form's "I'm interested in" answers, lower-cased.
_INTEREST_SERVICES = {"full management": "residential_management", "rent guarantee": "residential_management",
                      "hmo": "hmo_management", "short let": "shortlet_management"}


def _lead_service(kind: str, payload) -> str:
    fields = payload if isinstance(payload, dict) else {}
    if kind == "sa_appraisal":
        return "shortlet_management"
    if kind == "commercial_appraisal":
        return ("commercial_management" if str(fields.get("lease_status") or "").strip().lower() == "let"
                else "commercial_letting")
    if kind != "rent_review":
        return ""
    service = _INTEREST_SERVICES.get(" ".join(str(fields.get("interest") or "").lower().split()), "")
    if service == "residential_management" and str(fields.get("type") or "").strip().upper().startswith("HMO"):
        return "hmo_management"
    return service


def upgrade() -> None:
    for name in PARTNER_SWITCHES:
        op.add_column("partners", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("partners", sa.Column("commercial_management_enabled", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    op.execute("UPDATE partners SET commercial_management_enabled = commercial_enabled")
    op.add_column("leads", sa.Column("service", sa.String(length=30), nullable=False, server_default=sa.text("''")))
    leads = sa.table("leads", sa.column("id", sa.Integer), sa.column("kind", sa.String),
                     sa.column("payload", sa.JSON), sa.column("service", sa.String))
    conn = op.get_bind()
    rows = conn.execute(sa.select(leads.c.id, leads.c.kind, leads.c.payload)
                        .where(leads.c.kind.in_(("sa_appraisal", "commercial_appraisal", "rent_review")))).all()
    for lead_id, kind, payload in rows:
        service = _lead_service(kind, payload)
        if service:
            conn.execute(leads.update().where(leads.c.id == lead_id).values(service=service))


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch_op:
        batch_op.drop_column("service")
    with op.batch_alter_table("partners") as batch_op:
        batch_op.drop_column("commercial_management_enabled")
        for name in reversed(PARTNER_SWITCHES):
            batch_op.drop_column(name)
