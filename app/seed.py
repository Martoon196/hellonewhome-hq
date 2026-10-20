"""Idempotent seeding: schema (through Alembic), settings, areas, placeholder partners, resources, admin user."""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from . import config
from .auth import generate_password, hash_password, normalise_email
from .db import SessionLocal
from .migrations import run_migrations
from .models import Area, Partner, Resource, User
from .services import content, settings


AREA_SEED_FIELDS = ("name", "region", "covers", "postcodes", "places", "demand", "licensing", "shortlet",
                    "rent_avg", "rent_change", "rent_note")


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
    report: dict = {"areas": 0, "refreshed": 0, "partners": 0, "resources": 0, "admin_created": False, "admin_password": None}
    try:
        settings.ensure_defaults(db)
        if config.FEES_CONFIRMED and not settings.get(db, "fees_confirmed"):
            settings.put(db, "fees_confirmed", True)

        # Areas: new slugs are inserted; existing rows are refreshed only when the seed row carries a higher
        # "revision" than the one recorded in the seed_revisions setting (see _refresh_area).
        stored_revisions = settings.get(db, "seed_revisions")
        applied: dict = dict(stored_revisions) if isinstance(stored_revisions, dict) else {}
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
        if applied != (stored_revisions if isinstance(stored_revisions, dict) else {}):
            settings.put(db, "seed_revisions", applied)
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
                    url=item.get("url", ""), is_placeholder=bool(item.get("placeholder", True)), sort=(i + 1) * 10,
                    visibility="region" if region else "all", region=region,
                ))
                report["resources"] += 1

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

        db.commit()
        return report
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(seed())
