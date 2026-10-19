"""Partner portal: login, dashboard, leads, resources, marketing kit, profile, area.

Everything a partner sees is scoped through `partner_areas()`: an area the
partner does not own, a lead outside those areas or a resource not visible to
them answers 404, never 403, so the portal gives nothing away.
"""
from __future__ import annotations

import json
import logging
import math
import re
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from markupsafe import Markup, escape
from sqlalchemy import false
from sqlalchemy.orm import Session

from .. import config
from ..auth import (PARTNER_PAUSED_MESSAGE, audit, check_origin, clear_session, current_user, hash_password,
                    login_allowed, partner_blocked, require_partner, safe_next, set_session, throttle_hit, touch_login,
                    verify_password)
from ..db import get_db
from ..models import Announcement, Area, Lead, Partner, Resource, User
from ..services import content, kit, settings
from ..services.times import now_uk_stamp

log = logging.getLogger("hnh.portal")
router = APIRouter(prefix="/partners", dependencies=[Depends(check_origin)])

LEAD_STATUSES = ("new", "contacted", "booked", "instructed", "lost")
LEAD_KIND_LABELS = {
    "rent_review": "rent review", "sa_appraisal": "short-let appraisal", "contact": "enquiry", "alert": "property alert",
    "partner_application": "partner application", "enquiry": "enquiry", "partner_request": "change request",
}
PHOTO_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
PHOTO_MAX_MB = min(5, config.MAX_UPLOAD_MB)
BIO_MAX = 400
KIT_TABS = [("flyers", "Flyers"), ("fb", "Facebook posts"), ("graphics", "Graphics"), ("gbp", "Google Business Profile"),
            ("letters", "Letters"), ("scripts", "Scripts")]


def render_portal(request: Request, template: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    from ..main import templates
    db = ctx.pop("db", None) or next(get_db())
    base = {"request": request, "s": settings.get_all(db), "section": ctx.get("section", "")}
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def partner_areas(db: Session, user: User) -> list[Area]:
    if user.role == "admin":
        return db.query(Area).filter(Area.status != "hidden").order_by(Area.sort).all()
    if not user.partner_id:
        return []
    return db.query(Area).filter(Area.partner_id == user.partner_id, Area.status != "hidden").order_by(Area.sort).all()


# ---- shared helpers --------------------------------------------------------
def _flash(ok: str = "", err: str = "") -> dict:
    if err:
        return {"flash": err[:300], "flash_kind": "err"}
    if ok:
        return {"flash": ok[:300], "flash_kind": "ok"}
    return {}


def _redirect(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    qs = urlencode({"err": err} if err else ({"ok": ok} if ok else {}))
    return RedirectResponse(f"{path}?{qs}" if qs else path, status_code=303)


def _sla_hours(s: dict) -> float:
    try:
        return max(0.5, float(s.get("lead_sla_partner_hours") or 4))
    except (TypeError, ValueError):
        return 4.0


def _hours_since(dt: datetime | None) -> float:
    if not dt:
        return 0.0
    return max(0.0, (datetime.utcnow() - dt).total_seconds() / 3600)


def wa_digits(raw: str) -> str:
    """International digits for wa.me from a UK number in any common form; '' if implausible."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "44" + digits[1:]
    elif not s.startswith("+") and not digits.startswith("44") and len(digits) == 10:
        digits = "44" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def wa_display(digits: str) -> str:
    if digits.startswith("44") and len(digits) == 12:
        return f"0{digits[2:6]} {digits[6:]}"
    return f"+{digits}" if digits else ""


def _tel_href(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", raw or "")
    return f"tel:{cleaned}" if cleaned else ""


def _dom_id(value) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-") or "item"


def _scoped_leads(db: Session, user: User, areas: list[Area]):
    """Leads this user may see. Partner change requests are HQ's, not the partner's, so they are left out."""
    q = db.query(Lead).filter(Lead.kind != "partner_request")
    if user.role == "admin":
        return q
    ids = [a.id for a in areas]
    return q.filter(Lead.area_id.in_(ids)) if ids else q.filter(false())


def _lead_or_404(db: Session, user: User, lead_id: int) -> Lead:
    lead = _scoped_leads(db, user, partner_areas(db, user)).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def _area_or_404(db: Session, user: User, slug: str) -> Area:
    for a in partner_areas(db, user):
        if a.slug == slug:
            return a
    raise HTTPException(status_code=404, detail="Area not found")


def _kit_area(db: Session, user: User, slug: str) -> Area | None:
    areas = partner_areas(db, user)
    if slug:
        return _area_or_404(db, user, slug)
    return areas[0] if areas else None


def _kit_ctx(db: Session, area: Area) -> dict[str, str]:
    s = settings.get_all(db)
    return kit.context_for(area, hq_whatsapp=str(s.get("hq_whatsapp", "")), hq_whatsapp_display=str(s.get("hq_whatsapp_display", "")),
                           hq_phone=str(s.get("hq_phone", "")), base_url=config.BASE_URL)


def _safe_upload_path(rel: str) -> Path:
    base = config.UPLOAD_DIR.resolve()
    try:
        path = (base / rel).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    if base not in path.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return path


def _resource_visible(r: Resource, area_ids: set[int], regions: set[str]) -> bool:
    if r.visibility == "all":
        return True
    if r.visibility == "region":
        return bool(r.region) and r.region in regions
    if r.visibility == "area":
        return r.area_id in area_ids
    return False


def _visible_resources(db: Session, user: User, areas: list[Area]) -> list[Resource]:
    items = db.query(Resource).order_by(Resource.category, Resource.sort, Resource.id).all()
    if user.role == "admin":
        return items
    ids = {a.id for a in areas}
    regions = {a.region for a in areas}
    return [r for r in items if _resource_visible(r, ids, regions)]


def _resource_or_404(db: Session, user: User, rid: int) -> Resource:
    for r in _visible_resources(db, user, partner_areas(db, user)):
        if r.id == rid:
            return r
    raise HTTPException(status_code=404, detail="Resource not found")


# ---- auth ------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/partners", db: Session = Depends(get_db),
               user: User | None = Depends(current_user)):
    next = safe_next(next, "/partners")
    if user and user.role in ("partner", "admin"):
        return RedirectResponse(next, status_code=303)
    return render_portal(request, "portal/login.html", {"db": db, "next": next, "error": ""})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/partners"),
          db: Session = Depends(get_db)):
    norm, keys, allowed = login_allowed(request, email)
    if not allowed:
        return render_portal(request, "portal/login.html", {"db": db, "next": next, "error": "Too many attempts. Try again in 15 minutes."}, 429)
    user = db.query(User).filter(User.email == norm).first()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        for key in keys:
            throttle_hit(key)
        return render_portal(request, "portal/login.html", {"db": db, "next": next, "error": "Email or password not recognised."}, 401)
    if partner_blocked(user):
        return render_portal(request, "portal/login.html", {"db": db, "next": next, "error": PARTNER_PAUSED_MESSAGE}, 403)
    touch_login(db, user)
    audit(db, user, "login", "portal")
    db.commit()
    target = safe_next(next, "/partners")
    if user.role == "admin" and target == "/partners":
        target = "/admin"
    resp = RedirectResponse(target, status_code=303)
    set_session(resp, user)
    return resp


@router.post("/logout")
def logout(user: User | None = Depends(current_user)):
    resp = RedirectResponse("/partners/login", status_code=303)
    clear_session(resp)
    return resp


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, first: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    return render_portal(request, "portal/password.html", {"db": db, "user": user, "first": bool(first), "error": "", "section": "password"})


@router.post("/password", response_class=HTMLResponse)
def password_change(request: Request, current: str = Form(""), new: str = Form(...), confirm: str = Form(...),
                    db: Session = Depends(get_db), user: User = Depends(require_partner)):
    first = bool(user.must_change_password)
    # The current (or temporary) password is always required: a cookie that survived a reset must not be
    # enough to choose the new password, so a stale or stolen session cannot take the account over.
    if not verify_password(current, user.password_hash):
        error = "The temporary password is wrong. Use the one head office gave you." if first else "Current password is wrong."
        return render_portal(request, "portal/password.html", {"db": db, "user": user, "first": first, "error": error, "section": "password"}, 400)
    if len(new) < 10 or new != confirm:
        return render_portal(request, "portal/password.html", {"db": db, "user": user, "first": first, "error": "Use at least 10 characters, and make both boxes match.", "section": "password"}, 400)
    if new == current:
        return render_portal(request, "portal/password.html", {"db": db, "user": user, "first": first, "error": "Choose a password you have not used here before.", "section": "password"}, 400)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    db.add(user)
    audit(db, user, "password_change")
    db.commit()
    # Every other session dies with the old hash; this browser gets a cookie bound to the new one.
    resp = RedirectResponse("/admin" if user.role == "admin" else "/partners", status_code=303)
    set_session(resp, user)
    return resp


# ---- dashboard -------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_partner)):
    areas = partner_areas(db, user)
    q = _scoped_leads(db, user, areas)
    leads = q.order_by(Lead.created_at.desc(), Lead.id.desc()).limit(10).all()
    open_count = q.filter(Lead.status.in_(("new", "contacted", "booked"))).count()
    s = settings.get_all(db)
    sla_hours = _sla_hours(s)
    cutoff = datetime.utcnow() - timedelta(hours=sla_hours)
    breach_count = q.filter(Lead.status == "new", Lead.created_at < cutoff).count()
    announcements = db.query(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).limit(5).all()
    return render_portal(request, "portal/dashboard.html", {
        "db": db, "user": user, "areas": areas, "leads": leads, "open_count": open_count,
        "announcements": announcements, "section": "dashboard",
        "sla_hours": sla_hours, "cutoff": cutoff, "breach_count": breach_count,
    })


# ---- leads -----------------------------------------------------------------
@router.get("/leads", response_class=HTMLResponse)
def leads(request: Request, status: str = "", kind: str = "", page: int = 1, ok: str = "", err: str = "",
          db: Session = Depends(get_db), user: User = Depends(require_partner)):
    areas = partner_areas(db, user)
    base_q = _scoped_leads(db, user, areas)
    kinds = sorted({k for (k,) in base_q.with_entities(Lead.kind).distinct().all() if k})
    q = base_q
    status = status if status in LEAD_STATUSES else ""
    kind = kind if kind in kinds else ""
    if status:
        q = q.filter(Lead.status == status)
    if kind:
        q = q.filter(Lead.kind == kind)
    per_page = 50
    total = q.count()
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    rows = q.order_by(Lead.created_at.desc(), Lead.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    s = settings.get_all(db)
    sla_hours = _sla_hours(s)
    cutoff = datetime.utcnow() - timedelta(hours=sla_hours)

    def page_url(n: int) -> str:
        params = {k: v for k, v in (("status", status), ("kind", kind)) if v}
        params["page"] = n
        return "/partners/leads?" + urlencode(params)

    return render_portal(request, "portal/leads.html", {
        "db": db, "user": user, "section": "leads", "rows": rows, "total": total, "page": page, "pages": pages,
        "status": status, "kind": kind, "kinds": kinds, "statuses": LEAD_STATUSES, "kind_labels": LEAD_KIND_LABELS,
        "sla_hours": sla_hours, "cutoff": cutoff, "page_url": page_url, **_flash(ok, err),
    })


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, ok: str = "", err: str = "",
                db: Session = Depends(get_db), user: User = Depends(require_partner)):
    lead = _lead_or_404(db, user, lead_id)
    s = settings.get_all(db)
    payload_rows = [(str(k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else "" if v is None else str(v))
                    for k, v in (lead.payload or {}).items() if k != "website"]
    partner_first = user.partner.display_first if user.partner else "the team"
    kind_label = LEAD_KIND_LABELS.get(lead.kind, lead.kind.replace("_", " "))
    first = (lead.name or "").split()[0] if lead.name else ""
    wa = wa_digits(lead.phone)
    wa_text = f"Hi{' ' + first if first else ''}, it's {partner_first} from Hello New Home about your {kind_label}."
    notes = [ln for ln in (lead.notes or "").splitlines() if ln.strip()]
    elapsed = _hours_since(lead.created_at)
    sla_hours = _sla_hours(s)
    return render_portal(request, "portal/lead.html", {
        "db": db, "user": user, "section": "leads", "lead": lead, "payload_rows": payload_rows, "kind_label": kind_label,
        "wa": wa, "wa_display": wa_display(wa), "wa_href": f"https://wa.me/{wa}?text={quote(wa_text)}" if wa else "",
        "tel_href": _tel_href(lead.phone), "notes": notes, "statuses": LEAD_STATUSES,
        "elapsed": elapsed, "sla_hours": sla_hours, "breached": lead.status == "new" and elapsed > sla_hours,
        **_flash(ok, err),
    })


@router.post("/leads/{lead_id}")
def lead_update(lead_id: int, request: Request, status: str = Form(""), note: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(require_partner)):
    lead = _lead_or_404(db, user, lead_id)
    changes: list[str] = []
    status = status.strip().lower()
    if status and status not in LEAD_STATUSES:
        return _redirect(f"/partners/leads/{lead.id}", err="That status is not recognised.")
    if status and status != lead.status:
        changes.append(f"status {lead.status} to {status}")
        lead.status = status
    note = note.strip()
    if note:
        who = user.partner.display_first if user.partner else user.email
        line = f"[{now_uk_stamp()} {who}] {' '.join(note.split())[:1000]}"
        lead.notes = f"{lead.notes.rstrip()}\n{line}" if lead.notes else line
        changes.append("note added")
    if not changes:
        return _redirect(f"/partners/leads/{lead.id}", err="Nothing to save.")
    lead.updated_at = datetime.utcnow()
    db.add(lead)
    audit(db, user, "lead_update", f"lead:{lead.id}", "; ".join(changes))
    db.commit()
    return _redirect(f"/partners/leads/{lead.id}", ok="Saved.")


# ---- resources -------------------------------------------------------------
@router.get("/resources", response_class=HTMLResponse)
def resources(request: Request, ok: str = "", err: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    areas = partner_areas(db, user)
    groups: dict[str, list[Resource]] = {}
    for r in _visible_resources(db, user, areas):
        groups.setdefault(r.category or "General", []).append(r)
    return render_portal(request, "portal/resources.html", {
        "db": db, "user": user, "section": "resources", "groups": groups, **_flash(ok, err),
    })


@router.get("/resources/{rid}", response_class=HTMLResponse)
def resource_page(rid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_partner)):
    r = _resource_or_404(db, user, rid)
    body = Markup(r.body) if r.kind == "page" and r.body else ""
    return render_portal(request, "portal/resource.html", {"db": db, "user": user, "section": "resources", "r": r, "body": body})


@router.get("/files/{rid}")
def resource_file(rid: int, db: Session = Depends(get_db), user: User = Depends(require_partner)):
    r = _resource_or_404(db, user, rid)
    if r.kind != "file" or not r.file_path or r.is_placeholder:
        raise HTTPException(status_code=404, detail="File not found")
    path = _safe_upload_path(r.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    prefix = f"{r.id}-"
    original = path.name[len(prefix):] if path.name.startswith(prefix) else path.name
    return FileResponse(path, filename=original or path.name)


# ---- marketing kit ---------------------------------------------------------
_HTML_KEYS = ("front_html", "back_html", "html")


def _templates() -> dict:
    try:
        data = content.marketing_templates()
    except RuntimeError as e:          # invalid JSON on disk: show an empty kit, never a 500
        log.warning("marketing templates unavailable: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _tpl_list(tpl: dict, key: str) -> list[dict]:
    value = tpl.get(key)
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _render_item(item: dict, ctx: dict, html_ctx: dict) -> dict:
    out = {}
    for k, v in item.items():
        out[k] = kit.render_deep(v, html_ctx if k in _HTML_KEYS else ctx)
    out["dom_id"] = _dom_id(item.get("id"))
    return out


def _find(tpl: dict, key: str, item_id: str) -> dict | None:
    for item in _tpl_list(tpl, key):
        if str(item.get("id", "")) == item_id:
            return item
    return None


def _kit_bundle(db: Session, user: User, slug: str):
    area = _kit_area(db, user, slug)
    if area is None:
        return None, None, None, None
    ctx = _kit_ctx(db, area)
    html_ctx = {k: str(escape(v)) for k, v in ctx.items()}
    return area, ctx, html_ctx, _templates()


@router.get("/kit", response_class=HTMLResponse)
def kit_index(request: Request, area: str = "", tab: str = "flyers", pillar: str = "", region: str = "",
              db: Session = Depends(get_db), user: User = Depends(require_partner)):
    areas = partner_areas(db, user)
    ar, ctx, html_ctx, tpl = _kit_bundle(db, user, area)
    s = settings.get_all(db)
    if ar is None:
        return render_portal(request, "portal/kit.html", {"db": db, "user": user, "section": "kit", "areas": areas, "area": None})
    tab = tab if tab in dict(KIT_TABS) else "flyers"

    flyers = [_render_item(f, ctx, html_ctx) for f in _tpl_list(tpl, "flyers")]
    posts_all = [_render_item(p, ctx, html_ctx) for p in _tpl_list(tpl, "fb_posts")
                 if str(p.get("pillar", "")) != "london" or ar.slug == "london"]
    pillars = sorted({str(p.get("pillar", "")) for p in posts_all if p.get("pillar")})
    pillar = pillar if pillar in pillars else ""
    region = "all" if region == "all" else ""
    posts = [p for p in posts_all
             if (region == "all" or str(p.get("region", "both")) in ("both", "", ar.region))
             and (not pillar or str(p.get("pillar", "")) == pillar)]
    graphics = [_render_item(g, ctx, html_ctx) for g in _tpl_list(tpl, "graphics")]
    gbp_raw = tpl.get("gbp")
    gbp = kit.render_deep(gbp_raw, ctx) if isinstance(gbp_raw, dict) else {}
    gbp_services = [x for x in (gbp.get("services") or []) if isinstance(x, dict)] if isinstance(gbp.get("services"), list) else []
    gbp_posts = [x for x in (gbp.get("posts") or []) if isinstance(x, dict)] if isinstance(gbp.get("posts"), list) else []
    gbp_qna = [x for x in (gbp.get("qna") or []) if isinstance(x, dict)] if isinstance(gbp.get("qna"), list) else []
    gbp_lists = {k: [str(x) for x in gbp[k]] for k in ("categories", "attributes", "photo_plan") if isinstance(gbp.get(k), list)}
    letters = [_render_item(x, ctx, html_ctx) for x in _tpl_list(tpl, "letters")]
    scripts = [_render_item(x, ctx, html_ctx) for x in _tpl_list(tpl, "scripts")]
    counts = {"flyers": len(flyers), "fb": len(posts), "graphics": len(graphics),
              "gbp": len(gbp_services) + len(gbp_posts) + len(gbp_qna) + (1 if gbp.get("description") else 0),
              "letters": len(letters), "scripts": len(scripts)}
    return render_portal(request, "portal/kit.html", {
        "db": db, "user": user, "section": "kit", "areas": areas, "area": ar, "ctx": ctx, "tab": tab, "tabs": KIT_TABS,
        "counts": counts, "flyers": flyers, "posts": posts, "pillars": pillars, "pillar": pillar, "region": region,
        "graphics": graphics, "gbp": gbp, "gbp_services": gbp_services, "gbp_posts": gbp_posts, "gbp_qna": gbp_qna,
        "gbp_lists": gbp_lists, "letters": letters, "scripts": scripts, "formats": _GRAPHIC_FORMATS,
        "fees_confirmed": bool(s.get("fees_confirmed")), "empty": not any(counts.values()),
    })


def _print_page(request: Request, db: Session, user: User, item: dict, pages: list[str], size: str, area: Area, ctx: dict,
                kind: str) -> HTMLResponse:
    size = size.upper() if size.upper() in ("A4", "A5", "A6") else ("A4" if kind == "letter" else "A5")
    dims = {"A4": ("210mm", "297mm"), "A5": ("148mm", "210mm"), "A6": ("105mm", "148mm")}[size]
    return render_portal(request, "portal/kit_print.html", {
        "db": db, "user": user, "item": item, "pages": [Markup(p) for p in pages if p], "size": size, "width": dims[0],
        "height": dims[1], "area": area, "ctx": ctx, "kind": kind,
        "fees_confirmed": bool(settings.get(db, "fees_confirmed")),
    })


@router.get("/kit/flyer/{fid}", response_class=HTMLResponse)
def kit_flyer(fid: str, request: Request, area: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    ar, ctx, html_ctx, tpl = _kit_bundle(db, user, area)
    item = _find(tpl, "flyers", fid) if ar else None
    if ar is None or item is None:
        raise HTTPException(status_code=404, detail="Flyer not found")
    r = _render_item(item, ctx, html_ctx)
    return _print_page(request, db, user, r, [str(r.get("front_html", "")), str(r.get("back_html", ""))], str(r.get("size", "A5")), ar, ctx, "flyer")


@router.get("/kit/letter/{lid}", response_class=HTMLResponse)
def kit_letter(lid: str, request: Request, area: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    ar, ctx, html_ctx, tpl = _kit_bundle(db, user, area)
    item = _find(tpl, "letters", lid) if ar else None
    if ar is None or item is None:
        raise HTTPException(status_code=404, detail="Letter not found")
    r = _render_item(item, ctx, html_ctx)
    return _print_page(request, db, user, r, [str(r.get("html", ""))], "A4", ar, ctx, "letter")


# Graphic formats: canvas size, padding and type sizes tuned so a ~18-character headline line fits a square.
_GRAPHIC_FORMATS = {
    "square": {"w": 1080, "h": 1080, "pad": 80, "wm": 46, "head": 92, "head_wrap": 18, "head_lines": 4, "sub": 40, "sub_wrap": 40, "sub_lines": 4, "cta": 36, "foot": 30},
    "story": {"w": 1080, "h": 1920, "pad": 90, "wm": 50, "head": 104, "head_wrap": 16, "head_lines": 6, "sub": 44, "sub_wrap": 34, "sub_lines": 6, "cta": 40, "foot": 32},
    "cover": {"w": 1640, "h": 624, "pad": 72, "wm": 40, "head": 72, "head_wrap": 30, "head_lines": 2, "sub": 32, "sub_wrap": 70, "sub_lines": 2, "cta": 30, "foot": 26},
    "a-board": {"w": 2480, "h": 3508, "pad": 200, "wm": 120, "head": 236, "head_wrap": 16, "head_lines": 6, "sub": 100, "sub_wrap": 34, "sub_lines": 6, "cta": 92, "foot": 80},
}
_GRAPHIC_STYLES = {
    "dark": {"bg": "#141416", "text": "#FFFFFF", "muted": "#CFCBD1", "accent": "#E0287A", "new": "#E0287A", "cta_bg": "#E0287A", "cta_text": "#FFFFFF"},
    "pink": {"bg": "#E0287A", "text": "#FFFFFF", "muted": "#FBE9F1", "accent": "#141416", "new": "#141416", "cta_bg": "#141416", "cta_text": "#FFFFFF"},
    "light": {"bg": "#FBF3F6", "text": "#141416", "muted": "#5F5A60", "accent": "#E0287A", "new": "#E0287A", "cta_bg": "#E0287A", "cta_text": "#FFFFFF"},
}
_FONT = "Lato, Helvetica Neue, Arial, sans-serif"


def _fit_lines(text: str, width: int, size: float, max_lines: int) -> tuple[list[str], float]:
    text = " ".join((text or "").split())
    lines = textwrap.wrap(text, width, break_long_words=False) or [""]
    for _ in range(6):
        if len(lines) <= max_lines:
            break
        width = int(width * 1.2)
        size *= 0.85
        lines = textwrap.wrap(text, width, break_long_words=False) or [""]
    return lines[:max_lines], size


def _tspans(lines: list[str], x: float, first_y: float, line_h: float) -> str:
    return "".join(f'<tspan x="{x:.0f}" y="{first_y + i * line_h:.0f}">{xml_escape(ln)}</tspan>' for i, ln in enumerate(lines))


def build_graphic_svg(g: dict, ctx: dict) -> str:
    f = _GRAPHIC_FORMATS.get(str(g.get("format", "")), _GRAPHIC_FORMATS["square"])
    st = _GRAPHIC_STYLES.get(str(g.get("style", "")), _GRAPHIC_STYLES["dark"])
    W, H, pad = f["w"], f["h"], f["pad"]
    head_lines, head_size = _fit_lines(str(g.get("headline", "")), f["head_wrap"], f["head"], f["head_lines"])
    sub_lines, sub_size = _fit_lines(str(g.get("sub", "")), f["sub_wrap"], f["sub"], f["sub_lines"])
    if sub_lines == [""]:
        sub_lines = []
    head_lh, sub_lh = head_size * 1.06, sub_size * 1.4
    gap = head_size * 0.45
    rule_h = max(4, head_size * 0.08)
    block_h = len(head_lines) * head_lh + (gap + rule_h + gap + len(sub_lines) * sub_lh if sub_lines else 0)
    cta = " ".join(str(g.get("cta", "")).split())
    cta_size = f["cta"]
    cta_h = cta_size * 2.1
    top = pad + f["wm"] + f["wm"] * 1.2
    bottom = H - pad - (cta_h if cta else 0) - f["wm"] * 0.8
    y0 = top + max(0.0, (bottom - top - block_h) / 2)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{_FONT}">',
             f"<title>{xml_escape(str(g.get('headline', '')))}</title>",
             f'<rect width="{W}" height="{H}" fill="{st["bg"]}"/>']
    # wordmark, top left; WhatsApp number top right
    wm = f["wm"]
    parts.append(f'<text x="{pad}" y="{pad + wm * 0.85:.0f}" font-size="{wm}" font-weight="900" fill="{st["text"]}" letter-spacing="-1">'
                 f'hello<tspan fill="{st["new"]}">new</tspan>home</text>')
    wa_disp = str(ctx.get("whatsapp_display", ""))
    if wa_disp:
        parts.append(f'<text x="{W - pad}" y="{pad + wm * 0.85:.0f}" font-size="{f["foot"]}" font-weight="700" fill="{st["muted"]}" text-anchor="end">'
                     f'WhatsApp {xml_escape(wa_disp)}</text>')
    # headline, rule, sub
    y = y0 + head_size * 0.9
    parts.append(f'<text font-size="{head_size:.0f}" font-weight="900" fill="{st["text"]}" letter-spacing="-1.5">{_tspans(head_lines, pad, y, head_lh)}</text>')
    y += (len(head_lines) - 1) * head_lh
    if sub_lines:
        rule_y = y + gap
        parts.append(f'<rect x="{pad}" y="{rule_y:.0f}" width="{head_size * 1.3:.0f}" height="{rule_h:.0f}" rx="{rule_h / 2:.0f}" fill="{st["accent"]}"/>')
        y = rule_y + rule_h + gap + sub_size * 0.9
        parts.append(f'<text font-size="{sub_size:.0f}" font-weight="400" fill="{st["muted"]}">{_tspans(sub_lines, pad, y, sub_lh)}</text>')
    # call to action pill, bottom left; area URL bottom right when there is room
    cta_w = 0.0
    if cta:
        cta_w = len(cta) * cta_size * 0.6 + cta_size * 2.2
        cy = H - pad - cta_h
        parts.append(f'<rect x="{pad}" y="{cy:.0f}" width="{cta_w:.0f}" height="{cta_h:.0f}" rx="{cta_h / 2:.0f}" fill="{st["cta_bg"]}"/>')
        parts.append(f'<text x="{pad + cta_w / 2:.0f}" y="{cy + cta_h / 2 + cta_size * 0.36:.0f}" font-size="{cta_size}" font-weight="700" '
                     f'fill="{st["cta_text"]}" text-anchor="middle">{xml_escape(cta)}</text>')
    url = re.sub(r"^https?://", "", str(ctx.get("area_url", "")))
    if url:
        if not cta or pad + cta_w + f["wm"] + len(url) * f["foot"] * 0.55 < W - pad:
            # beside the pill, right-aligned
            parts.append(f'<text x="{W - pad}" y="{H - pad - cta_h / 2 + f["foot"] * 0.36:.0f}" font-size="{f["foot"]}" font-weight="700" '
                         f'fill="{st["muted"]}" text-anchor="end">{xml_escape(url)}</text>')
        else:
            # no room beside a long pill: sit the URL just above it
            parts.append(f'<text x="{pad}" y="{H - pad - cta_h - f["foot"] * 0.9:.0f}" font-size="{f["foot"]}" font-weight="700" '
                         f'fill="{st["muted"]}">{xml_escape(url)}</text>')
    parts.append("</svg>")
    return "".join(parts)


@router.get("/kit/graphic/{gid}.svg")
def kit_graphic_svg(gid: str, area: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    ar, ctx, html_ctx, tpl = _kit_bundle(db, user, area)
    item = _find(tpl, "graphics", gid) if ar else None
    if ar is None or item is None:
        raise HTTPException(status_code=404, detail="Graphic not found")
    svg = build_graphic_svg(kit.render_deep(item, ctx), ctx)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Content-Disposition": f'inline; filename="{_dom_id(gid)}-{ar.slug}.svg"', "Cache-Control": "no-store"})


@router.get("/kit/graphic/{gid}", response_class=HTMLResponse)
def kit_graphic_page(gid: str, request: Request, area: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    ar, ctx, html_ctx, tpl = _kit_bundle(db, user, area)
    item = _find(tpl, "graphics", gid) if ar else None
    if ar is None or item is None:
        raise HTTPException(status_code=404, detail="Graphic not found")
    g = _render_item(item, ctx, html_ctx)
    fmt = _GRAPHIC_FORMATS.get(str(g.get("format", "")), _GRAPHIC_FORMATS["square"])
    return render_portal(request, "portal/kit_graphic.html", {
        "db": db, "user": user, "section": "kit", "area": ar, "g": g, "fmt": fmt,
        "svg_url": f"/partners/kit/graphic/{quote(gid)}.svg?area={quote(ar.slug)}", "png_name": f"{_dom_id(gid)}-{ar.slug}",
    })


# ---- profile ---------------------------------------------------------------
def _profile_ctx(db: Session, user: User, ok: str = "", err: str = "") -> dict:
    partner = user.partner if user.partner_id else None
    areas = partner_areas(db, user) if partner else []
    s = settings.get_all(db)
    return {
        "db": db, "user": user, "section": "profile", "partner": partner, "areas": areas, "bio_max": BIO_MAX,
        "photo_max_mb": PHOTO_MAX_MB, "wa_display_fallback": s.get("hq_whatsapp_display", ""),
        "photo_url": f"/partners/profile/photo?v={int(datetime.utcnow().timestamp())}" if partner and partner.photo_path else "",
        **_flash(ok, err),
    }


@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, ok: str = "", err: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    return render_portal(request, "portal/profile.html", _profile_ctx(db, user, ok, err))


@router.get("/profile/photo")
def profile_photo(db: Session = Depends(get_db), user: User = Depends(require_partner)):
    partner = user.partner if user.partner_id else None
    if not partner or not partner.photo_path:
        raise HTTPException(status_code=404, detail="No photo")
    path = _safe_upload_path(partner.photo_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No photo")
    return FileResponse(path, media_type=PHOTO_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream"),
                        headers={"Cache-Control": "no-store"})


def _sniff_image(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def _save_photo(partner: Partner, upload: UploadFile) -> str | None:
    """Store the photo as photos/<partner_id>.<ext>; returns an error message or None."""
    name = Path(upload.filename or "").name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in PHOTO_TYPES:
        return "Photo must be a JPG, PNG or WebP file."
    limit = PHOTO_MAX_MB * 1024 * 1024
    data = bytearray()
    while True:
        chunk = upload.file.read(64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            return f"Photo must be {PHOTO_MAX_MB} MB or smaller."
    sniffed = _sniff_image(bytes(data[:16]))
    if not sniffed or (sniffed == "jpg" and ext not in ("jpg", "jpeg")) or (sniffed != "jpg" and sniffed != ext):
        return "That file is not the image type its name says it is."
    ext = "jpg" if sniffed == "jpg" else sniffed
    folder = config.UPLOAD_DIR / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{partner.id}.{ext}"
    if partner.photo_path:
        try:
            old = _safe_upload_path(partner.photo_path)
            if old != target.resolve() and old.is_file():
                old.unlink()
        except HTTPException:
            pass
    target.write_bytes(bytes(data))
    partner.photo_path = f"photos/{partner.id}.{ext}"
    return None


def _remove_photo(partner: Partner) -> None:
    if not partner.photo_path:
        return
    try:
        old = _safe_upload_path(partner.photo_path)
        if old.is_file():
            old.unlink()
    except HTTPException:
        pass
    partner.photo_path = ""


@router.post("/profile")
def profile_save(request: Request, first_name: str = Form(""), name: str = Form(""), phone: str = Form(""),
                 whatsapp: str = Form(""), whatsapp_display: str = Form(""), bio: str = Form(""),
                 remove_photo: str = Form(""), photo: UploadFile | None = File(None),
                 db: Session = Depends(get_db), user: User = Depends(require_partner)):
    partner = user.partner if user.partner_id else None
    if not partner:
        return _redirect("/partners/profile", err="This login has no partner profile to edit.")
    name = " ".join(name.split())[:120]
    first_name = " ".join(first_name.split())[:60]
    phone = " ".join(phone.split())[:40]
    bio = bio.strip()
    if not name:
        return _redirect("/partners/profile", err="Your name is needed: it is what landlords see.")
    if len(bio) > BIO_MAX:
        return _redirect("/partners/profile", err=f"Keep your bio to {BIO_MAX} characters.")
    digits = wa_digits(whatsapp)
    if whatsapp.strip() and not digits:
        return _redirect("/partners/profile", err="That WhatsApp number does not look right. Use the 07 or +44 form.")
    display = " ".join(whatsapp_display.split())[:30] or (wa_display(digits) if digits else "")
    changes = []
    for field, value in (("first_name", first_name), ("name", name), ("phone", phone), ("whatsapp", digits),
                         ("whatsapp_display", display), ("bio", bio)):
        if getattr(partner, field) != value:
            setattr(partner, field, value)
            changes.append(field)
    if remove_photo:
        if partner.photo_path:
            _remove_photo(partner)
            changes.append("photo removed")
    elif photo is not None and photo.filename:
        problem = _save_photo(partner, photo)
        if problem:
            db.rollback()
            return _redirect("/partners/profile", err=problem)
        changes.append("photo")
    if not changes:
        return _redirect("/partners/profile", ok="Nothing changed.")
    db.add(partner)
    audit(db, user, "profile_update", f"partner:{partner.id}", ", ".join(changes))
    db.commit()
    return _redirect("/partners/profile", ok="Profile saved.")


# ---- my area ---------------------------------------------------------------
@router.get("/area", response_class=HTMLResponse)
def my_area(request: Request, ok: str = "", err: str = "", db: Session = Depends(get_db), user: User = Depends(require_partner)):
    areas = partner_areas(db, user)
    requests_q = db.query(Lead).filter(Lead.kind == "partner_request")
    if user.role != "admin":
        requests_q = requests_q.filter(Lead.partner_id == user.partner_id) if user.partner_id else requests_q.filter(false())
    sent = requests_q.order_by(Lead.created_at.desc()).limit(10).all()
    cards = [{"area": a, "maps_url": "https://www.google.com/maps/search/?api=1&query=" + quote(f"{a.name} UK"),
              "hub": f"{config.BASE_URL}/areas/{a.slug}"} for a in areas]
    return render_portal(request, "portal/area.html", {
        "db": db, "user": user, "section": "area", "cards": cards, "sent": sent, **_flash(ok, err),
    })


@router.post("/area/request")
def area_request(request: Request, area_id: int = Form(...), change: str = Form("", alias="request"), reason: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_partner)):
    area = next((a for a in partner_areas(db, user) if a.id == area_id), None)
    if area is None:
        raise HTTPException(status_code=404, detail="Area not found")
    change = change.strip()[:2000]
    reason = reason.strip()[:2000]
    if not change:
        return _redirect("/partners/area", err="Say what you would like changed.")
    partner = user.partner if user.partner_id else None
    lead = Lead(
        kind="partner_request", name=(partner.name if partner else user.email)[:120],
        phone=(partner.phone if partner else "")[:40], email=(partner.email or user.email if partner else user.email)[:200],
        region=area.region, area_id=area.id, partner_id=user.partner_id,
        payload={"request": change, "reason": reason, "area": area.slug}, source_page="/partners/area",
    )
    db.add(lead)
    db.flush()
    audit(db, user, "area_change_request", f"area:{area.slug}", f"lead:{lead.id} {change[:200]}")
    db.commit()
    return _redirect("/partners/area", ok=f"Request sent to head office for {area.name}. You will hear back by WhatsApp or email.")
