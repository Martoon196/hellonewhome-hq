"""Token rendering for the marketing kit and area SEO fallback content.

Tokens look like {{area}}, {{partner_first}}, {{place_1}} and are listed in
`TOKENS`. Rendering never raises on an unknown token; it leaves it visible so
the gap is obvious in a preview.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import Area

TOKENS = [
    "area", "name", "covers", "partner", "partner_first", "partner_bio", "whatsapp_display", "whatsapp_link", "phone",
    "area_url", "rent_avg", "rent_change", "rent_note", "licensing", "shortlet", "demand",
    "place_1", "place_2", "place_3", "place_4", "place_5", "place_6", "region_law", "region_law_long",
    "month", "year", "base_url", "slug",
]

_TOKEN_RE = re.compile(r"\{\{\s*([a-z_0-9]+)\s*\}\}")

HQ_TEAM = "the head office team"
HQ_BIO = ("Hello New Home started here. The office on Central Business Park is where every area's "
          "contracts, compliance and rent collection are run from.")
PENDING_BIO = ("Head office is looking after this area while the local team is appointed: contracts, compliance "
               "and rent collection all run from Swansea, on the same capped fee.")


def partner_pending(area: Area) -> bool:
    """True while an area has no appointed partner: no partner row, or one still marked pending.
    Seeded placeholders such as "Durham partner (to be appointed)" fall in here, so the public site
    never says "WhatsApp Durham" or names a person who does not exist yet."""
    return not area.is_hq and (area.partner is None or area.partner.status == "pending")


def partner_name(area: Area) -> str:
    if area.is_hq or partner_pending(area):
        return "The head office team"
    return area.partner.name


def partner_first(area: Area) -> str:
    if area.is_hq or partner_pending(area):
        return HQ_TEAM
    return area.partner.display_first or HQ_TEAM


def partner_bio(area: Area) -> str:
    if area.is_hq:
        return HQ_BIO
    if partner_pending(area):
        return PENDING_BIO
    if not area.partner.bio:
        return (f"{area.partner.display_first or area.partner.name} is the Local Partner for {area.name}; contracts, "
                "compliance and rent collection run from head office.")
    return area.partner.bio


def region_law(region: str) -> str:
    return "Rent Smart Wales licensed" if region == "wales" else "Redress scheme and client money protected"


def region_law_long(region: str) -> str:
    """One-paragraph legal summary used by the area content template."""
    if region == "wales":
        return ("Wales has its own rulebook. Every landlord must be registered with Rent Smart Wales and either licensed "
                "or using a licensed agent; tenants are contract-holders on an occupation contract with a written statement "
                "within 14 days; holding deposits are capped at one week's rent; and no-fault possession needs six months' notice.")
    return ("England runs on the Renters' Rights Act: since 1 May 2026 tenancies are periodic, section 21 has gone and possession "
            "needs a legal ground; rent rises once a year by two months' notice; holding deposits are capped at one week's rent "
            "and security deposits at five weeks; every adult's right to rent is checked before the tenancy starts.")


def context_for(area: Area, *, hq_whatsapp: str, hq_whatsapp_display: str, hq_phone: str, base_url: str,
                today: date | None = None) -> dict[str, str]:
    today = today or date.today()
    places = list(area.places or []) + ["", "", "", "", "", ""]
    wa = area.contact_whatsapp(hq_whatsapp)
    ctx = {
        "area": area.name,
        "name": area.name,
        "covers": area.covers or area.name,
        "partner": partner_name(area),
        "partner_first": partner_first(area),
        "partner_bio": partner_bio(area),
        "whatsapp_display": area.contact_whatsapp_display(hq_whatsapp_display),
        "whatsapp_link": f"https://wa.me/{wa}",
        "phone": (area.partner.phone if (area.partner and area.partner.phone) else area.phone) or hq_phone,
        "area_url": f"{base_url}/areas/{area.slug}",
        "rent_avg": area.rent_avg or "",
        "rent_change": area.rent_change or "",
        "rent_note": area.rent_note or "",
        "licensing": area.licensing or "",
        "shortlet": area.shortlet or "",
        "demand": area.demand or "",
        "place_1": places[0],
        "place_2": places[1],
        "place_3": places[2],
        "place_4": places[3],
        "place_5": places[4],
        "place_6": places[5],
        "region_law": region_law(area.region),
        "region_law_long": region_law_long(area.region),
        "slug": area.slug,
        "month": today.strftime("%B"),
        "year": str(today.year),
        "base_url": base_url,
    }
    return ctx


def render(text: str, ctx: dict[str, str]) -> str:
    if not text:
        return ""
    return _TOKEN_RE.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), text)


def render_deep(obj, ctx: dict[str, str]):
    """Render tokens throughout a nested dict/list structure of strings."""
    if isinstance(obj, str):
        return render(obj, ctx)
    if isinstance(obj, list):
        return [render_deep(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: render_deep(v, ctx) for k, v in obj.items()}
    return obj
