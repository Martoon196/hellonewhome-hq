"""Public website: brand pages, SEO area pages, lead capture, dynamic assets."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, client_ip, require_partner, throttle_check, throttle_hit
from ..db import get_db
from ..models import Area, Lead, Resource, User
from ..services import content, kit, mail, ordris_listings, routing, settings
from ..services.postcodes import extract_postcode, locate

log = logging.getLogger("hnh.public")
# check_origin: the site's own forms post same-origin; a cross-site POST to /api/lead is refused.
router = APIRouter(dependencies=[Depends(check_origin)])

KIND_SUBJECTS = {
    "rent_review": "Free rent review request",
    "sa_appraisal": "Short-let appraisal request",
    "commercial_appraisal": "Commercial appraisal request",
    "contact": "General enquiry",
    "alert": "Property alert registration",
    "partner_application": "Local Partner application",
}
# Only these kinds (plus the plain "enquiry") can arrive from the public form; partner_request is the portal's.
PUBLIC_KINDS = set(KIND_SUBJECTS) | {"enquiry"}

# Commercial appraisal (plan, Phase 5): the extra fields the form carries, validated before the lead is stored.
COMMERCIAL_USES = ("office", "retail", "industrial", "leisure", "mixed", "other")
COMMERCIAL_LEASE_STATUSES = ("vacant", "let", "lease_ending")
COMMERCIAL_SIZE_MAX = 5_000_000          # square feet
COMMERCIAL_RENT_MAX = 50_000_000         # pounds a year
# Questions landlords ask about commercial lettings: shown on /commercial and carried in its FAQPage markup.
COMMERCIAL_FAQS = [
    ("Does the Renters' Rights Act apply to my shop or office?",
     "No. The Renters' Rights Act 2025 governs residential tenancies in England. A business lease is governed by the "
     "Landlord and Tenant Act 1954, the lease itself and the general law of contract, which is why the paperwork and "
     "the timetable are different."),
    ("Do I have to protect a commercial tenant's deposit in a scheme?",
     "No. Tenancy deposit protection applies to residential tenancies of homes (assured tenancies in England, occupation "
     "contracts in Wales). A commercial rent deposit is held "
     "under a rent deposit deed that says how much, where it sits, when it can be drawn on and when it is returned."),
    ("What does contracting out of the 1954 Act mean?",
     "By default a business tenant has the right to renew the lease when it ends. Landlord and tenant can agree to "
     "exclude that right, but only by serving the statutory warning notice and the tenant making the declaration "
     "before the lease is signed. Your solicitor handles the notice; we make sure it is in the heads of terms."),
    ("Can I let a property with an EPC rating of F or G?",
     "Not without a registered exemption. Non-domestic property needs an E or better to be let, and the government "
     "has proposed tightening that to C and then B. We check the rating and the exemption position at the appraisal."),
    ("Who pays the business rates?",
     "The occupier, once let. Empty property is the owner's liability after the relief period, which is why a vacant "
     "unit is appraised with the rates bill in mind."),
]


def _optional_int(value, field: str, high: int) -> int | None:
    """An optional whole number from a form: digits in a string count ("£25,000 a year" reads as 25000); anything
    else that is not blank is refused with the field named."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    else:
        digits = re.sub(r"[^0-9]", "", str(value))
        if not digits:
            raise HTTPException(status_code=422, detail=f"{field} must be a whole number")
        number = int(digits[:12])
    if not 0 <= number <= high:
        raise HTTPException(status_code=422, detail=f"{field} must be between 0 and {high:,}")
    return number


def _commercial_fields(data: dict) -> dict:
    """The commercial appraisal's own fields, validated and normalised for the lead's payload. property_use and
    lease_status must be one of the listed values; size_sqft and asking_rent are optional whole numbers; the
    business name is trimmed text. A bad value is a 422 naming the field, and nothing is stored."""
    use = str(data.get("property_use") or "").strip().lower()
    if use not in COMMERCIAL_USES:
        raise HTTPException(status_code=422, detail=f"property_use must be one of {', '.join(COMMERCIAL_USES)}")
    lease = re.sub(r"[\s-]+", "_", str(data.get("lease_status") or "").strip().lower())
    if lease not in COMMERCIAL_LEASE_STATUSES:
        raise HTTPException(status_code=422, detail=f"lease_status must be one of {', '.join(COMMERCIAL_LEASE_STATUSES)}")
    fields = {
        "business_name": " ".join(str(data.get("business_name") or "").split())[:120],
        "property_use": use,
        "size_sqft": _optional_int(data.get("size_sqft"), "size_sqft", COMMERCIAL_SIZE_MAX),
        "lease_status": lease,
        "asking_rent": _optional_int(data.get("asking_rent"), "asking_rent", COMMERCIAL_RENT_MAX),
    }
    raw_rent = str(data.get("asking_rent") or "").strip()
    if fields["asking_rent"] is not None and raw_rent != str(fields["asking_rent"]):
        fields["asking_rent_as_typed"] = raw_rent[:LEAD_MAX_VALUE]      # "£25,000 a year" keeps its wording
    return fields

# Lead intake limits: a lead is a short form, never a document.
LEAD_MAX_BYTES = 32 * 1024          # whole request body
LEAD_MAX_FIELDS = 40                # keys kept in Lead.payload
LEAD_MAX_VALUE = 500                # characters kept per payload value
LEAD_MAX_KEY = 60
LEAD_RATE_LIMIT = 30                # stored leads per address per hour
LEAD_RATE_WINDOW = 60 * 60

# Partner photos are stored by the admin panel and portal as photos/<partner_id>.<ext>.
PHOTO_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
_PHOTO_NAME = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")


# ---- helpers ---------------------------------------------------------------
def public_areas(db: Session) -> list[Area]:
    return db.query(Area).filter(Area.status.in_(("active", "coming"))).order_by(Area.sort, Area.id).all()


def render_public(request: Request, template: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    from ..main import templates
    db = ctx.pop("db", None) or next(get_db())
    base = {
        "request": request,
        "s": settings.get_all(db),
        "areas_nav": public_areas(db),
        "nav_active": ctx.get("nav_active", ""),
        "body_region": ctx.get("body_region", ""),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def area_cards_html(request: Request, db: Session) -> str:
    from ..main import templates
    return templates.get_template("public/partials/area_cards.html").render(areas=public_areas(db))


def area_options_html(db: Session) -> str:
    """<option> rows for the property-alert town picker: every public area, then "Somewhere else"."""
    rows = [f'<option value="{escape(a.slug)}">{escape(a.name)}</option>' for a in public_areas(db)]
    rows.append('<option value="">Somewhere else</option>')
    return "".join(rows)


def _area_or_404(db: Session, slug: str) -> Area:
    area = db.query(Area).filter(Area.slug == slug).first()
    if not area or area.status == "hidden":
        raise HTTPException(status_code=404, detail="Area not found")
    return area


def area_ctx(db: Session, area: Area) -> dict:
    s = settings.get_all(db)
    ctx = kit.context_for(area, hq_whatsapp=s["hq_whatsapp"], hq_whatsapp_display=s["hq_whatsapp_display"],
                          hq_phone=s["hq_phone"], base_url=config.BASE_URL)
    ctx["region_law_long"] = ("Licensed by Rent Smart Wales; every landlord must be registered." if area.region == "wales"
                              else "England's Renters' Rights Act: periodic tenancies, no section 21, Right to Rent checks.")
    return ctx


def _render_page_template(tpl: dict, ctx: dict) -> dict:
    """Render the area content template for one area.

    Section HTML is emitted with |safe by public/area.html, so its tokens are filled from an HTML-escaped
    copy of the context (partner names come from the partner's own profile form and are hostile until
    escaped). Every other field is autoescaped by Jinja and takes the plain values.
    """
    html_ctx = {k: str(escape(v)) for k, v in ctx.items()}
    out = {}
    for key, value in tpl.items():
        if key == "sections" and isinstance(value, list):
            out[key] = [
                {k: kit.render_deep(v, html_ctx if k == "html" else ctx) for k, v in sec.items()}
                for sec in value if isinstance(sec, dict)
            ]
        else:
            out[key] = kit.render_deep(value, ctx)
    return out


def area_page_content(db: Session, area: Area, page: str) -> dict:
    """Bespoke content for the page if present, else the tokenised template rendered for this area."""
    bespoke = (area.content or {}).get(page)
    if isinstance(bespoke, dict) and bespoke.get("sections"):
        return bespoke
    tpl = content.area_content_template().get(page, {})
    if tpl:
        # The content template expects {{region_law}} as a full legal paragraph; the hero uses the short phrase.
        ctx = dict(area_ctx(db, area))
        ctx["region_law"] = ctx.get("region_law_long", ctx["region_law"])
        return _render_page_template(tpl, ctx)
    return {
        "meta_title": f"{'Letting agents' if page == 'hub' else 'Property management' if page == 'lettings' else 'Short-let management'} in {area.name} | Hello New Home",
        "meta_description": f"Capped-fee lettings and short-let management in {area.name}. No lock-in, no VAT. {area.covers}.",
        "h1": f"Hello New Home in {area.name}",
        "intro": area.demand or "",
        "sections": [],
        "faqs": [],
    }


_CTA_HREF = re.compile(r'href="/(valuation|serviced-accommodation)(#[^"]*)?"')


def _carry_area(pc: dict, slug: str) -> dict:
    """Bespoke section HTML may link to the rent review or short-let pages; keep the visitor's area on those links."""
    sections = []
    for sec in pc.get("sections") or []:
        if not isinstance(sec, dict):
            continue          # a malformed bespoke section is skipped, never a 500 on the public page
        html = sec.get("html")
        html = html if isinstance(html, str) else ""
        if "/valuation" in html or "/serviced-accommodation" in html:
            sec = dict(sec, html=_CTA_HREF.sub(lambda m: f'href="/{m.group(1)}?area={slug}{m.group(2) or ""}"', html))
        sections.append(sec)
    faqs = [f for f in (pc.get("faqs") or []) if isinstance(f, dict)] if isinstance(pc.get("faqs"), list) else []
    return dict(pc, sections=sections, faqs=faqs)


def area_neighbourhoods(area: Area) -> list[dict]:
    items = (area.content or {}).get("neighbourhoods") or []
    if items:
        return items
    return [{"name": p, "blurb": ""} for p in (area.places or [])]


def _jsonld_dumps(data) -> str:
    """Serialise for an inline <script type="application/ld+json">. Angle brackets and ampersands become JSON
    \\u escapes (still valid JSON for the crawler), so no value can close the tag or carry markup."""
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _e164(phone: str) -> str:
    """'01792 807 051' -> '+441792807051' for schema.org telephone fields; other shapes are passed through."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+44" + digits[1:]
    if digits.startswith("44") and len(digits) == 12:
        return "+" + digits
    return (phone or "").strip()


_HQ_ADDRESS = {"@type": "PostalAddress", "addressLocality": "Swansea", "postalCode": "SA7 0AG", "addressCountry": "GB"}


def site_jsonld(s: dict) -> str:
    """Organization + WebSite for the home page."""
    base = config.BASE_URL
    same_as = [u for u in (s.get("facebook_url"), s.get("instagram_url")) if u]
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": f"{base}/#organization",
        "name": "Hello New Home",
        "legalName": s.get("company_name", ""),
        "url": base,
        "logo": f"{base}/assets/favicon.svg",
        "image": f"{base}/assets/og-default.svg",
        "email": s.get("hq_email", ""),
        "telephone": _e164(s.get("hq_phone", "")),
        "address": dict(_HQ_ADDRESS, streetAddress=s.get("hq_address", "")),
        "contactPoint": [{
            "@type": "ContactPoint",
            "contactType": "customer service",
            "telephone": _e164(s.get("hq_phone", "")),
            "email": s.get("hq_email", ""),
            "areaServed": "GB",
            "availableLanguage": "en-GB",
        }],
    }
    if same_as:
        org["sameAs"] = same_as
    website = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "name": "Hello New Home",
        "url": base,
        "inLanguage": "en-GB",
        "publisher": {"@id": f"{base}/#organization"},
    }
    return _jsonld_dumps([org, website])


def jsonld_for(area: Area, page: dict, s: dict, url: str, page_key: str = "hub") -> str:
    """RealEstateAgent + BreadcrumbList (+ FAQPage when the page has FAQs) for an area page."""
    base = config.BASE_URL
    data = [
        {
            "@context": "https://schema.org",
            "@type": "RealEstateAgent",
            "name": f"Hello New Home {area.name}",
            "url": url,
            "telephone": _e164(s["hq_phone"]),
            "email": s["hq_email"],
            "areaServed": [{"@type": "Place", "name": p} for p in (area.places or [])] or area.name,
            "parentOrganization": {"@type": "Organization", "@id": f"{base}/#organization", "name": "Hello New Home", "url": base},
            "address": _HQ_ADDRESS,
            "priceRange": "££",
        }
    ]
    crumbs = [("Home", base), ("Areas", f"{base}/areas"), (area.name, f"{base}/areas/{area.slug}")]
    if page_key == "lettings":
        crumbs.append(("Property management", f"{base}/areas/{area.slug}/lettings"))
    elif page_key == "shortlets":
        crumbs.append(("Short-let management", f"{base}/areas/{area.slug}/short-lets"))
    elif page_key == "commercial":
        crumbs.append(("Commercial lettings", f"{base}/areas/{area.slug}/commercial"))
    data.append({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": href} for i, (name, href) in enumerate(crumbs, start=1)
        ],
    })
    faqs = [f for f in (page.get("faqs") or []) if isinstance(f, dict) and f.get("q")]
    if faqs:
        data.append({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": str(f.get("q", "")),
                            "acceptedAnswer": {"@type": "Answer", "text": str(f.get("a", ""))}} for f in faqs],
        })
    return _jsonld_dumps(data)


# ---- brand pages (fragments) -----------------------------------------------
@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return _fragment_page(request, db, "/")


def fragment(page: str, request: Request, db: Session = Depends(get_db)):
    path = f"/{page}"
    from ..main import PAGES
    if path not in PAGES:
        # tolerate old .html links from the static site
        if page.endswith(".html"):
            stem = page[:-5]
            if stem.startswith("area-"):
                return RedirectResponse(f"/areas/{stem[5:]}", status_code=301)
            return RedirectResponse("/" if stem == "index" else ("/partners-programme" if stem == "partners" else f"/{stem}"), status_code=301)
        raise HTTPException(status_code=404, detail="Page not found")
    return _fragment_page(request, db, path)


def _fragment_page(request: Request, db: Session, path: str) -> HTMLResponse:
    from ..main import PAGES
    page = PAGES[path]
    body = page["body"]
    if "{{AREA_CARDS}}" in body:
        body = body.replace("{{AREA_CARDS}}", area_cards_html(request, db))
    if "{{AREA_OPTIONS}}" in body:
        body = body.replace("{{AREA_OPTIONS}}", area_options_html(db))
    meta = page["meta"]
    title = meta.get("title", "Hello New Home")
    ctx = {
        "db": db,
        "title": title if title.startswith("Hello New Home") else f"{title} | Hello New Home",
        "description": meta.get("description", ""),
        "nav_active": meta.get("nav", ""),
        "body": Markup(body),
        "canonical": f"{config.BASE_URL}{'' if path == '/' else path}",
    }
    if path == "/":
        ctx["jsonld"] = Markup(site_jsonld(settings.get_all(db)))
    return render_public(request, "public/page.html", ctx)


# ---- area pages ------------------------------------------------------------
@router.get("/areas", response_class=HTMLResponse)
def areas_index(request: Request, db: Session = Depends(get_db)):
    return _fragment_page(request, db, "/areas")


@router.get("/areas/{slug}", response_class=HTMLResponse)
def area_hub(slug: str, request: Request, db: Session = Depends(get_db)):
    return _area_page(request, db, slug, "hub")


@router.get("/areas/{slug}/lettings", response_class=HTMLResponse)
def area_lettings(slug: str, request: Request, db: Session = Depends(get_db)):
    return _area_page(request, db, slug, "lettings")


@router.get("/areas/{slug}/short-lets", response_class=HTMLResponse)
def area_shortlets(slug: str, request: Request, db: Session = Depends(get_db)):
    return _area_page(request, db, slug, "shortlets")


# ---- commercial lettings (plan, Phase 5) -----------------------------------
def _commercial_contact(db: Session, area: Area | None, s: dict) -> dict:
    """Who a commercial landlord talks to: the area's partner when they are active and cleared to take commercial
    instructions, otherwise head office. Head office is also the answer with no area at all."""
    hq = {"name": "The head office team", "first": kit.HQ_TEAM, "is_hq": True, "phone": s["hq_phone"],
          "whatsapp": s["hq_whatsapp"], "whatsapp_display": s["hq_whatsapp_display"], "bio": kit.HQ_BIO}
    if area is None:
        return hq
    partner = routing.partner_for(area, commercial=True)
    if partner is None or kit.partner_pending(area):
        return hq
    ctx = kit.context_for(area, hq_whatsapp=s["hq_whatsapp"], hq_whatsapp_display=s["hq_whatsapp_display"],
                          hq_phone=s["hq_phone"], base_url=config.BASE_URL)
    return {"name": ctx["partner"], "first": ctx["partner_first"], "is_hq": False, "phone": ctx["phone"],
            "whatsapp": area.contact_whatsapp(s["hq_whatsapp"]), "whatsapp_display": ctx["whatsapp_display"],
            "bio": ctx["partner_bio"]}


def _commercial_jsonld(area: Area | None, s: dict, url: str) -> str:
    base = config.BASE_URL
    crumbs = [("Home", base), ("Commercial lettings", f"{base}/commercial")]
    if area is not None:
        crumbs = [("Home", base), ("Areas", f"{base}/areas"), (area.name, f"{base}/areas/{area.slug}"),
                  ("Commercial lettings", url)]
    data = [
        {
            "@context": "https://schema.org",
            "@type": "Service",
            "name": f"Commercial property lettings{(' in ' + area.name) if area else ''}",
            "serviceType": "Commercial property letting and management",
            "url": url,
            "areaServed": area.name if area else "England and Wales",
            "provider": {"@type": "Organization", "@id": f"{base}/#organization", "name": "Hello New Home", "url": base},
        },
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i, "name": name, "item": href}
                                for i, (name, href) in enumerate(crumbs, start=1)],
        },
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                           for q, a in COMMERCIAL_FAQS],
        },
    ]
    return _jsonld_dumps(data)


def _commercial_page(request: Request, db: Session, area: Area | None) -> HTMLResponse:
    s = settings.get_all(db)
    url = f"{config.BASE_URL}/commercial" if area is None else f"{config.BASE_URL}/areas/{area.slug}/commercial"
    where = f" in {area.name}" if area else ""
    return render_public(request, "public/commercial.html", {
        "db": db,
        "area": area,
        "contact": _commercial_contact(db, area, s),
        "fees": settings.commercial_fees(s),
        "fees_pending": settings.FEES_PENDING_SENTENCE,
        "faqs": COMMERCIAL_FAQS,
        "uses": COMMERCIAL_USES,
        "title": f"Commercial property lettings{where} | Hello New Home",
        "description": (f"Shops, offices, workshops and units let and managed{where}: appraisal, marketing, heads of terms, "
                        "the lease by your solicitor, rent collection, management and rent reviews. Business leases, "
                        "not residential tenancies."),
        "canonical": url,
        "jsonld": Markup(_commercial_jsonld(area, s, url)),
        "noindex": bool(area) and area.status == "paused",
        "nav_active": "landlords" if area is None else "areas",
        "body_region": area.region if area else "",
        "other_areas": [a for a in public_areas(db) if area is None or a.id != area.id],
    })


@router.get("/commercial", response_class=HTMLResponse)
def commercial(request: Request, db: Session = Depends(get_db)):
    return _commercial_page(request, db, None)


@router.get("/areas/{slug}/commercial", response_class=HTMLResponse)
def area_commercial(slug: str, request: Request, db: Session = Depends(get_db)):
    return _commercial_page(request, db, _area_or_404(db, slug))


def _area_page(request: Request, db: Session, slug: str, page: str) -> HTMLResponse:
    area = _area_or_404(db, slug)
    s = settings.get_all(db)
    pc = _carry_area(area_page_content(db, area, page), area.slug)
    suffix = {"hub": "", "lettings": "/lettings", "shortlets": "/short-lets"}[page]
    url = f"{config.BASE_URL}/areas/{area.slug}{suffix}"
    # Homes to rent from Ordris (plan, Phase 6 C5): the lettings page only, and nothing is asked for or shown
    # while Ordris is unconfigured, so the page is then exactly as it was. fetch_listings never raises.
    listings = (ordris_listings.for_template(ordris_listings.fetch_listings(area.postcodes or []))
                if page == "lettings" and ordris_listings.configured() else [])
    return render_public(request, "public/area.html", {
        "db": db,
        "area": area,
        "page": page,
        "pc": pc,
        "listings": listings,
        "ctx": area_ctx(db, area),
        "neighbourhoods": area_neighbourhoods(area),
        "title": pc.get("meta_title") or f"Hello New Home {area.name}",
        "description": pc.get("meta_description", ""),
        "canonical": url,
        "jsonld": Markup(jsonld_for(area, pc, s, url, page)),
        # Paused areas stay reachable for anyone holding the link but are kept out of search results.
        "noindex": area.status == "paused",
        "nav_active": "areas",
        "body_region": area.region,
        "other_areas": [a for a in public_areas(db) if a.id != area.id],
    })


# ---- dynamic assets & crawl files -----------------------------------------
def areas_payload(db: Session) -> list[dict]:
    """The public areas list as the browser sees it (window.HNH_AREAS and GET /api/areas)."""
    s = settings.get_all(db)
    data = []
    for a in public_areas(db):
        data.append({
            "slug": a.slug, "name": a.name, "region": a.region, "role": a.role, "label": a.label,
            "partner": kit.partner_name(a), "partner_first": kit.partner_first(a), "status": a.status,
            "phone": s["hq_phone"], "whatsapp": a.contact_whatsapp(s["hq_whatsapp"]),
            "whatsapp_display": a.contact_whatsapp_display(s["hq_whatsapp_display"]),
            "postcodes": a.postcodes or [], "covers": a.covers,
            "pages": {"hub": f"/areas/{a.slug}", "lettings": f"/areas/{a.slug}/lettings",
                      "short_lets": f"/areas/{a.slug}/short-lets", "commercial": f"/areas/{a.slug}/commercial"},
        })
    return data


@router.get("/areas.js", include_in_schema=False)
def areas_js(db: Session = Depends(get_db)):
    body = "window.HNH_AREAS = " + json.dumps(areas_payload(db), ensure_ascii=False).replace("</", "<\\/") + ";\n"
    return Response(body, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@router.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe for the platform health check. No database work, so a slow disk never fails it."""
    return {"ok": True}


# ---- uploads (never served from the directory directly) -------------------
def _upload_path(rel: str) -> Path | None:
    """Absolute path for a stored relative upload path, or None if it is empty or escapes UPLOAD_DIR."""
    if not rel:
        return None
    root = config.UPLOAD_DIR.resolve()
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    return target if root in target.parents else None


@router.get("/uploads/photos/{name}", include_in_schema=False)
def partner_photo(name: str):
    """Public partner photo, as shown on the area pages. Image types only; 404 for anything else."""
    if "/" in name or "\\" in name or ".." in name or not _PHOTO_NAME.match(name):
        raise HTTPException(status_code=404, detail="Not found")
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in PHOTO_TYPES:
        raise HTTPException(status_code=404, detail="Not found")
    folder = (config.UPLOAD_DIR / "photos").resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=PHOTO_TYPES[ext], headers={"Cache-Control": "public, max-age=86400"})


def _resource_visible_to(db: Session, user: User, res: Resource) -> bool:
    if user.role == "admin":
        return True
    if res.is_placeholder:
        return False
    if res.visibility == "all":
        return True
    if not user.partner_id:
        return False
    areas = db.query(Area).filter(Area.partner_id == user.partner_id, Area.status != "hidden").all()
    if res.visibility == "region":
        return bool(res.region) and res.region in {a.region for a in areas}
    if res.visibility == "area":
        return res.area_id in {a.id for a in areas}
    return False


@router.get("/files/{resource_id}", include_in_schema=False)
def resource_file(resource_id: int, db: Session = Depends(get_db), user: User = Depends(require_partner)):
    """Canonical download for a resource library file. Login required; partners only get what is visible to them."""
    res = db.get(Resource, resource_id)
    if not res or not res.file_path or not _resource_visible_to(db, user, res):
        raise HTTPException(status_code=404, detail="File not found")
    path = _upload_path(res.file_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    prefix = f"{res.id}-"
    original = path.name[len(prefix):] if path.name.startswith(prefix) else path.name
    return FileResponse(path, filename=original or path.name, headers={"Cache-Control": "private, max-age=0"})


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(db: Session = Depends(get_db)):
    from ..main import PAGES
    urls = [f"{config.BASE_URL}{'' if p == '/' else p}" for p in PAGES] + [f"{config.BASE_URL}/commercial"]
    for a in public_areas(db):
        urls += [f"{config.BASE_URL}/areas/{a.slug}", f"{config.BASE_URL}/areas/{a.slug}/lettings",
                 f"{config.BASE_URL}/areas/{a.slug}/short-lets", f"{config.BASE_URL}/areas/{a.slug}/commercial"]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "".join(f"  <url><loc>{escape(u)}</loc></url>\n" for u in urls) + "</urlset>\n"
    return Response(xml, media_type="application/xml")


@router.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /partners\nDisallow: /api/\nSitemap: {config.BASE_URL}/sitemap.xml\n")


# ---- API -------------------------------------------------------------------
@router.get("/api/areas")
def api_areas(db: Session = Depends(get_db)):
    """Public areas list, the same rows as /areas.js, for the finder when that script has not loaded."""
    return JSONResponse(areas_payload(db), headers={"Cache-Control": "public, max-age=300"})


@router.get("/api/locate")
def api_locate(postcode: str = "", db: Session = Depends(get_db)):
    loc = locate(db, postcode)
    if not loc:
        return {"ok": False, "error": "Not a UK postcode"}
    s = settings.get_all(db)
    a = loc.area
    return {
        "ok": True, "outward": loc.outward, "region": loc.region, "covered": loc.covered,
        "area": None if not a else {
            "slug": a.slug, "name": a.name, "status": a.status, "label": a.label, "partner": kit.partner_name(a),
            "whatsapp": a.contact_whatsapp(s["hq_whatsapp"]), "whatsapp_display": a.contact_whatsapp_display(s["hq_whatsapp_display"]),
        },
    }


def _clip(value, limit: int = LEAD_MAX_VALUE):
    """A payload value cut down to something a lead can plausibly need; nested structures are flattened to text."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value)[:limit]


def _lead_payload(data: dict) -> dict:
    out: dict = {}
    for k, v in data.items():
        key = str(k)
        if key.startswith("_"):
            continue
        out[key[:LEAD_MAX_KEY]] = _clip(v)
        if len(out) >= LEAD_MAX_FIELDS:
            break
    return out


async def _lead_data(request: Request) -> dict:
    """The posted lead as a dict, from JSON or a form, with the body capped before it is parsed."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > LEAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Request too large")
    raw = await request.body()
    if len(raw) > LEAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Request too large")
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad payload")
    else:
        form = await request.form()
        data = {k: v for k, v in form.items() if not hasattr(v, "filename")}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Bad payload")
    return data


@router.post("/api/lead")
async def api_lead(request: Request, db: Session = Depends(get_db)):
    """Accepts JSON (from site.js) or a form post. Stores the lead, routes it, notifies HQ."""
    data = await _lead_data(request)
    # Honeypot: bots fill every field.
    if data.get("website"):
        return {"ok": True}
    rate_key = f"lead:{client_ip(request)}"
    if not throttle_check(rate_key, LEAD_RATE_LIMIT, LEAD_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many enquiries from this connection. Try again later.")
    subject = str(data.get("_subject", ""))
    kind = str(data.get("kind") or next((k for k, v in KIND_SUBJECTS.items() if v == subject), "enquiry"))
    if kind not in PUBLIC_KINDS:
        kind = "enquiry"
    postcode = str(data.get("postcode") or extract_postcode(str(data.get("address", ""))[:300]) or "")[:12]
    loc = locate(db, postcode) if postcode else None
    area = loc.area if loc else None
    if not area and data.get("area"):
        # The hidden area field (and old links) may only route to an area the public site lists.
        area = db.query(Area).filter(Area.slug == str(data["area"])[:60], Area.status.in_(("active", "coming"))).first()
    name = str(data.get("name", ""))[:120]
    if not name and kind != "alert":
        raise HTTPException(status_code=422, detail="Name is required")
    commercial = kind == routing.COMMERCIAL_LEAD_KIND
    payload = _lead_payload(data)
    if commercial:
        payload.update(_commercial_fields(data))
    lead = Lead(
        kind=kind, name=name, phone=str(data.get("phone", ""))[:40], email=str(data.get("email", ""))[:200],
        postcode=(loc.outward if loc else postcode[:12]), address=str(data.get("address", ""))[:300],
        region=(loc.region if loc else ""), area_id=area.id if area else None,
        # The one routing rule (services.routing): a head-office area has no partner, and a commercial appraisal stays
        # with head office unless the area's partner is cleared to take it.
        partner_id=routing.partner_id_for(area, commercial=commercial),
        payload=payload,
        source_page=str(data.get("_page", ""))[:200],
    )
    db.add(lead)
    db.commit()
    throttle_hit(rate_key)
    s = settings.get_all(db)
    _notify(lead, area, s)
    return {
        "ok": True, "id": lead.id,
        "area": None if not area else {"slug": area.slug, "name": area.name, "partner": kit.partner_name(area),
                                       "whatsapp": area.contact_whatsapp(s["hq_whatsapp"])},
    }


def _notify(lead: Lead, area: Area | None, s: dict) -> None:
    if not mail.configured():
        log.info("lead %s (%s) stored; SMTP not configured", lead.id, lead.kind)
        return
    commercial = routing.is_commercial_lead(lead)
    if commercial:
        # The kind label says commercial; the subject never carries the landlord's or the business's name.
        subject = f"[HNH lead] {KIND_SUBJECTS[lead.kind]} ({lead.postcode or 'no postcode'})"
    else:
        subject = f"[HNH lead] {KIND_SUBJECTS.get(lead.kind, lead.kind)}: {lead.name} ({lead.postcode or 'no postcode'})"
    lines = [f"{k}: {v}" for k, v in (lead.payload or {}).items()]
    lines.append(f"Area: {area.name if area else 'not covered'} ({lead.region})")
    lines.append(f"Routed to: {lead.partner.name if lead.partner else 'head office'}")
    lines.append(f"Admin: {config.BASE_URL}/admin/leads/{lead.id}")
    if not mail.send(subject, "\n".join(lines), mail.notify_recipients(area, commercial=commercial)):
        log.warning("lead %s stored but the notification was not sent", lead.id)
