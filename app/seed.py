"""Idempotent seeding: schema (through Alembic), settings, areas, placeholder partners, resources, admin user."""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from . import config
from .auth import API_TOKEN_MIN_LENGTH, ensure_api_client, generate_password, hash_password, normalise_email
from .db import SessionLocal
from .migrations import run_migrations
from .models import Area, AuditLog, Partner, Resource, User
from .services import content, settings

GROUPHQ_CLIENT = "grouphq"
GROUPHQ_SCOPES = ("partners:write",)
PROPERTIES_SCOPES = ("properties:write",)
# The Partner API clients the environment can bootstrap: (client name, scopes, the config attribute holding the
# secret, the environment variable it came from). Property Predator and Key Properties send property instructions
# ("Let this property with Hello New Home"); Group HQ seats partners.
PARTNER_CLIENTS = (
    (GROUPHQ_CLIENT, GROUPHQ_SCOPES, "PARTNER_TOKEN_GROUPHQ", "HNH_PARTNER_TOKEN_GROUPHQ"),
    ("propertypredator", PROPERTIES_SCOPES, "PARTNER_TOKEN_PROPERTYPREDATOR", "HNH_PARTNER_TOKEN_PROPERTYPREDATOR"),
    ("keyproperties", PROPERTIES_SCOPES, "PARTNER_TOKEN_KEYPROPERTIES", "HNH_PARTNER_TOKEN_KEYPROPERTIES"),
)


AREA_SEED_FIELDS = ("name", "region", "covers", "postcodes", "places", "demand", "licensing", "shortlet",
                    "rent_avg", "rent_change", "rent_note")


def _approve_commercial_fees(db: Session) -> None:
    """The founder approved the commercial fee sheet on 22 October 2026: tick the confirmed box, and quote the
    fees plus VAT. The VAT note is replaced only while it still reads the old default, never an edited one."""
    settings.put(db, "fees_commercial_confirmed", True)
    stored = settings.get(db, "fees_commercial")
    if isinstance(stored, dict) and stored.get("vat_note") == settings.OLD_COMMERCIAL_VAT_NOTE:
        settings.put(db, "fees_commercial", dict(stored, vat_note=settings.DEFAULTS["fees_commercial"]["vat_note"]))


# One-time changes to stored settings, applied in order and recorded in seed_revisions under "settings:<name>"
# exactly as area rows and resource pages are: each runs once on every database, so a later change made in
# Admin -> Settings (unticking a box, say) survives every deploy. Bump the number to run a changed one again.
SETTINGS_REVISIONS = (
    ("commercial-fees-approved", 1, _approve_commercial_fees),
)


def _real(value) -> str:
    """A seed value, or '' when it is a bracketed placeholder such as '[Partner name]'."""
    text = str(value or "").strip()
    return "" if text.startswith("[") or "[" in text else text


def _fill_partner(partner: Partner, raw: dict) -> Partner:
    """Copy a seed row's partner details onto a Partner record. A row may carry a real person (name, first
    name, contact details, bio, status) or bracketed/X-ed placeholders, which leave the fields blank."""
    name = _real(raw.get("partner"))
    partner.name = name or f"{raw['name']} partner (to be appointed)"
    partner.first_name = _real(raw.get("partner_first"))
    partner.email = normalise_email(_real(raw.get("partner_email")))
    partner.phone = _real(raw.get("partner_phone"))
    partner.status = (raw.get("partner_status") or "pending") if name else "pending"
    partner.tier = raw.get("partner_tier") or "area"
    partner.whatsapp = "" if "X" in (raw.get("whatsapp") or "") else (raw.get("whatsapp") or "")
    partner.whatsapp_display = _real(raw.get("whatsapp_display"))
    partner.bio = _real(raw.get("partner_bio"))
    return partner


def _resource_body(item: dict) -> str:
    """The HTML body of a seeded page: inline "body", or the file named by "body_file" under content/resources/."""
    if item.get("body"):
        return str(item["body"])
    return content.resource_body(str(item["body_file"])) if item.get("body_file") else ""


def _refresh_area(db: Session, area: Area, raw: dict) -> None:
    """A seed row whose "revision" is higher than the one last applied updates the live row in place: the
    area's public facts, its status, its bespoke content, and the partner when the live record is still the
    seeded placeholder (or already belongs to the person the row names). Anything else head office has
    done to the area or the partner is left alone, and the same revision is never applied twice."""
    for field in AREA_SEED_FIELDS:
        if field in raw:
            setattr(area, field, raw[field])
    if raw.get("status"):
        area.status = raw["status"]
    bespoke = content.bespoke_area_content(area.slug)
    if bespoke:
        area.content = bespoke
    if raw.get("role") == "partner" and _real(raw.get("partner")):
        partner = area.partner
        email = normalise_email(_real(raw.get("partner_email")))
        if partner is None:
            partner = _fill_partner(Partner(), raw)
            db.add(partner)
            db.flush()
            area.partner_id = partner.id
        elif partner.name.endswith("(to be appointed)") or (email and normalise_email(partner.email) == email):
            _fill_partner(partner, raw)
            db.add(partner)
    db.add(area)


def seed(db: Session | None = None, *, quiet: bool = False) -> dict:
    """Bring the schema up to date and seed reference data. Safe to run on every boot."""
    run_migrations()
    own = db is None
    db = db or SessionLocal()
    report: dict = {"areas": 0, "refreshed": 0, "partners": 0, "resources": 0, "admin_created": False, "admin_password": None,
                    "api_client": None, "api_clients": {}}
    try:
        settings.ensure_defaults(db)
        stored_revisions = settings.get(db, "seed_revisions")
        applied: dict = dict(stored_revisions) if isinstance(stored_revisions, dict) else {}
        for name, rev, apply in SETTINGS_REVISIONS:
            tag = f"settings:{name}"
            if rev > int(applied.get(tag, 0)):
                apply(db)
                applied[tag] = rev
        if config.FEES_CONFIRMED and not settings.get(db, "fees_confirmed"):
            settings.put(db, "fees_confirmed", True)
        if config.FEES_COMMERCIAL_CONFIRMED and not settings.get(db, "fees_commercial_confirmed"):
            settings.put(db, "fees_commercial_confirmed", True)

        # Areas: new slugs are inserted; existing rows are refreshed only when the seed row carries a higher
        # "revision" than the one recorded in the seed_revisions setting (see _refresh_area).
        existing = {a.slug: a for a in db.query(Area).all()}
        for i, raw in enumerate(content.areas_seed()):
            rev = int(raw.get("revision") or 0)
            if raw["slug"] in existing:
                if rev > int(applied.get(raw["slug"], 0)):
                    _refresh_area(db, existing[raw["slug"]], raw)
                    applied[raw["slug"]] = rev
                    report["refreshed"] += 1
                continue
            if rev:
                applied[raw["slug"]] = rev
            partner = None
            if raw.get("role") == "partner":
                partner = _fill_partner(Partner(), raw)
                db.add(partner)
                db.flush()
                report["partners"] += 1
            area = Area(
                slug=raw["slug"], name=raw["name"], region=raw.get("region", "england"),
                role=raw.get("role", "partner"),
                status=raw.get("status") or ("active" if raw.get("role") == "hq" else "coming"),
                partner_id=partner.id if partner else None,
                covers=raw.get("covers", ""), postcodes=raw.get("postcodes", []), places=raw.get("places", []),
                demand=raw.get("demand", ""), licensing=raw.get("licensing", ""), shortlet=raw.get("shortlet", ""),
                rent_avg=raw.get("rent_avg", ""), rent_change=raw.get("rent_change", ""), rent_note=raw.get("rent_note", ""),
                phone=raw.get("phone", "") if raw.get("role") == "hq" else "",
                whatsapp=raw.get("whatsapp", "") if raw.get("role") == "hq" else "",
                whatsapp_display=raw.get("whatsapp_display", "") if raw.get("role") == "hq" else "",
                content=content.bespoke_area_content(raw["slug"]),
                sort=(i + 1) * 10,
            )
            db.add(area)
            report["areas"] += 1
        db.flush()

        # Fill bespoke content for areas that have none yet but now have a content file
        for area in db.query(Area).all():
            if not area.content:
                bespoke = content.bespoke_area_content(area.slug)
                if bespoke:
                    area.content = bespoke
                    db.add(area)

        # Resource library index
        if db.query(Resource).count() == 0:
            for i, item in enumerate(content.resources_seed()):
                region = str(item.get("region") or "").lower()
                region = region if region in ("england", "wales") else ""
                db.add(Resource(
                    category=item.get("category", "General"), title=item.get("title", "Untitled"),
                    description=item.get("description", ""), kind=item.get("kind", "link"),
                    url=item.get("url", ""), body=_resource_body(item), is_placeholder=bool(item.get("placeholder", True)),
                    sort=(i + 1) * 10, visibility="region" if region else "all", region=region,
                ))
                report["resources"] += 1
            db.flush()          # the revision loop below looks the rows up by title (the session does not autoflush)

        # Seeded resource pages that carry a "key" and a "revision" (the commercial training page first): inserted on a
        # live database that already has its library, and refreshed in place whenever the seed's revision is higher
        # than the one recorded in seed_revisions under "resource:<key>", exactly as area rows are. The row is found
        # by its title; between bumps, head office's edits to it survive every deploy. A bump replaces the page, so
        # the text it replaced is kept whole in the audit log: an edit head office made since the last bump can be
        # copied back from there.
        for i, item in enumerate(content.resources_seed()):
            key, rev = item.get("key"), int(item.get("revision") or 0)
            if not key or not rev:
                continue
            tag = f"resource:{key}"
            if rev <= int(applied.get(tag, 0)):
                continue
            region = str(item.get("region") or "").lower()
            region = region if region in ("england", "wales") else ""
            res = db.query(Resource).filter(Resource.title == item.get("title", "")).order_by(Resource.id).first()
            if res is None:
                res = Resource(title=item.get("title", "Untitled"), sort=(i + 1) * 10)
                db.add(res)
                report["resources"] += 1
            else:
                report["refreshed"] += 1
            body = _resource_body(item)
            if res.id is not None and (res.body or "") and res.body != body:
                db.add(AuditLog(user_id=None, action="seed.resource_refresh", target=f"resource:{res.id}",
                                detail=f"{res.title}: replaced by seed revision {rev}. The text it replaced:\n{res.body}"))
            res.category = item.get("category", "General")
            res.description = item.get("description", "")
            res.kind = item.get("kind", "page")
            res.url = item.get("url", "")
            res.body = body
            res.is_placeholder = bool(item.get("placeholder", False))
            res.visibility = "region" if region else "all"
            res.region = region
            db.add(res)
            applied[tag] = rev
        if applied != (stored_revisions if isinstance(stored_revisions, dict) else {}):
            settings.put(db, "seed_revisions", applied)

        # The agency management system is always listed in the resource library (idempotent on its URL).
        ordris_url = str(settings.get(db, "ordris_url") or "").strip()
        if ordris_url and db.query(Resource).filter(Resource.url == ordris_url).count() == 0:
            db.add(Resource(
                category="Software", title="Ordris: agency management system",
                description="Contracts, tenancies, inspections, rent statements and landlord records live in Ordris. "
                            "Log in with the account head office set up for you; this portal holds your leads, "
                            "area pages and marketing kit.",
                kind="link", url=ordris_url, is_placeholder=False, sort=5, visibility="all", region="",
            ))
            report["resources"] += 1

        # Admin user
        if db.query(User).filter(User.role == "admin").count() == 0:
            password = config.ADMIN_PASSWORD or generate_password(14)
            db.add(User(email=normalise_email(config.ADMIN_EMAIL), password_hash=hash_password(password), role="admin",
                        must_change_password=not bool(config.ADMIN_PASSWORD)))
            report["admin_created"] = True
            report["admin_password"] = password
            if not quiet:
                # Only a generated password is printed; one supplied through ADMIN_PASSWORD never goes to the log.
                shown = "(from ADMIN_PASSWORD)" if config.ADMIN_PASSWORD else password
                print(f"[seed] Admin account created: {config.ADMIN_EMAIL}  password: {shown}", file=sys.stderr)

        # The Partner API clients, bootstrapped from the environment so Render needs no shell step. Only the
        # SHA-256 of each variable is stored; a value too short to be a real secret is ignored, which leaves the
        # API closed rather than guarded by a weak one. report["api_client"] keeps Group HQ's outcome by name.
        for name, scopes, attribute, variable in PARTNER_CLIENTS:
            secret = getattr(config, attribute, "")
            if not secret:
                continue
            if len(secret) < API_TOKEN_MIN_LENGTH:
                if not quiet:
                    print(f"[seed] {variable} is shorter than {API_TOKEN_MIN_LENGTH} characters and was ignored; the "
                          f"{name} API client was not created or changed", file=sys.stderr)
                continue
            _, outcome = ensure_api_client(db, name, scopes, secret)
            report["api_clients"][name] = outcome
            if name == GROUPHQ_CLIENT:
                report["api_client"] = outcome
            if not quiet and outcome != "unchanged":
                print(f"[seed] Partner API client {name} {outcome} from {variable}", file=sys.stderr)

        db.commit()
        return report
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(seed())
