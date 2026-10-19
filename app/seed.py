"""Idempotent seeding: tables, settings, areas, placeholder partners, resources, admin user."""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from . import config
from .auth import generate_password, hash_password
from .db import Base, SessionLocal, engine
from .models import Area, Partner, Resource, User
from .services import content, settings


def seed(db: Session | None = None, *, quiet: bool = False) -> dict:
    """Create tables and seed reference data. Safe to run on every boot."""
    Base.metadata.create_all(engine)
    own = db is None
    db = db or SessionLocal()
    report: dict = {"areas": 0, "partners": 0, "resources": 0, "admin_created": False, "admin_password": None}
    try:
        settings.ensure_defaults(db)

        # Areas (only those not already present; existing rows are never overwritten)
        existing_slugs = {a.slug for a in db.query(Area).all()}
        for i, raw in enumerate(content.areas_seed()):
            if raw["slug"] in existing_slugs:
                continue
            partner = None
            if raw.get("role") == "partner":
                partner = Partner(
                    name=raw.get("partner") if raw.get("partner", "").startswith("[") is False else f"{raw['name']} partner (to be appointed)",
                    first_name="",
                    status="pending",
                    tier="area",
                    whatsapp="" if "X" in (raw.get("whatsapp") or "") else (raw.get("whatsapp") or ""),
                    whatsapp_display="" if "[" in (raw.get("whatsapp_display") or "") else (raw.get("whatsapp_display") or ""),
                    bio="" if "[" in (raw.get("partner_bio") or "") else (raw.get("partner_bio") or ""),
                )
                db.add(partner)
                db.flush()
                report["partners"] += 1
            area = Area(
                slug=raw["slug"], name=raw["name"], region=raw.get("region", "england"),
                role=raw.get("role", "partner"),
                status="active" if raw.get("role") == "hq" else "coming",
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
                    url=item.get("url", ""), is_placeholder=bool(item.get("placeholder", True)), sort=(i + 1) * 10,
                    visibility="region" if region else "all", region=region,
                ))
                report["resources"] += 1

        # Admin user
        if db.query(User).filter(User.role == "admin").count() == 0:
            password = config.ADMIN_PASSWORD or generate_password(14)
            db.add(User(email=config.ADMIN_EMAIL.lower(), password_hash=hash_password(password), role="admin",
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
