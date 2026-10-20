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

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    """The current time as naive UTC: the one form every timestamp column holds, on either engine."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    area: Mapped[Area | None] = relationship()
    partner: Mapped[Partner | None] = relationship()


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


class AuditLog(Base):
    """Who changed what in the admin panel and portal."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
