"""Admin panel: areas, partners, leads, resources, announcements, settings, territory, users, audit log.

Conventions: every route sits behind require_admin; writes call audit() then commit; POSTs redirect
with status 303 and carry a flash message in ?ok= or ?err=; forms that fail validation re-render
with the submitted values and a 400 so nothing typed is lost.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from .. import config
from ..auth import (audit, check_origin, clear_session, current_user, generate_password, hash_password, login_allowed,
                    require_admin, safe_next, set_session, throttle_hit, touch_login, verify_password)
from ..db import get_db
from ..models import Announcement, Area, AuditLog, Lead, Partner, Resource, User
from ..services import settings
from ..services.postcodes import normalise as normalise_postcode, region_for
from ..services.times import fmt_uk

router = APIRouter(prefix="/admin", dependencies=[Depends(check_origin)])

# ---- vocabularies ----------------------------------------------------------
AREA_REGIONS = ("england", "wales")
AREA_ROLES = ("hq", "partner")
AREA_STATUSES = ("active", "coming", "paused", "hidden")
PARTNER_TIERS = ("introducer", "area")
PARTNER_STATUSES = ("pending", "active", "paused", "left")
LEAD_STATUSES = ("new", "contacted", "booked", "instructed", "lost")
LEAD_KINDS = ("rent_review", "sa_appraisal", "contact", "alert", "partner_application", "enquiry", "partner_request")
LEAD_KIND_LABELS = {
    "rent_review": "Rent review", "sa_appraisal": "Short-let appraisal", "contact": "Contact", "alert": "Property alert",
    "partner_application": "Partner application", "enquiry": "Enquiry", "partner_request": "Partner change request",
}
RESOURCE_KINDS = ("file", "link", "page")
RESOURCE_VISIBILITY = ("all", "region", "area")
RESOURCE_EXTS = {"pdf", "docx", "xlsx", "pptx", "png", "jpg", "jpeg", "mp4", "zip"}
PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}
CONTENT_KEYS = ("hub", "lettings", "shortlets", "neighbourhoods")
CONTENT_OPTIONAL_KEYS = ("sources", "slug")
PAGE_SIZE = 50

POSTCODE_RE = re.compile(r"^[A-Z]{1,2}(\d[A-Z\d]?)?$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]")


# ---- rendering and small helpers ------------------------------------------
def render_admin(request: Request, template: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    from ..main import templates
    db = ctx.pop("db", None) or next(get_db())
    base = {"request": request, "s": settings.get_all(db), "section": ctx.get("section", "")}
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def _flash(request: Request) -> dict:
    """Flash message carried in the query string by the redirect after a POST."""
    err = request.query_params.get("err", "")
    ok = request.query_params.get("ok", "")
    if err:
        return {"flash": err[:400], "flash_kind": "err"}
    if ok:
        return {"flash": ok[:400], "flash_kind": "ok"}
    return {}


def _go(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    if ok or err:
        path += ("&" if "?" in path else "?") + urlencode({"err": err} if err else {"ok": ok})
    return RedirectResponse(path, status_code=303)


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


def _s(form: FormData, key: str, default: str = "", limit: int = 4000) -> str:
    """A trimmed string form field; uploads and missing keys read as the default."""
    v = form.get(key)
    if not isinstance(v, str):
        return default
    return v.strip()[:limit]


def _int(raw, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _slugify(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:60]


def _whatsapp(raw: str) -> tuple[str, str]:
    """International digits for wa.me. Returns (digits, error); blank input is allowed."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return "", ""
    if digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]
    if not digits.startswith("44") or not 11 <= len(digits) <= 13:
        return "", "WhatsApp numbers must be digits starting 44, for example 447700900123."
    return digits, ""


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _like(term: str) -> str:
    """Escape LIKE wildcards in user input so a search for 100% means 100%."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---- uploads ---------------------------------------------------------------
def _safe_name(filename: str) -> str:
    """Strip any client path and keep a conservative character set; the extension is lower-cased."""
    name = Path((filename or "").replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    def clean(part: str) -> str:
        return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", part)).strip("-.")

    stem = clean(stem)[:70] or "file"
    ext = clean(ext).lower()[:10]
    return f"{stem}.{ext}" if ext else stem


def _upload_name_ext(upload: UploadFile) -> tuple[str, str]:
    name = _safe_name(upload.filename or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return name, ext


def _has_upload(value) -> bool:
    return isinstance(value, UploadFile) and bool(value.filename)


def _upload_path(rel: str) -> Path | None:
    """Absolute path for a stored relative path, or None if it escapes UPLOAD_DIR."""
    if not rel:
        return None
    root = config.UPLOAD_DIR.resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        return None
    return target


def _remove_file(rel: str) -> None:
    target = _upload_path(rel)
    if target and target.is_file():
        target.unlink()


_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"\xff\xd8\xff", "jpeg"))


def _save_upload(upload: UploadFile, rel: str, allowed: set[str], *, image: bool = False) -> str:
    """Stream an upload to UPLOAD_DIR/rel. Returns an error message, or '' on success."""
    name, ext = _upload_name_ext(upload)
    if ext not in allowed:
        return f"{name} is not an allowed file type. Use {', '.join(sorted(allowed))}."
    dest = _upload_path(rel)
    if dest is None:
        return "Could not store the file."
    dest.parent.mkdir(parents=True, exist_ok=True)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    head = b""
    try:
        upload.file.seek(0)
        with tmp.open("wb") as out:
            while True:
                chunk = upload.file.read(256 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > limit:
                    return f"{name} is larger than {config.MAX_UPLOAD_MB} MB."
                out.write(chunk)
        if size == 0:
            return f"{name} is empty."
        if image:
            ok = head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            if not ok:
                return f"{name} does not look like a PNG, JPEG or WebP image."
        tmp.replace(dest)
        return ""
    finally:
        tmp.unlink(missing_ok=True)


# ---- auth ------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/admin", db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    next = safe_next(next, "/admin")
    if user and user.role == "admin":
        return RedirectResponse(next, status_code=303)
    return render_admin(request, "admin/login.html", {"db": db, "next": next, "error": ""})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin"), db: Session = Depends(get_db)):
    norm, keys, allowed = login_allowed(request, email)
    if not allowed:
        return render_admin(request, "admin/login.html", {"db": db, "next": next, "error": "Too many attempts. Try again in 15 minutes."}, 429)
    user = db.query(User).filter(User.email == norm, User.role == "admin").first()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        for key in keys:
            throttle_hit(key)
        return render_admin(request, "admin/login.html", {"db": db, "next": next, "error": "Email or password not recognised."}, 401)
    touch_login(db, user)
    audit(db, user, "login", "admin")
    db.commit()
    resp = RedirectResponse(safe_next(next, "/admin"), status_code=303)
    set_session(resp, user)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    clear_session(resp)
    return resp


# ---- dashboard -------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    stats = {
        "areas_active": db.query(Area).filter(Area.status == "active").count(),
        "areas_coming": db.query(Area).filter(Area.status == "coming").count(),
        "partners_active": db.query(Partner).filter(Partner.status == "active").count(),
        "partners_pending": db.query(Partner).filter(Partner.status == "pending").count(),
        "leads_new": db.query(Lead).filter(Lead.status == "new").count(),
        "leads_total": db.query(Lead).count(),
        "leads_uncovered": db.query(Lead).filter(Lead.area_id.is_(None)).count(),
        "resources": db.query(Resource).count(),
        "resources_placeholder": db.query(Resource).filter(Resource.is_placeholder.is_(True)).count(),
    }
    recent = db.query(Lead).order_by(Lead.created_at.desc(), Lead.id.desc()).limit(10).all()
    current = settings.get_all(db)
    placeholders = _setting_placeholders(current)
    hq_hours, hq_cutoff = _hq_sla(current)
    stats["leads_hq_late"] = _hq_leads(db).filter(Lead.status == "new", Lead.created_at < hq_cutoff).count()
    ctx = {"db": db, "user": user, "stats": stats, "recent": recent, "placeholders": placeholders, "section": "dashboard",
           "hq_hours": hq_hours, "hq_cutoff": hq_cutoff}
    ctx.update(_flash(request))
    return render_admin(request, "admin/dashboard.html", ctx)


# ============================================================================
# 1. Areas
# ============================================================================
def _parse_postcodes(raw: str) -> tuple[list[str], str]:
    codes: list[str] = []
    bad: list[str] = []
    for tok in re.split(r"[,;\s]+", (raw or "").upper()):
        tok = tok.strip()
        if not tok:
            continue
        if not POSTCODE_RE.match(tok):
            bad.append(tok)
        elif tok not in codes:
            codes.append(tok)
    if bad:
        return codes, (f"Not valid postcode districts: {', '.join(bad[:12])}. "
                       "Use outward codes such as DH1 or SP10, or a whole postcode area such as SE.")
    return codes, ""


def _postcode_clashes(db: Session, codes: list[str], exclude_id: int | None) -> str:
    q = db.query(Area)
    if exclude_id is not None:
        q = q.filter(Area.id != exclude_id)
    owner: dict[str, str] = {}
    for a in q.all():
        for c in a.postcodes or []:
            owner.setdefault(c, a.name)
    clashes = [f"{c} is already in {owner[c]}" for c in codes if c in owner]
    if clashes:
        return "Postcode clash: " + "; ".join(clashes) + ". Remove it from that area first."
    return ""


def _postcode_nesting(db: Session, codes: list[str], exclude_id: int | None) -> str:
    """A note (not an error) when a district sits inside a whole postcode area another area lists, or the
    reverse. The exact district wins at routing time (services.postcodes.locate), so the overlap is allowed,
    but it must be a conscious choice rather than a surprise on the territory page."""
    q = db.query(Area)
    if exclude_id is not None:
        q = q.filter(Area.id != exclude_id)
    others = [(c, a.name) for a in q.all() for c in (a.postcodes or [])]
    notes: list[str] = []
    for c in codes:
        letters = re.match(r"^[A-Z]{1,2}", c).group(0)
        if c.isalpha():
            inside = sorted({f"{oc} ({name})" for oc, name in others if not oc.isalpha() and oc.startswith(c)
                             and re.match(r"^[A-Z]{1,2}", oc).group(0) == c})
            if inside:
                notes.append(f"{c} covers the whole postcode area, but {', '.join(inside)} will keep their own routing")
        else:
            parent = next((name for oc, name in others if oc == letters), None)
            if parent:
                notes.append(f"{c} sits inside the whole area {letters} listed for {parent}; the exact district wins")
    return ("Note: " + "; ".join(notes) + ".") if notes else ""


def _parse_content(raw: str) -> tuple[dict, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"Content is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}."
    if not isinstance(data, dict):
        return {}, "Content must be a JSON object with the keys hub, lettings, shortlets and neighbourhoods."
    unknown = [k for k in data if k not in CONTENT_KEYS + CONTENT_OPTIONAL_KEYS]
    if unknown:
        return {}, (f"Unexpected content key{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
                    "Expected hub, lettings, shortlets and neighbourhoods (sources is optional).")
    for key in ("hub", "lettings", "shortlets"):
        page = data.get(key)
        if page is None:
            continue
        if not isinstance(page, dict):
            return {}, f"Content key {key} must be an object with meta_title, meta_description, h1, intro, sections and faqs."
        if "sections" in page and not isinstance(page["sections"], list):
            return {}, f"Content {key}.sections must be a list."
        for i, sec in enumerate(page.get("sections") or [], start=1):
            if not isinstance(sec, dict) or not isinstance(sec.get("h2", ""), str) or not isinstance(sec.get("html", ""), str):
                return {}, f"Content {key}.sections item {i} must be an object with text h2 and html."
        if "faqs" in page and not isinstance(page["faqs"], list):
            return {}, f"Content {key}.faqs must be a list."
        for i, faq in enumerate(page.get("faqs") or [], start=1):
            if not isinstance(faq, dict) or not isinstance(faq.get("q"), str) or not isinstance(faq.get("a"), str):
                return {}, f"Content {key}.faqs item {i} must be an object with text q and a."
        for field in ("meta_title", "meta_description", "h1", "intro"):
            if field in page and not isinstance(page[field], str):
                return {}, f"Content {key}.{field} must be text."
    if "neighbourhoods" in data and not isinstance(data["neighbourhoods"], list):
        return {}, "Content key neighbourhoods must be a list of {name, blurb} objects."
    for i, n in enumerate(data.get("neighbourhoods") or [], start=1):
        if not isinstance(n, dict) or not isinstance(n.get("name", ""), str) or not isinstance(n.get("blurb", ""), str):
            return {}, f"Content neighbourhoods item {i} must be an object with text name and blurb."
    return data, ""


def _area_vals(area: Area | None) -> dict:
    if area is None:
        return {"slug": "", "name": "", "region": "england", "role": "partner", "status": "coming", "partner_id": "",
                "covers": "", "postcodes": "", "places": "", "demand": "", "licensing": "", "shortlet": "",
                "rent_avg": "", "rent_change": "", "rent_note": "", "phone": "", "whatsapp": "", "whatsapp_display": "",
                "sort": 100, "content": ""}
    return {
        "slug": area.slug, "name": area.name, "region": area.region, "role": area.role, "status": area.status,
        "partner_id": area.partner_id or "", "covers": area.covers, "postcodes": ", ".join(area.postcodes or []),
        "places": "\n".join(area.places or []), "demand": area.demand, "licensing": area.licensing,
        "shortlet": area.shortlet, "rent_avg": area.rent_avg, "rent_change": area.rent_change, "rent_note": area.rent_note,
        "phone": area.phone, "whatsapp": area.whatsapp, "whatsapp_display": area.whatsapp_display, "sort": area.sort,
        "content": json.dumps(area.content, indent=2, ensure_ascii=False) if area.content else "",
    }


def _area_vals_from_form(form: FormData) -> dict:
    vals = {k: _s(form, k) for k in ("slug", "name", "region", "role", "status", "partner_id", "covers", "postcodes",
                                      "places", "demand", "licensing", "shortlet", "rent_avg", "rent_change",
                                      "rent_note", "phone", "whatsapp", "whatsapp_display")}
    vals["sort"] = _int(_s(form, "sort"), 100)
    vals["content"] = _s(form, "content", limit=400_000)
    return vals


def _area_form(request: Request, db: Session, vals: dict, area: Area | None, *, error: str = "", status: int = 200) -> HTMLResponse:
    partners = db.query(Partner).order_by(Partner.name).all()
    ctx = {
        "db": db, "section": "areas", "vals": vals, "area": area,
        "partner_options": [("", "No partner yet")] + [(p.id, f"{p.name} ({p.status})") for p in partners],
        "lead_count": db.query(Lead).filter(Lead.area_id == area.id).count() if area else 0,
        "resource_count": db.query(Resource).filter(Resource.area_id == area.id).count() if area else 0,
    }
    if error:
        ctx.update(flash=error, flash_kind="err")
    else:
        ctx.update(_flash(request))
    return render_admin(request, "admin/area_form.html", ctx, status)


@router.get("/areas", response_class=HTMLResponse)
def areas_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    ctx = {"db": db, "areas": areas, "section": "areas"}
    ctx.update(_flash(request))
    return render_admin(request, "admin/areas.html", ctx)


@router.get("/areas/new", response_class=HTMLResponse)
def area_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _area_form(request, db, _area_vals(None), None)


@router.get("/areas/{area_id}", response_class=HTMLResponse)
def area_edit(area_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    area = _get_or_404(db, Area, area_id)
    return _area_form(request, db, _area_vals(area), area)


async def _save_area(request: Request, db: Session, user: User, area: Area | None):
    form = await request.form()
    vals = _area_vals_from_form(form)
    errors: list[str] = []

    name = vals["name"][:120]
    if not name:
        errors.append("Name is required.")
    slug = vals["slug"] or _slugify(name)
    vals["slug"] = slug
    if not SLUG_RE.match(slug or ""):
        errors.append("Slug may only contain lower-case letters, numbers and single hyphens.")
    else:
        q = db.query(Area).filter(Area.slug == slug)
        if area is not None:
            q = q.filter(Area.id != area.id)
        if q.first() is not None:
            errors.append(f"The slug {slug} is already used by another area.")
    if vals["region"] not in AREA_REGIONS:
        errors.append("Choose a region.")
    if vals["role"] not in AREA_ROLES:
        errors.append("Choose who runs the area.")
    if vals["status"] not in AREA_STATUSES:
        errors.append("Choose a status.")

    partner_id: int | None = None
    if vals["partner_id"]:
        partner = db.get(Partner, _int(vals["partner_id"], -1))
        if partner is None:
            errors.append("That partner no longer exists.")
        else:
            partner_id = partner.id
    if vals["role"] == "hq":
        partner_id = None

    postcodes, err = _parse_postcodes(vals["postcodes"])
    if err:
        errors.append(err)
    elif postcodes:
        clash = _postcode_clashes(db, postcodes, area.id if area else None)
        if clash:
            errors.append(clash)
    places = [p.strip()[:120] for p in vals["places"].splitlines() if p.strip()]
    content, err = _parse_content(vals["content"])
    if err:
        errors.append(err)

    phone = whatsapp = whatsapp_display = ""
    if vals["role"] == "hq":
        phone = vals["phone"][:40]
        whatsapp, err = _whatsapp(vals["whatsapp"])
        if err:
            errors.append(err)
        whatsapp_display = vals["whatsapp_display"][:30]

    if errors:
        return _area_form(request, db, vals, area, error=" ".join(errors), status=400)

    action = "area.update"
    if area is None:
        area = Area(slug=slug, name=name)
        db.add(area)
        action = "area.create"
    area.slug, area.name = slug, name
    area.region, area.role, area.status = vals["region"], vals["role"], vals["status"]
    area.partner_id = partner_id
    area.covers = vals["covers"]
    area.postcodes = postcodes
    area.places = places
    area.demand, area.licensing, area.shortlet = vals["demand"], vals["licensing"], vals["shortlet"]
    area.rent_avg, area.rent_change, area.rent_note = vals["rent_avg"][:20], vals["rent_change"][:20], vals["rent_note"]
    area.phone, area.whatsapp, area.whatsapp_display = phone, whatsapp, whatsapp_display
    area.content = content
    area.sort = vals["sort"]
    db.add(area)
    db.flush()
    audit(db, user, action, f"area:{area.id}",
          f"{area.name} ({area.slug}); status {area.status}; {len(postcodes)} postcode districts; partner {partner_id or 'none'}")
    db.commit()

    nesting = _postcode_nesting(db, postcodes, area.id) if postcodes else ""
    if _s(form, "action") == "view":
        if area.is_public and not nesting:
            return _go(f"/areas/{area.slug}")
        if area.is_public:
            return _go(f"/admin/areas/{area.id}", ok=f"Area saved. {nesting}")
        return _go(f"/admin/areas/{area.id}", ok=f"Area saved. The live page is not public while the status is {area.status}. {nesting}".rstrip())
    return _go(f"/admin/areas/{area.id}", ok=f"Area saved. {nesting}".rstrip())


@router.post("/areas/new", response_class=HTMLResponse)
async def area_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return await _save_area(request, db, user, None)


@router.post("/areas/{area_id}", response_class=HTMLResponse)
async def area_update(area_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    area = _get_or_404(db, Area, area_id)
    return await _save_area(request, db, user, area)


@router.post("/areas/{area_id}/status")
def area_status(area_id: int, status: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    area = _get_or_404(db, Area, area_id)
    if status not in AREA_STATUSES:
        return _go("/admin/areas", err="Unknown status.")
    before = area.status
    area.status = status
    db.add(area)
    audit(db, user, "area.status", f"area:{area.id}", f"{area.name}: {before} to {status}")
    db.commit()
    return _go("/admin/areas", ok=f"{area.name} is now {status}.")


@router.post("/areas/{area_id}/delete")
def area_delete(area_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    area = _get_or_404(db, Area, area_id)
    leads = db.query(Lead).filter(Lead.area_id == area.id).count()
    if leads:
        return _go(f"/admin/areas/{area.id}",
                   err=f"{leads} lead{'s' if leads != 1 else ''} reference {area.name}, so it cannot be deleted. Set its status to hidden instead.")
    resources = db.query(Resource).filter(Resource.area_id == area.id).count()
    if resources:
        return _go(f"/admin/areas/{area.id}",
                   err=f"{resources} resource{'s' if resources != 1 else ''} are limited to {area.name}. Change their visibility first, or set the area to hidden.")
    name, slug = area.name, area.slug
    db.delete(area)
    audit(db, user, "area.delete", f"area:{area_id}", f"{name} ({slug})")
    db.commit()
    return _go("/admin/areas", ok=f"{name} deleted.")


# ============================================================================
# 2. Partners
# ============================================================================
def _partner_vals(p: Partner | None) -> dict:
    if p is None:
        return {"name": "", "first_name": "", "email": "", "phone": "", "whatsapp": "", "whatsapp_display": "", "bio": "",
                "tier": "area", "status": "pending", "units_managed": 0, "notes": "", "areas": []}
    return {"name": p.name, "first_name": p.first_name, "email": p.email, "phone": p.phone, "whatsapp": p.whatsapp,
            "whatsapp_display": p.whatsapp_display, "bio": p.bio, "tier": p.tier, "status": p.status,
            "units_managed": p.units_managed, "notes": p.notes, "areas": [a.id for a in p.areas]}


def _partner_vals_from_form(form: FormData) -> dict:
    vals = {k: _s(form, k) for k in ("name", "first_name", "email", "phone", "whatsapp", "whatsapp_display", "bio",
                                      "tier", "status", "notes")}
    vals["units_managed"] = _int(_s(form, "units_managed"), 0)
    vals["areas"] = [_int(v, -1) for v in form.getlist("areas") if isinstance(v, str)]
    return vals


def _partner_form(request: Request, db: Session, vals: dict, partner: Partner | None, *, error: str = "", status: int = 200) -> HTMLResponse:
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    logins = list(partner.users) if partner else []
    ctx = {"db": db, "section": "partners", "vals": vals, "partner": partner, "areas": areas, "logins": logins,
           "tiers": PARTNER_TIERS, "statuses": PARTNER_STATUSES}
    if error:
        ctx.update(flash=error, flash_kind="err")
    else:
        ctx.update(_flash(request))
    return render_admin(request, "admin/partner_form.html", ctx, status)


@router.get("/partners", response_class=HTMLResponse)
def partners_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partners = db.query(Partner).order_by(Partner.name).all()
    ctx = {"db": db, "partners": partners, "section": "partners"}
    ctx.update(_flash(request))
    return render_admin(request, "admin/partners.html", ctx)


@router.get("/partners/new", response_class=HTMLResponse)
def partner_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _partner_form(request, db, _partner_vals(None), None)


@router.get("/partners/{partner_id}", response_class=HTMLResponse)
def partner_edit(partner_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    return _partner_form(request, db, _partner_vals(partner), partner)


async def _save_partner(request: Request, db: Session, user: User, partner: Partner | None):
    form = await request.form()
    vals = _partner_vals_from_form(form)
    errors: list[str] = []
    if not vals["name"]:
        errors.append("Name is required.")
    email = vals["email"].lower()[:200]
    vals["email"] = email
    if email and not EMAIL_RE.match(email):
        errors.append("Email address does not look right.")
    whatsapp, err = _whatsapp(vals["whatsapp"])
    if err:
        errors.append(err)
    if vals["tier"] not in PARTNER_TIERS:
        errors.append("Choose a tier.")
    if vals["status"] not in PARTNER_STATUSES:
        errors.append("Choose a status.")
    if vals["units_managed"] < 0:
        errors.append("Units managed cannot be negative.")
    area_ids = [i for i in vals["areas"] if i > 0]
    selected = db.query(Area).filter(Area.id.in_(area_ids)).all() if area_ids else []
    if len(selected) != len(set(area_ids)):
        errors.append("One of the chosen areas no longer exists.")

    photo = form.get("photo")
    photo_ext = ""
    if _has_upload(photo):
        _, photo_ext = _upload_name_ext(photo)
        if photo_ext not in PHOTO_EXTS:
            errors.append("Photo must be a JPG, PNG or WebP image.")
    if errors:
        return _partner_form(request, db, vals, partner, error=" ".join(errors), status=400)

    action = "partner.update"
    if partner is None:
        partner = Partner(name=vals["name"][:120])
        db.add(partner)
        action = "partner.create"
    partner.name = vals["name"][:120]
    partner.first_name = vals["first_name"][:60]
    partner.email = email
    partner.phone = vals["phone"][:40]
    partner.whatsapp = whatsapp
    partner.whatsapp_display = vals["whatsapp_display"][:30]
    partner.bio = vals["bio"]
    partner.tier = vals["tier"]
    partner.status = vals["status"]
    partner.units_managed = vals["units_managed"]
    partner.notes = vals["notes"]
    db.add(partner)
    db.flush()

    # Areas: unassign the ones no longer chosen, assign the chosen ones (moving them from any other partner).
    chosen = {a.id for a in selected}
    for a in db.query(Area).filter(Area.partner_id == partner.id).all():
        if a.id not in chosen:
            a.partner_id = None
            db.add(a)
    for a in selected:
        if a.partner_id != partner.id:
            a.partner_id = partner.id
            db.add(a)

    # Photo: remove, or replace (stored as photos/<partner_id>.<ext>, served by GET /uploads/photos/<name>).
    if form.get("remove_photo") == "1" and partner.photo_path:
        _remove_file(partner.photo_path)
        partner.photo_path = ""
    if _has_upload(photo):
        rel = f"photos/{partner.id}.{photo_ext}"
        err = _save_upload(photo, rel, PHOTO_EXTS, image=True)
        if err:
            db.rollback()
            return _partner_form(request, db, vals, partner if action == "partner.update" else None, error=err, status=400)
        if partner.photo_path and partner.photo_path != rel:
            _remove_file(partner.photo_path)
        partner.photo_path = rel
        db.add(partner)

    audit(db, user, action, f"partner:{partner.id}",
          f"{partner.name}; status {partner.status}; tier {partner.tier}; areas {', '.join(a.name for a in selected) or 'none'}")
    db.commit()
    return _go(f"/admin/partners/{partner.id}", ok="Partner saved.")


@router.post("/partners/new", response_class=HTMLResponse)
async def partner_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return await _save_partner(request, db, user, None)


@router.post("/partners/{partner_id}", response_class=HTMLResponse)
async def partner_update(partner_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    return await _save_partner(request, db, user, partner)


def _credentials(request: Request, db: Session, *, title: str, intro: str, email: str, password: str,
                 login_url: str, back_url: str, back_label: str, section: str) -> HTMLResponse:
    resp = render_admin(request, "admin/credentials.html", {
        "db": db, "title": title, "intro": intro, "email": email, "password": password, "login_url": login_url,
        "back_url": back_url, "back_label": back_label, "section": section,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _partner_login(partner: Partner) -> User | None:
    for u in partner.users:
        if u.role == "partner":
            return u
    return None


@router.post("/partners/{partner_id}/login", response_class=HTMLResponse)
def partner_login_create(partner_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    back = f"/admin/partners/{partner.id}"
    email = (partner.email or "").lower()
    if not email:
        return _go(back, err="Add the partner's email address and save before creating a login.")
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        if existing.partner_id == partner.id:
            return _go(back, err="This partner already has a login. Use Reset password instead.")
        return _go(back, err="That email address already belongs to another login.")
    password = generate_password(12)
    login = User(email=email, password_hash=hash_password(password), role="partner", partner_id=partner.id,
                 is_active=True, must_change_password=True)
    db.add(login)
    audit(db, user, "partner.login_create", f"partner:{partner.id}", email)
    db.commit()
    return _credentials(request, db, title="Partner login created",
                        intro=f"Send these to {partner.display_first or partner.name} by a private channel. "
                              "They will be asked to choose their own password the first time they log in.",
                        email=email, password=password, login_url="/partners/login", back_url=back,
                        back_label=partner.name, section="partners")


@router.post("/partners/{partner_id}/reset", response_class=HTMLResponse)
def partner_login_reset(partner_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    back = f"/admin/partners/{partner.id}"
    login = _partner_login(partner)
    if login is None:
        return _go(back, err="This partner has no login yet. Create one first.")
    password = generate_password(12)
    login.password_hash = hash_password(password)
    login.must_change_password = True
    db.add(login)
    audit(db, user, "partner.login_reset", f"partner:{partner.id}", login.email)
    db.commit()
    return _credentials(request, db, title="Partner password reset",
                        intro=f"The old password no longer works. Send this temporary password to "
                              f"{partner.display_first or partner.name} by a private channel.",
                        email=login.email, password=password, login_url="/partners/login", back_url=back,
                        back_label=partner.name, section="partners")


@router.post("/partners/{partner_id}/deactivate")
def partner_login_deactivate(partner_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    login = _partner_login(partner)
    if login is None:
        return _go(f"/admin/partners/{partner.id}", err="This partner has no login.")
    login.is_active = False
    db.add(login)
    audit(db, user, "partner.login_deactivate", f"partner:{partner.id}", login.email)
    db.commit()
    return _go(f"/admin/partners/{partner.id}", ok="Login deactivated. They can no longer sign in.")


@router.post("/partners/{partner_id}/activate")
def partner_login_activate(partner_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    partner = _get_or_404(db, Partner, partner_id)
    login = _partner_login(partner)
    if login is None:
        return _go(f"/admin/partners/{partner.id}", err="This partner has no login.")
    login.is_active = True
    db.add(login)
    audit(db, user, "partner.login_activate", f"partner:{partner.id}", login.email)
    db.commit()
    return _go(f"/admin/partners/{partner.id}", ok="Login reactivated.")


# ============================================================================
# 3. Leads
# ============================================================================
def _hq_sla(current: dict) -> tuple[float, datetime]:
    """Head office's own response target (settings lead_sla_hq_hours) and the created_at cut-off for it."""
    try:
        hours = max(0.5, float(current.get("lead_sla_hq_hours") or 24))
    except (TypeError, ValueError):
        hours = 24.0
    return hours, datetime.utcnow() - timedelta(hours=hours)


def _hq_leads(db: Session):
    """Leads head office answers itself: no partner attached (not covered, or a head-office area)."""
    return db.query(Lead).filter(Lead.partner_id.is_(None), Lead.kind != "partner_request")


def _lead_filters(request: Request) -> dict:
    qp = request.query_params
    return {"status": qp.get("status", "").strip(), "kind": qp.get("kind", "").strip(),
            "area": qp.get("area", "").strip(), "partner": qp.get("partner", "").strip(), "q": qp.get("q", "").strip()[:80]}


def _lead_query(db: Session, f: dict):
    q = db.query(Lead)
    if f["status"] in LEAD_STATUSES:
        q = q.filter(Lead.status == f["status"])
    elif f["status"] == "open":
        q = q.filter(Lead.status.in_(("new", "contacted", "booked")))
    if f["kind"] in LEAD_KINDS:
        q = q.filter(Lead.kind == f["kind"])
    if f["area"] == "none":
        q = q.filter(Lead.area_id.is_(None))
    elif f["area"].isdigit():
        q = q.filter(Lead.area_id == int(f["area"]))
    if f["partner"] == "none":          # head office's own leads: not covered, or a head-office area
        q = q.filter(Lead.partner_id.is_(None))
    elif f["partner"].isdigit():
        q = q.filter(Lead.partner_id == int(f["partner"]))
    if f["q"]:
        like = _like(f["q"])
        clauses = [Lead.name.ilike(like, escape="\\"), Lead.phone.ilike(like, escape="\\"),
                   Lead.email.ilike(like, escape="\\"), Lead.postcode.ilike(like, escape="\\")]
        outward = normalise_postcode(f["q"])
        if outward:
            clauses.append(Lead.postcode == outward)
        q = q.filter(or_(*clauses))
    return q.order_by(Lead.created_at.desc(), Lead.id.desc())


def _qs(f: dict, **extra) -> str:
    params = {k: v for k, v in f.items() if v}
    params.update({k: v for k, v in extra.items() if v})
    return urlencode(params)


@router.get("/leads", response_class=HTMLResponse)
def leads_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    f = _lead_filters(request)
    page = max(1, _int(request.query_params.get("page", "1"), 1))
    q = _lead_query(db, f)
    total = q.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    leads = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    hq_hours, hq_cutoff = _hq_sla(settings.get_all(db))
    ctx = {
        "db": db, "section": "leads", "leads": leads, "filters": f, "areas": areas, "total": total, "page": page, "pages": pages,
        "statuses": LEAD_STATUSES, "kinds": LEAD_KINDS, "kind_labels": LEAD_KIND_LABELS, "qs": _qs(f),
        "qs_prev": _qs(f, page=page - 1), "qs_next": _qs(f, page=page + 1), "page_size": PAGE_SIZE,
        "hq_hours": hq_hours, "hq_cutoff": hq_cutoff,
    }
    ctx.update(_flash(request))
    return render_admin(request, "admin/leads.html", ctx)


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if text and (text[0] in "=@\t\r" or (text[0] in "+-" and len(text) > 1 and not text[1].isdigit())):
        text = "'" + text
    return text


@router.get("/leads/export.csv")
def leads_export(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    f = _lead_filters(request)
    leads = _lead_query(db, f).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created", "updated", "kind", "status", "name", "phone", "email", "postcode", "address", "region",
                "area", "partner", "source_page", "notes", "payload"])
    for l in leads:
        w.writerow([_csv_cell(v) for v in (
            l.id, fmt_uk(l.created_at, "%Y-%m-%d %H:%M"), fmt_uk(l.updated_at, "%Y-%m-%d %H:%M"),
            l.kind, l.status, l.name, l.phone, l.email, l.postcode, l.address, l.region,
            l.area.name if l.area else "not covered", l.partner.name if l.partner else "", l.source_page, l.notes,
            json.dumps(l.payload or {}, ensure_ascii=False),
        )])
    audit(db, user, "lead.export", "leads", f"{len(leads)} rows; filters {json.dumps({k: v for k, v in f.items() if v})}")
    db.commit()
    filename = f"leads-{datetime.utcnow():%Y%m%d-%H%M}.csv"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    lead = _get_or_404(db, Lead, lead_id)
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    timeline = (db.query(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id)
                .filter(AuditLog.target == f"lead:{lead.id}").order_by(AuditLog.id.desc()).all())
    payload = [(k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in (lead.payload or {}).items()]
    ctx = {"db": db, "section": "leads", "lead": lead, "areas": areas, "timeline": timeline, "payload": payload,
           "statuses": LEAD_STATUSES}
    ctx.update(_flash(request))
    return render_admin(request, "admin/lead.html", ctx)


@router.post("/leads/{lead_id}")
def lead_update(lead_id: int, status: str = Form(""), notes: str = Form(""), area_id: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    lead = _get_or_404(db, Lead, lead_id)
    back = f"/admin/leads/{lead.id}"
    changes: list[str] = []
    if status:
        if status not in LEAD_STATUSES:
            return _go(back, err="Unknown status.")
        if status != lead.status:
            changes.append(f"status {lead.status} to {status}")
            lead.status = status
    notes = notes.strip()[:20_000]
    if notes != (lead.notes or ""):
        changes.append("notes updated")
        lead.notes = notes
    area_id = area_id.strip()
    if area_id == "":
        if lead.area_id is not None:
            changes.append(f"area {lead.area.name if lead.area else lead.area_id} removed (not covered)")
            lead.area_id = None
            lead.partner_id = None
    else:
        area = db.get(Area, _int(area_id, -1))
        if area is None:
            return _go(back, err="That area no longer exists.")
        if area.id != lead.area_id:
            changes.append(f"area to {area.name}")
            lead.area_id = area.id
            if not lead.region:
                lead.region = area.region
        new_partner = None if area.is_hq else area.partner_id
        if new_partner != lead.partner_id:
            changes.append(f"partner to {area.partner.name if (area.partner and not area.is_hq) else 'head office'}")
            lead.partner_id = new_partner
    if not changes:
        return _go(back, ok="No changes to save.")
    db.add(lead)
    audit(db, user, "lead.update", f"lead:{lead.id}", "; ".join(changes))
    db.commit()
    return _go(back, ok="Lead updated.")


# ============================================================================
# 4. Resources
# ============================================================================
def _resource_vals(r: Resource | None) -> dict:
    if r is None:
        return {"title": "", "description": "", "kind": "file", "url": "", "body": "", "category": "General",
                "visibility": "all", "region": "", "area_id": "", "sort": 100, "is_placeholder": True}
    return {"title": r.title, "description": r.description, "kind": r.kind, "url": r.url, "body": r.body,
            "category": r.category, "visibility": r.visibility, "region": r.region, "area_id": r.area_id or "",
            "sort": r.sort, "is_placeholder": r.is_placeholder}


def _resource_vals_from_form(form: FormData) -> dict:
    vals = {k: _s(form, k) for k in ("title", "description", "kind", "url", "category", "visibility", "region", "area_id")}
    vals["body"] = _s(form, "body", limit=200_000)
    vals["sort"] = _int(_s(form, "sort"), 100)
    vals["is_placeholder"] = form.get("is_placeholder") == "1"
    return vals


def _resource_form(request: Request, db: Session, vals: dict, res: Resource | None, *, error: str = "", status: int = 200) -> HTMLResponse:
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    categories = sorted({c for (c,) in db.query(Resource.category).distinct().all() if c})
    ctx = {"db": db, "section": "resources", "vals": vals, "res": res, "areas": areas, "categories": categories,
           "kinds": RESOURCE_KINDS, "exts": sorted(RESOURCE_EXTS), "max_mb": config.MAX_UPLOAD_MB}
    if error:
        ctx.update(flash=error, flash_kind="err")
    else:
        ctx.update(_flash(request))
    return render_admin(request, "admin/resource_form.html", ctx, status)


@router.get("/resources", response_class=HTMLResponse)
def resources_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    items = db.query(Resource).order_by(Resource.category, Resource.sort, Resource.id).all()
    groups: list[tuple[str, list[Resource]]] = []
    for r in items:
        if groups and groups[-1][0] == r.category:
            groups[-1][1].append(r)
        else:
            groups.append((r.category, [r]))
    area_names = {a.id: a.name for a in db.query(Area).all()}
    ctx = {"db": db, "section": "resources", "groups": groups, "area_names": area_names,
           "placeholder_count": sum(1 for r in items if r.is_placeholder), "total": len(items)}
    ctx.update(_flash(request))
    return render_admin(request, "admin/resources.html", ctx)


@router.get("/resources/new", response_class=HTMLResponse)
def resource_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _resource_form(request, db, _resource_vals(None), None)


@router.get("/resources/{resource_id}", response_class=HTMLResponse)
def resource_edit(resource_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    res = _get_or_404(db, Resource, resource_id)
    return _resource_form(request, db, _resource_vals(res), res)


async def _save_resource(request: Request, db: Session, user: User, res: Resource | None):
    form = await request.form()
    vals = _resource_vals_from_form(form)
    errors: list[str] = []
    if not vals["title"]:
        errors.append("Title is required.")
    if vals["kind"] not in RESOURCE_KINDS:
        errors.append("Choose a kind.")
    if vals["visibility"] not in RESOURCE_VISIBILITY:
        errors.append("Choose who can see it.")
    region = ""
    area_id: int | None = None
    if vals["visibility"] == "region":
        region = vals["region"]
        if region not in AREA_REGIONS:
            errors.append("Choose the region that can see it.")
    elif vals["visibility"] == "area":
        area = db.get(Area, _int(vals["area_id"], -1))
        if area is None:
            errors.append("Choose the area that can see it.")
        else:
            area_id = area.id
    url = vals["url"][:500]
    if url and not re.match(r"^https?://", url, re.I):
        errors.append("Links must start with http:// or https://.")
    upload = form.get("file")
    safe_name = ""
    if _has_upload(upload):
        safe_name, ext = _upload_name_ext(upload)
        if ext not in RESOURCE_EXTS:
            errors.append(f"Files must be one of: {', '.join(sorted(RESOURCE_EXTS))}.")
    if errors:
        return _resource_form(request, db, vals, res, error=" ".join(errors), status=400)

    action = "resource.update"
    if res is None:
        res = Resource(title=vals["title"][:200])
        db.add(res)
        action = "resource.create"
    res.title = vals["title"][:200]
    res.description = vals["description"]
    res.kind = vals["kind"]
    res.url = url
    res.body = vals["body"]
    res.category = (vals["category"] or "General")[:80]
    res.visibility = vals["visibility"]
    res.region = region
    res.area_id = area_id
    res.sort = vals["sort"]
    db.add(res)
    db.flush()

    if form.get("remove_file") == "1" and res.file_path:
        _remove_file(res.file_path)
        res.file_path = ""
    if _has_upload(upload):
        rel = f"resources/{res.id}-{safe_name}"
        err = _save_upload(upload, rel, RESOURCE_EXTS)
        if err:
            db.rollback()
            return _resource_form(request, db, vals, res if action == "resource.update" else None, error=err, status=400)
        if res.file_path and res.file_path != rel:
            _remove_file(res.file_path)
        res.file_path = rel
    supplied = bool(res.file_path) or bool(res.url) or bool(res.body.strip())
    res.is_placeholder = False if supplied else vals["is_placeholder"]
    db.add(res)
    audit(db, user, action, f"resource:{res.id}",
          f"{res.title} [{res.category}] kind {res.kind}; visibility {res.visibility}; "
          f"{'placeholder' if res.is_placeholder else 'ready'}; file {res.file_path or 'none'}")
    db.commit()
    return _go(f"/admin/resources/{res.id}", ok="Resource saved.")


@router.post("/resources/new", response_class=HTMLResponse)
async def resource_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return await _save_resource(request, db, user, None)


@router.post("/resources/{resource_id}", response_class=HTMLResponse)
async def resource_update(resource_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    res = _get_or_404(db, Resource, resource_id)
    return await _save_resource(request, db, user, res)


@router.post("/resources/{resource_id}/delete")
def resource_delete(resource_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    res = _get_or_404(db, Resource, resource_id)
    title, path = res.title, res.file_path
    if path:
        _remove_file(path)
    db.delete(res)
    audit(db, user, "resource.delete", f"resource:{resource_id}", f"{title}; file {path or 'none'}")
    db.commit()
    return _go("/admin/resources", ok=f"{title} deleted.")


# ============================================================================
# 5. Announcements
# ============================================================================
def _announcement_form(request: Request, db: Session, vals: dict, item: Announcement | None, *, error: str = "", status: int = 200):
    ctx = {"db": db, "section": "announcements", "vals": vals, "item": item}
    if error:
        ctx.update(flash=error, flash_kind="err")
    else:
        ctx.update(_flash(request))
    return render_admin(request, "admin/announcement_form.html", ctx, status)


@router.get("/announcements", response_class=HTMLResponse)
def announcements_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    items = db.query(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc()).all()
    ctx = {"db": db, "section": "announcements", "items": items}
    ctx.update(_flash(request))
    return render_admin(request, "admin/announcements.html", ctx)


@router.get("/announcements/new", response_class=HTMLResponse)
def announcement_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _announcement_form(request, db, {"title": "", "body": "", "pinned": False}, None)


@router.get("/announcements/{item_id}", response_class=HTMLResponse)
def announcement_edit(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    item = _get_or_404(db, Announcement, item_id)
    return _announcement_form(request, db, {"title": item.title, "body": item.body, "pinned": item.pinned}, item)


def _save_announcement(request: Request, db: Session, user: User, item: Announcement | None, title: str, body: str, pinned: str):
    vals = {"title": title.strip()[:200], "body": body.strip()[:20_000], "pinned": pinned == "1"}
    if not vals["title"]:
        return _announcement_form(request, db, vals, item, error="Title is required.", status=400)
    action = "announcement.update"
    if item is None:
        item = Announcement(title=vals["title"])
        db.add(item)
        action = "announcement.create"
    item.title, item.body, item.pinned = vals["title"], vals["body"], vals["pinned"]
    db.add(item)
    db.flush()
    audit(db, user, action, f"announcement:{item.id}", f"{item.title}{' (pinned)' if item.pinned else ''}")
    db.commit()
    return _go("/admin/announcements", ok="Announcement saved.")


@router.post("/announcements/new", response_class=HTMLResponse)
def announcement_create(request: Request, title: str = Form(""), body: str = Form(""), pinned: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _save_announcement(request, db, user, None, title, body, pinned)


@router.post("/announcements/{item_id}", response_class=HTMLResponse)
def announcement_update(item_id: int, request: Request, title: str = Form(""), body: str = Form(""), pinned: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(require_admin)):
    item = _get_or_404(db, Announcement, item_id)
    return _save_announcement(request, db, user, item, title, body, pinned)


@router.post("/announcements/{item_id}/pin")
def announcement_pin(item_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    item = _get_or_404(db, Announcement, item_id)
    item.pinned = not item.pinned
    db.add(item)
    audit(db, user, "announcement.pin", f"announcement:{item.id}", f"{item.title}: {'pinned' if item.pinned else 'unpinned'}")
    db.commit()
    return _go("/admin/announcements", ok=f"{item.title} {'pinned' if item.pinned else 'unpinned'}.")


@router.post("/announcements/{item_id}/delete")
def announcement_delete(item_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    item = _get_or_404(db, Announcement, item_id)
    title = item.title
    db.delete(item)
    audit(db, user, "announcement.delete", f"announcement:{item_id}", title)
    db.commit()
    return _go("/admin/announcements", ok=f"{title} deleted.")


# ============================================================================
# 6. Settings
# ============================================================================
SETTING_GROUPS: list[tuple[str, list[str]]] = [
    ("Head office", ["hq_name", "hq_phone", "hq_whatsapp", "hq_whatsapp_display", "hq_email", "hq_address"]),
    ("Company and compliance", ["company_name", "company_number", "registered_office", "rsw_licence", "cmp_scheme",
                                "cmp_number", "redress_scheme", "ico_number"]),
    ("Social", ["facebook_url", "instagram_url"]),
    ("Lead response targets", ["lead_sla_hq_hours", "lead_sla_partner_hours"]),
    ("Fees", ["fees", "fees_confirmed"]),
    ("Systems", ["ordris_url", "ordris_label"]),
]
SETTING_LABELS = {
    "hq_name": "Head office name", "hq_phone": "Phone", "hq_whatsapp": "WhatsApp number (digits starting 44)",
    "hq_whatsapp_display": "WhatsApp as shown on the site", "hq_email": "Email", "hq_address": "Address",
    "company_name": "Company name", "company_number": "Company number", "registered_office": "Registered office",
    "rsw_licence": "Rent Smart Wales licence", "cmp_scheme": "Client money protection scheme",
    "cmp_number": "Client money protection number", "redress_scheme": "Redress scheme", "ico_number": "ICO registration",
    "facebook_url": "Facebook page", "instagram_url": "Instagram profile",
    "lead_sla_hq_hours": "Head office response target (hours)", "lead_sla_partner_hours": "Partner response target (hours)",
    "fees_confirmed": "Fee sheet confirmed by the founder (clears the hold notice on the partners' marketing kit)",
    "ordris_url": "Ordris login link (agency management system; shown in the partner portal, blank to hide)",
    "ordris_label": "Ordris description shown to partners",
    "fees.tenant_find": "Tenant find", "fees.full_management_pct": "Full management (percentage)",
    "fees.full_management_cap": "Full management cap (per month)", "fees.setup": "Set-up fee",
    "fees.rent_guarantee_addon": "Rent guarantee add-on (per month)", "fees.hmo_pct": "HMO management (percentage)",
    "fees.room_let_only": "Room let only", "fees.sa_launch": "Short-let launch", "fees.sa_full_pct": "Short-let full management (percentage)",
    "fees.sa_cohost_pct": "Short-let co-hosting (percentage)",
}


def _setting_fields(current: dict) -> list[tuple[str, list[dict]]]:
    grouped = {k for _, keys in SETTING_GROUPS for k in keys}
    groups = list(SETTING_GROUPS) + [("Other", [k for k in settings.DEFAULTS if k not in grouped])]
    out: list[tuple[str, list[dict]]] = []
    for title, keys in groups:
        fields: list[dict] = []
        for key in keys:
            if key not in settings.DEFAULTS:
                continue
            default = settings.DEFAULTS[key]
            value = current.get(key, default)
            if isinstance(default, dict):
                stored = value if isinstance(value, dict) else {}
                for sub, sub_default in default.items():
                    fields.append({"name": f"{key}__{sub}", "label": SETTING_LABELS.get(f"{key}.{sub}", _label(sub)),
                                   "value": stored.get(sub, sub_default), "type": "text"})
            elif isinstance(default, bool):
                fields.append({"name": key, "label": SETTING_LABELS.get(key, _label(key)), "value": bool(value), "type": "checkbox"})
            elif isinstance(default, int):
                fields.append({"name": key, "label": SETTING_LABELS.get(key, _label(key)), "value": value, "type": "number"})
            else:
                fields.append({"name": key, "label": SETTING_LABELS.get(key, _label(key)), "value": "" if value is None else value,
                               "type": "textarea" if len(str(default)) > 70 else "text"})
        if fields:
            out.append((title, fields))
    return out


def _setting_placeholders(current: dict) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for key, value in current.items():
        if isinstance(value, dict):
            for sub, v in value.items():
                if isinstance(v, str) and PLACEHOLDER_RE.search(v):
                    found.append((SETTING_LABELS.get(f"{key}.{sub}", f"{_label(key)}: {sub}"), v))
        elif isinstance(value, str) and PLACEHOLDER_RE.search(value):
            found.append((SETTING_LABELS.get(key, _label(key)), value))
    return found


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    current = settings.get_all(db)
    ctx = {"db": db, "section": "settings", "groups": _setting_fields(current), "placeholders": _setting_placeholders(current)}
    ctx.update(_flash(request))
    return render_admin(request, "admin/settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    current = settings.get_all(db)
    posted: dict = {}
    errors: list[str] = []
    for key, default in settings.DEFAULTS.items():
        label = SETTING_LABELS.get(key, _label(key))
        if isinstance(default, dict):
            stored = current.get(key) if isinstance(current.get(key), dict) else {}
            new = dict(stored)
            for sub, sub_default in default.items():
                new[sub] = _s(form, f"{key}__{sub}", str(sub_default), limit=100)
            posted[key] = new
        elif isinstance(default, bool):
            posted[key] = form.get(key) == "1"
        elif isinstance(default, int):
            raw = _s(form, key, str(default))
            try:
                posted[key] = int(raw)
            except ValueError:
                posted[key] = raw
                errors.append(f"{label} must be a whole number.")
        else:
            value = _s(form, key, limit=2000)
            if key == "hq_whatsapp":
                value, err = _whatsapp(value)
                if err:
                    errors.append(err)
            posted[key] = value
    if errors:
        ctx = {"db": db, "section": "settings", "groups": _setting_fields(posted), "placeholders": _setting_placeholders(current),
               "flash": " ".join(errors), "flash_kind": "err"}
        return render_admin(request, "admin/settings.html", ctx, 400)
    changed = [k for k, v in posted.items() if v != current.get(k)]
    for key in changed:
        settings.put(db, key, posted[key])
    if changed:
        audit(db, user, "settings.update", "settings", ", ".join(changed))
        db.commit()
        return _go("/admin/settings", ok=f"Settings saved ({len(changed)} changed).")
    return _go("/admin/settings", ok="No changes to save.")


# ============================================================================
# 7. Territory checker
# ============================================================================
@router.get("/territory", response_class=HTMLResponse)
def territory(request: Request, postcode: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    areas = db.query(Area).order_by(Area.sort, Area.id).all()
    result: dict | None = None
    postcode = postcode.strip()[:12]
    if postcode:
        outward = normalise_postcode(postcode)
        if not outward:
            result = {"query": postcode, "error": "That is not a UK postcode."}
        else:
            letters = re.match(r"^[A-Z]{1,2}", outward).group(0)
            live = [a for a in areas if a.status in ("active", "coming")]
            match, rule = None, "No rule matched: this postcode is not covered."
            for a in live:
                if outward in (a.postcodes or []):
                    match, rule = a, f"Exact district {outward} is listed for {a.name}."
                    break
            if match is None:
                for a in live:
                    if letters in (a.postcodes or []):
                        match, rule = a, f"Whole postcode area {letters} is listed for {a.name} (no area lists {outward} exactly)."
                        break
            dormant = [a for a in areas if a.status not in ("active", "coming")
                       and (outward in (a.postcodes or []) or letters in (a.postcodes or []))]
            result = {"query": postcode, "outward": outward, "letters": letters, "region": region_for(letters),
                      "area": match, "rule": rule, "dormant": dormant}

    owners: dict[str, list[Area]] = {}
    for a in areas:
        for code in a.postcodes or []:
            owners.setdefault(code, []).append(a)
    duplicates = {code: [a.name for a in owner] for code, owner in owners.items() if len(owner) > 1}
    nested: list[str] = []
    whole = {code: owner[0] for code, owner in owners.items() if code.isalpha()}
    for code, owner in owners.items():
        if code.isalpha():
            continue
        letters = re.match(r"^[A-Z]{1,2}", code).group(0)
        parent = whole.get(letters)
        if parent is not None and any(a.id != parent.id for a in owner):
            nested.append(f"{code} ({', '.join(a.name for a in owner if a.id != parent.id)}) sits inside the whole area "
                          f"{letters} listed for {parent.name}; the exact district wins.")
    ctx = {"db": db, "section": "territory", "postcode": postcode, "result": result, "areas": areas,
           "duplicates": duplicates, "nested": nested}
    return render_admin(request, "admin/territory.html", ctx)


# ============================================================================
# 8. Admin users
# ============================================================================
@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    admins = db.query(User).filter(User.role == "admin").order_by(User.created_at, User.id).all()
    ctx = {"db": db, "section": "users", "admins": admins, "me": user}
    ctx.update(_flash(request))
    return render_admin(request, "admin/users.html", ctx)


@router.post("/users", response_class=HTMLResponse)
def user_create(request: Request, email: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    email = email.strip().lower()[:200]
    if not EMAIL_RE.match(email):
        return _go("/admin/users", err="Enter a valid email address.")
    if db.query(User).filter(User.email == email).first() is not None:
        return _go("/admin/users", err="A login with that email address already exists.")
    password = generate_password(14)
    new = User(email=email, password_hash=hash_password(password), role="admin", is_active=True, must_change_password=True)
    db.add(new)
    audit(db, user, "user.create", f"user:{email}", "admin")
    db.commit()
    return _credentials(request, db, title="Admin user created",
                        intro="Send these by a private channel. They can change the password at any time from the "
                              "partner portal's Password page once logged in.",
                        email=email, password=password, login_url="/admin/login", back_url="/admin/users",
                        back_label="admin users", section="users")


@router.post("/users/{user_id}/deactivate")
def user_deactivate(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = _get_or_404(db, User, user_id)
    if target.role != "admin":
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        return _go("/admin/users", err="You cannot deactivate your own account. Ask another admin to do it.")
    target.is_active = False
    db.add(target)
    audit(db, user, "user.deactivate", f"user:{target.email}")
    db.commit()
    return _go("/admin/users", ok=f"{target.email} deactivated.")


@router.post("/users/{user_id}/activate")
def user_activate(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = _get_or_404(db, User, user_id)
    if target.role != "admin":
        raise HTTPException(status_code=404, detail="User not found")
    target.is_active = True
    db.add(target)
    audit(db, user, "user.activate", f"user:{target.email}")
    db.commit()
    return _go("/admin/users", ok=f"{target.email} reactivated.")


# ============================================================================
# 9. Audit log
# ============================================================================
@router.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = (db.query(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id)
            .order_by(AuditLog.id.desc()).limit(200).all())
    return render_admin(request, "admin/audit.html", {"db": db, "section": "audit", "rows": rows})
