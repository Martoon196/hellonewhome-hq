"""Database models.

All tables the admin panel, partner portal and public site need are defined
here. The schema is versioned with Alembic (`alembic/versions`, applied by
`app.migrations.run_migrations` at startup): a change to a model needs a
migration alongside it, and additions should stay backwards-compatible
(nullable columns, or columns with defaults).

The same models run on SQLite and Postgres, so a few rules hold throughout:

* every `String(n)` is wide enough for anything the routers or the seed can
  store (SQLite ignores the length; Postgres rejects an over-long value);
* every `DateTime` is timezone-naive UTC, written through `now()` and shown in
  UK time by `services.times`, so both engines hold identical values
  (`timestamp without time zone` on Postgres);
* `JSON` columns take Python `None` on both engines (SQLAlchemy stores it as the
  JSON literal `null`, which also satisfies NOT NULL);
* column names avoid Postgres reserved words, so the copy script and any raw
  SQL can name them unquoted.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, event, false, func, text,
                        true)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .db import Base


def now() -> datetime:
    """The current time as naive UTC: the one form every timestamp column holds, on either engine."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Partner referral codes (revision 0006): six characters from an alphabet without look-alikes (no I, O, 0 or 1), so a
# code read out over the phone or typed from a flyer comes out right. Stored upper-case; head office may replace one
# with any 3 to 20 letters, digits and hyphens (services.referrals.code_problem).
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 6
REFERRAL_CODE_MAX = 20


class Setting(Base):
    """Key/value site settings (HQ contact details, fee sheet, etc.)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSON, nullable=True)


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    first_name: Mapped[str] = mapped_column(String(60), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    whatsapp: Mapped[str] = mapped_column(String(32), default="")          # international digits, e.g. 447700900123
    whatsapp_display: Mapped[str] = mapped_column(String(40), default="")  # e.g. 07700 900123
    bio: Mapped[str] = mapped_column(Text, default="")
    photo_path: Mapped[str] = mapped_column(String(300), default="")       # relative to UPLOAD_DIR
    tier: Mapped[str] = mapped_column(String(20), default="introducer")    # introducer | area
    status: Mapped[str] = mapped_column(String(20), default="pending")     # pending | active | paused | left
    units_managed: Mapped[int] = mapped_column(Integer, default=0)         # set by HQ for dashboard KPIs
    notes: Mapped[str] = mapped_column(Text, default="")                   # HQ-only notes
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Recorded by Group HQ through the Partner API (revision 0002); NULL until it has said anything. Shown
    # read-only on the admin partner page and never edited there.
    franchise_fee_waived: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    franchise_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cleared by head office to take commercial property instructions (revision 0004). While False, a commercial
    # lead or instruction in this partner's area is routed to head office and kept out of their portal; see
    # services.routing. The server default means every row that predates the column reads as not cleared.
    commercial_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    # The services this partner offers in their areas (revision 0005), set by head office on the partner page: the
    # founder offers management area by area, depending on who holds the area. Off, an enquiry for that service in
    # their area goes to head office, their area pages say it is not offered locally yet, and their portal leaves
    # it out (services.routing). Commercial letting is commercial_enabled above. Every partner took residential
    # management, HMOs and short lets before these existed, so those default to on; commercial management starts
    # equal to commercial_enabled (the migration copies it) and defaults to off for a new partner.
    residential_management_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    hmo_management_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    shortlet_management_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    commercial_management_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    # The partner's referral code (revision 0006): ?ref=<code> on any public page credits the enquiries that follow to
    # this partner (services.referrals). Unique; given to every partner when the row is first flushed (the listener at
    # the foot of this module) and editable by head office on the partner page, where the code given up is kept in
    # retired_referral_codes so it is never given to anyone else. Nullable in the schema only so the migration could
    # add it to existing rows before filling them; no row is left without one.
    referral_code: Mapped[str | None] = mapped_column(String(REFERRAL_CODE_MAX), nullable=True, unique=True, index=True)

    areas: Mapped[list["Area"]] = relationship(back_populates="partner")
    users: Mapped[list["User"]] = relationship(back_populates="partner")

    @property
    def display_first(self) -> str:
        return self.first_name or (self.name.split()[0] if self.name else "")

    @property
    def has_whatsapp(self) -> bool:
        return bool(self.whatsapp) and "X" not in self.whatsapp.upper()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="partner")        # admin | partner
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    partner: Mapped[Partner | None] = relationship(back_populates="users")


# Logins are matched on the lower-cased email (auth.login_keys), so two addresses that differ only in case
# must not both exist. A functional unique index says so on both engines; `ix_users_email` stays for lookups.
Index("ux_users_email_lower", func.lower(User.email), unique=True)


class Area(Base):
    __tablename__ = "areas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(20), default="england")      # england | wales
    role: Mapped[str] = mapped_column(String(20), default="partner")        # hq | partner
    status: Mapped[str] = mapped_column(String(20), default="coming")       # active | coming | paused | hidden
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    covers: Mapped[str] = mapped_column(Text, default="")
    postcodes: Mapped[list] = mapped_column(JSON, default=list)             # outward codes or whole areas, e.g. "DH1", "SE"
    places: Mapped[list] = mapped_column(JSON, default=list)
    demand: Mapped[str] = mapped_column(Text, default="")
    licensing: Mapped[str] = mapped_column(Text, default="")
    shortlet: Mapped[str] = mapped_column(Text, default="")
    rent_avg: Mapped[str] = mapped_column(String(40), default="")
    rent_change: Mapped[str] = mapped_column(String(40), default="")
    rent_note: Mapped[str] = mapped_column(Text, default="")
    # HQ-run areas carry their own contact; partner areas use the partner's.
    phone: Mapped[str] = mapped_column(String(40), default="")
    whatsapp: Mapped[str] = mapped_column(String(32), default="")
    whatsapp_display: Mapped[str] = mapped_column(String(40), default="")
    # Bespoke SEO content: {"hub": {...}, "lettings": {...}, "shortlets": {...}, "neighbourhoods": [...], "sources": [...]}
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    partner: Mapped[Partner | None] = relationship(back_populates="areas")

    # ---- derived contact details --------------------------------------
    @property
    def is_hq(self) -> bool:
        return self.role == "hq"

    @property
    def label(self) -> str:
        return "Head office" if self.is_hq else "Local Partner"

    @property
    def partner_name(self) -> str:
        if self.is_hq:
            return "The head office team"
        return self.partner.name if self.partner else "[Partner name]"

    @property
    def partner_first(self) -> str:
        if self.is_hq:
            return "the head office team"
        return self.partner.display_first if self.partner else "[Partner first name]"

    @property
    def partner_bio(self) -> str:
        if self.is_hq:
            return ("Hello New Home started here. The office on Central Business Park is where every area's "
                    "contracts, compliance and rent collection are run from.")
        return self.partner.bio if self.partner and self.partner.bio else "[Partner bio]"

    def contact_whatsapp(self, hq_whatsapp: str) -> str:
        """Digits for wa.me; falls back to HQ when the partner has no real number."""
        if not self.is_hq and self.partner and self.partner.has_whatsapp:
            return self.partner.whatsapp
        if self.whatsapp and "X" not in self.whatsapp.upper():
            return self.whatsapp
        return hq_whatsapp

    def contact_whatsapp_display(self, hq_display: str) -> str:
        if not self.is_hq and self.partner and self.partner.has_whatsapp:
            return self.partner.whatsapp_display or self.partner.whatsapp
        if self.whatsapp_display:
            return self.whatsapp_display
        return hq_display

    @property
    def is_public(self) -> bool:
        return self.status in ("active", "coming")


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="enquiry")  # rent_review | sa_appraisal | contact | alert | partner_application | enquiry
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    postcode: Mapped[str] = mapped_column(String(16), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    region: Mapped[str] = mapped_column(String(20), default="")
    area_id: Mapped[int | None] = mapped_column(ForeignKey("areas.id"), nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new")    # new | contacted | booked | instructed | lost
    payload: Mapped[dict] = mapped_column(JSON, default=dict)         # every submitted field
    notes: Mapped[str] = mapped_column(Text, default="")
    source_page: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    # The service the enquiry is for, as services.routing.lead_service reads it at intake (revision 0005):
    # residential_management | hmo_management | shortlet_management | commercial_letting | commercial_management,
    # or "" for one any partner can take (a tenant find, a general question). It decides the partner and what the
    # partner's portal shows when they do not offer the service.
    service: Mapped[str] = mapped_column(String(30), default="", server_default=text("''"), nullable=False)
    # The partner who referred the enquiry (revision 0006), which is not the partner it is routed to: the first is who
    # introduced the landlord (their ?ref= link, or head office by hand), the second is who serves the area. Head
    # office works out any introducer pay by hand from these (Admin -> Referrals); nothing here is a fee.
    # referrer_via says how it was recorded: "link" (the hnh_ref cookie) or "head office" (set on the lead page).
    referrer_partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True, index=True)
    referrer_via: Mapped[str] = mapped_column(String(20), default="", server_default=text("''"), nullable=False)
    # Group HQ's franchise seller code from ?seller=<code> (the hnh_seller cookie), kept on a partner-programme
    # application so head office can credit the seller in Group HQ. "" for every other lead.
    seller_code: Mapped[str] = mapped_column(String(24), default="", server_default=text("''"), nullable=False)

    area: Mapped[Area | None] = relationship()
    partner: Mapped[Partner | None] = relationship(foreign_keys=[partner_id])
    referrer: Mapped[Partner | None] = relationship(foreign_keys=[referrer_partner_id])


class Resource(Base):
    """Partner resource library item: an uploaded file, an external link or an in-app page."""
    __tablename__ = "resources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(80), default="General")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="link")      # file | link | page
    url: Mapped[str] = mapped_column(String(500), default="")
    file_path: Mapped[str] = mapped_column(String(300), default="")    # relative to UPLOAD_DIR
    body: Mapped[str] = mapped_column(Text, default="")                # for kind == page (simple HTML)
    visibility: Mapped[str] = mapped_column(String(20), default="all") # all | area | region
    area_id: Mapped[int | None] = mapped_column(ForeignKey("areas.id"), nullable=True)
    region: Mapped[str] = mapped_column(String(20), default="")        # when visibility == region
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ApiClient(Base):
    """A server-to-server caller of the Partner API (Group HQ is the first). The secret itself is never
    stored: `token_hash` is its SHA-256, compared in constant time by `auth.require_api_client`. `scopes` is a
    JSON list such as ["partners:write"]; an inactive row is refused like an unknown one."""
    __tablename__ = "api_clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


INSTRUCTION_ORIGINS = ("pp", "kp", "web")
INSTRUCTION_SERVICE_LINES = ("residential", "hmo", "sa", "commercial")
INSTRUCTION_STATUSES = ("new", "contacted", "appraised", "instructed", "let", "lost")
OUTBOX_STATES = ("pending", "sent", "failed")


class Instruction(Base):
    """A property a landlord has asked Hello New Home to let (plan, Phase 3 C2): sent server to server by
    Property Predator or Key Properties, or taken on the public site. It sits beside Lead rather than inside
    it because it carries a property, a landlord's consent, an introducer and the ids Ordris hands back.

    `ref` (HNH-000123) is the number people quote; it is derived from the id by `make_ref` once the row has
    been flushed. `external_ref` is the sender's own key (pp:<slug>, kp:<deal_id>) and is what makes a resend
    idempotent. `status_history` is a JSON list of {at, from, to, by, note}, newest last; it is always
    replaced, never appended in place, so SQLAlchemy sees the change. Nothing here is a fee figure: the
    partner portal shows the structured fields and never the raw `payload`."""
    __tablename__ = "instructions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True, index=True)
    external_ref: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    origin_system: Mapped[str] = mapped_column(String(10), default="web")        # pp | kp | web
    origin_url: Mapped[str] = mapped_column(String(500), default="")
    introducer_person_key: Mapped[str] = mapped_column(String(200), default="")  # lower-cased email (shared person key)
    introducer_pp_account_id: Mapped[str] = mapped_column(String(60), default="")
    introducer_kp_user_id: Mapped[str] = mapped_column(String(60), default="")
    landlord_name: Mapped[str] = mapped_column(String(120), default="")
    landlord_email: Mapped[str] = mapped_column(String(200), default="")
    landlord_phone: Mapped[str] = mapped_column(String(40), default="")
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    consent_text_version: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    postcode: Mapped[str] = mapped_column(String(16), default="")
    outward: Mapped[str] = mapped_column(String(8), default="")
    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    property_type: Mapped[str] = mapped_column(String(40), default="")
    furnished: Mapped[str] = mapped_column(String(20), default="")
    rent_expectation: Mapped[int | None] = mapped_column(Integer, nullable=True)   # pounds a month, the landlord's own figure
    available_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_line: Mapped[str] = mapped_column(String(20), default="residential")  # residential | hmo | sa | commercial
    area_id: Mapped[int | None] = mapped_column(ForeignKey("areas.id"), nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    status_history: Mapped[list] = mapped_column(JSON, default=list)
    ordris_landlord_client_id: Mapped[str] = mapped_column(String(60), default="")
    ordris_property_id: Mapped[str] = mapped_column(String(60), default="")
    ordris_tenancy_id: Mapped[str] = mapped_column(String(60), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)                     # the body as sent, for head office only
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    # The partner who introduced the property (revision 0006), as on Lead: "introducer" when the sender's introducer
    # email is an active partner's and the sender vouched for it (partner_api.introducer_vouched), "lead" when it was
    # made from a referred lead, "head office" when set by hand.
    referrer_partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True, index=True)
    referrer_via: Mapped[str] = mapped_column(String(20), default="", server_default=text("''"), nullable=False)

    area: Mapped[Area | None] = relationship()
    partner: Mapped[Partner | None] = relationship(foreign_keys=[partner_id])
    referrer: Mapped[Partner | None] = relationship(foreign_keys=[referrer_partner_id])

    @staticmethod
    def make_ref(instruction_id: int) -> str:
        return f"HNH-{instruction_id:06d}"

    @property
    def sent_to_ordris(self) -> bool:
        return bool(self.ordris_landlord_client_id or self.ordris_property_id)

    @property
    def partner_first(self) -> str:
        """The first name the landlord will hear from: the partner the instruction routed to, or head office. The
        instruction's own partner, not the area's: a commercial instruction in an uncleared partner's area is head
        office's (services.routing), and the sender is told so."""
        return (self.partner.display_first or "the head office team") if self.partner else "the head office team"


class Outbox(Base):
    """A server-to-server message the app owes another system (plan, Phase 3 C3): today the instruction sent
    to Ordris when a property is marked instructed. `kind` names the message (ordris.instruction), `target`
    the row it is about (instruction:<id>), `payload` the body as it will be sent. A row is pending until it
    is sent; a failed attempt records the error and leaves it for the retry command or the Resend button.
    The secret the message is sent with is never part of the row."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    target: Mapped[str] = mapped_column(String(200), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(10), default="pending", index=True)   # pending | sent | failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Who changed what in the admin panel and portal."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class RetiredReferralCode(Base):
    """A referral code a partner gave up when head office changed it (revision 0006). It stays with that partner for
    good: no other partner may be given it, so a flyer, a message or a 90-day hnh_ref cookie that still carries the
    old code can never credit someone else. It credits nobody (services.referrals), and the partner may take it back."""
    __tablename__ = "retired_referral_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(REFERRAL_CODE_MAX), unique=True, index=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    retired_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---- referral codes for new partners -------------------------------------------------------------------------------
def new_referral_code(session: Session, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A referral code no partner holds or has held and not in `taken` (codes handed out earlier in the same flush).
    Six characters from 32 give about a billion codes, so the first draw is almost always free; the loop is the
    guarantee."""
    with session.no_autoflush:
        while True:
            code = "".join(secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
            if (code not in taken and session.query(Partner.id).filter(Partner.referral_code == code).first() is None
                    and session.query(RetiredReferralCode.id).filter(RetiredReferralCode.code == code).first() is None):
                return code


@event.listens_for(Session, "before_flush")
def _give_new_partners_a_referral_code(session: Session, flush_context, instances) -> None:
    """Every partner gets a referral code the moment it is first written, whichever path creates it (the admin form,
    the Partner API, the seed or a script), so no partner is ever without a link to share."""
    taken: set[str] = set()
    for obj in list(session.new):
        if isinstance(obj, Partner) and not obj.referral_code:
            obj.referral_code = new_referral_code(session, taken)
            taken.add(obj.referral_code)
