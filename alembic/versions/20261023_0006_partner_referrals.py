"""Partner referrals: a referral code for every partner, and the referring partner on leads and instructions.

Revision ID: 0006_partner_referrals
Revises: 0005_partner_services
Create Date: 2026-10-23

Every mentee is enrolled in Hello New Home's Local Partner programme so they can earn from introductions, which
needs the site to know who introduced whom:

* partners.referral_code: a short code, unique, upper-case. ?ref=<code> on any public page remembers the partner for
  90 days (services.referrals). Every existing partner is given one here, drawn the way the app draws them
  (six characters without look-alikes, copied below so this script never changes with the app); a new partner is
  given one when it is first written. Head office may change it on the partner page. The column is nullable only
  so it could be added before the rows were filled; the unique index is created after the backfill.
* retired_referral_codes: a code a partner gave up when head office changed it. It stays theirs, so it is never
  given to another partner and an old link or cookie carrying it can never credit someone else. Empty at first.
* leads.referrer_partner_id and instructions.referrer_partner_id: the partner who introduced the enquiry or the
  property, which is not the partner it was routed to. referrer_via says how it was recorded. leads.seller_code keeps
  Group HQ's franchise seller code from ?seller=<code> on a partner-programme application.

An instruction already on record whose introducer email (introducer_person_key, sent by Property Predator or Key
Properties) is the email of a partner who is active and whose record existed when the instruction arrived is credited
to that partner when the sender vouched for the email, the rule the intake applies from now on: the body it posted
(instructions.payload) says introducer.email_verified true, or it came from Key Properties (origin kp), whose users
sign in by a link sent to that address, and says nothing either way. An unverified match is left for head office to
credit by hand. No lead carried a referral before this revision, so none is backfilled. Nothing here is a fee: pay is
worked out by hand from Admin -> Referrals.

Both engines take the new columns with plain ALTER TABLE ... ADD COLUMN (the foreign keys inline, which SQLite allows
on ADD COLUMN), so nothing is rebuilt on the way up. The way down drops the indexes and then the columns: leads and
instructions through batch mode (SQLite cannot drop a column that carries a foreign key, so it rebuilds those two
tables, which nothing references; Postgres drops the column and its constraint in place), partners with a plain DROP
COLUMN once its index is gone, so the table every other one references is never rebuilt. retired_referral_codes is
dropped first, since it references partners.
"""
from __future__ import annotations

import json
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_partner_referrals"
down_revision: Union[str, None] = "0005_partner_services"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# app.models at this revision: the referral code alphabet (no I, O, 0 or 1) and length.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_LENGTH = 6


def _code(taken: set[str]) -> str:
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))
        if code not in taken:
            taken.add(code)
            return code


def _vouched(origin, payload) -> bool:
    """Whether the sender vouched for the introducer's email (app.routers.partner_api.introducer_vouched at this
    revision, read from the body it posted): introducer.email_verified true, or Key Properties saying nothing."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    introducer = payload.get("introducer") if isinstance(payload, dict) else None
    flag = introducer.get("email_verified") if isinstance(introducer, dict) else None
    return flag is True or (flag is None and origin == "kp")


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column("partners", sa.Column("referral_code", sa.String(length=20), nullable=True))
    partners = sa.table("partners", sa.column("id", sa.Integer), sa.column("referral_code", sa.String),
                        sa.column("email", sa.String), sa.column("status", sa.String), sa.column("created_at", sa.DateTime))
    taken: set[str] = set()
    for (partner_id,) in conn.execute(sa.select(partners.c.id).order_by(partners.c.id)).all():
        conn.execute(partners.update().where(partners.c.id == partner_id).values(referral_code=_code(taken)))
    op.create_index("ix_partners_referral_code", "partners", ["referral_code"], unique=True)

    op.create_table(
        "retired_referral_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retired_referral_codes_code", "retired_referral_codes", ["code"], unique=True)
    op.create_index("ix_retired_referral_codes_partner_id", "retired_referral_codes", ["partner_id"], unique=False)

    for table in ("leads", "instructions"):
        op.add_column(table, sa.Column("referrer_partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=True),
                      inline_references=True)
        op.create_index(f"ix_{table}_referrer_partner_id", table, ["referrer_partner_id"], unique=False)
        op.add_column(table, sa.Column("referrer_via", sa.String(length=20), nullable=False, server_default=sa.text("''")))
    op.add_column("leads", sa.Column("seller_code", sa.String(length=24), nullable=False, server_default=sa.text("''")))

    # Instructions already sent by Property Predator or Key Properties with a partner as the introducer.
    instructions = sa.table("instructions", sa.column("id", sa.Integer), sa.column("introducer_person_key", sa.String),
                            sa.column("origin_system", sa.String), sa.column("payload", sa.JSON),
                            sa.column("created_at", sa.DateTime), sa.column("referrer_partner_id", sa.Integer),
                            sa.column("referrer_via", sa.String))
    active = {}
    for partner_id, email, created_at in conn.execute(
            sa.select(partners.c.id, partners.c.email, partners.c.created_at)
            .where(partners.c.status == "active").order_by(partners.c.id)).all():
        key = (email or "").strip().lower()
        if key and key not in active:
            active[key] = (partner_id, created_at)
    if active:
        rows = conn.execute(sa.select(instructions.c.id, instructions.c.introducer_person_key, instructions.c.origin_system,
                                      instructions.c.payload, instructions.c.created_at)
                            .where(instructions.c.introducer_person_key != "")).all()
        for instruction_id, person_key, origin, payload, arrived in rows:
            match = active.get((person_key or "").strip().lower())
            if match is None or not _vouched(origin, payload):
                continue
            partner_id, partner_created = match
            if partner_created is not None and arrived is not None and partner_created > arrived:
                continue
            conn.execute(instructions.update().where(instructions.c.id == instruction_id)
                         .values(referrer_partner_id=partner_id, referrer_via="introducer"))


def downgrade() -> None:
    op.drop_index("ix_retired_referral_codes_partner_id", table_name="retired_referral_codes")
    op.drop_index("ix_retired_referral_codes_code", table_name="retired_referral_codes")
    op.drop_table("retired_referral_codes")
    for table in ("instructions", "leads"):
        op.drop_index(f"ix_{table}_referrer_partner_id", table_name=table)
    with op.batch_alter_table("instructions") as batch_op:
        batch_op.drop_column("referrer_via")
        batch_op.drop_column("referrer_partner_id")
    with op.batch_alter_table("leads") as batch_op:
        batch_op.drop_column("seller_code")
        batch_op.drop_column("referrer_via")
        batch_op.drop_column("referrer_partner_id")
    op.drop_index("ix_partners_referral_code", table_name="partners")
    op.drop_column("partners", "referral_code")
