"""Partner API: server-to-server seating of partners by Group HQ (plan, Phase 2).

    POST /api/partner/partners                 create or update a partner by email, optionally with an area and a login
    POST /api/partner/partners/{id}/status     {status: pending | active | paused, reason?}
    GET  /api/partner/partners?email=          the partner with its areas and login state (200 with partner: null when none)

Every route sits behind auth.require_api_client: the caller names itself in X-Partner-Client, sends its
secret in X-Service-Token (or as a bearer token), must hold the partners:write scope (partners:read for
the lookup), and is refused outright when the request carries an Origin header. Errors are JSON:
{"ok": false, "reason": <short code>, "message": <sentence>}; successes carry "ok": true and the partner
record. Every answer is Cache-Control: no-store, since one of them carries a one-time password.

Validation, area assignment and the postcode overlap checks reuse the admin router's helpers, so a
partner seated here is exactly what head office would have made by hand. The fields head office owns
(bio, photo, notes, units managed) are never touched; name, contact details, tier and status are the
caller's and are updated on every call, which is what makes a repeated call idempotent.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import (ApiError, audit, generate_password, hash_password, normalise_email, require_api_client)
from ..db import get_db
from ..models import ApiClient, Area, Lead, Partner, User
from ..services import content
from ..services.postcodes import region_for
from .admin import (AREA_REGIONS, EMAIL_RE, PARTNER_TIERS, SLUG_RE, _parse_postcodes, _partner_login, _postcode_clashes,
                    _postcode_nesting, _slugify, _whatsapp)

router = APIRouter(prefix="/api/partner", tags=["partner-api"])

MAX_BODY_BYTES = 64 * 1024
# The statuses a caller may set. "left" is head office's decision and stays in the admin panel.
API_PARTNER_STATUSES = ("pending", "active", "paused")
PLACEHOLDER_SUFFIX = "(to be appointed)"     # the seed's name for an area partner who has not been found yet
NOTE_LIMIT = 2000


# ---- responses ---------------------------------------------------------------
def _reply(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _area_record(area: Area) -> dict:
    return {"id": area.id, "slug": area.slug, "name": area.name, "region": area.region, "status": area.status,
            "postcodes": list(area.postcodes or []), "public": area.is_public}


def partner_record(partner: Partner) -> dict:
    """The shape every route answers with. `id` and `user_id` sit at the top of the record because that is
    where the Group HQ connector reads them; `login` carries the rest of the login state."""
    login = _partner_login(partner)
    return {
        "id": partner.id, "name": partner.name, "first_name": partner.first_name, "email": partner.email,
        "phone": partner.phone, "whatsapp": partner.whatsapp, "whatsapp_display": partner.whatsapp_display,
        "tier": partner.tier, "status": partner.status,
        "franchise": {"fee_waived": partner.franchise_fee_waived, "reason": partner.franchise_note or ""},
        "areas": [_area_record(a) for a in sorted(partner.areas, key=lambda a: (a.sort, a.id))],
        "user_id": login.id if login else None,
        "login": None if login is None else {
            "user_id": login.id, "email": login.email, "is_active": bool(login.is_active),
            "must_change_password": bool(login.must_change_password), "last_login": _iso(login.last_login),
        },
        "created_at": _iso(partner.created_at),
    }


# ---- request parsing ---------------------------------------------------------
async def _json_object(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "request_too_large", f"The request body may be at most {MAX_BODY_BYTES // 1024} KB.")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "request_too_large", f"The request body may be at most {MAX_BODY_BYTES // 1024} KB.")
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _invalid(message: str, field: str) -> ApiError:
    return ApiError(422, "invalid_request", message, field=field)


def _text(data: dict, key: str, *, limit: int, required: bool = False) -> str | None:
    """A trimmed string field. None when the key is absent (an update leaves the stored value alone);
    an explicit null counts as absent too. A required field must be present and non-blank."""
    value = data.get(key)
    if value is None:
        if required:
            raise _invalid(f"{key} is required.", key)
        return None
    if not isinstance(value, str):
        raise _invalid(f"{key} must be text.", key)
    value = value.strip()
    if required and not value:
        raise _invalid(f"{key} is required.", key)
    if len(value) > limit:
        raise _invalid(f"{key} may be at most {limit} characters.", key)
    return value


def _flag(data: dict, key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false.", key)
    return value


def _choice(data: dict, key: str, options: tuple, *, required: bool) -> str | None:
    value = _text(data, key, limit=20, required=required)
    if value is None:
        return None
    value = value.lower()
    if value not in options:
        raise _invalid(f"{key} must be one of {', '.join(options)}.", key)
    return value


def _email(data: dict) -> str:
    email = normalise_email(_text(data, "email", limit=200, required=True))
    if not EMAIL_RE.match(email):
        raise _invalid("email does not look like an email address.", "email")
    return email


def _postcode_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(p, str) for p in value):
        raise _invalid(f"{field} must be a non-empty list of postcode districts, for example [\"SA1\", \"SA2\"].", field)
    codes, err = _parse_postcodes(", ".join(value))
    if err:
        raise _invalid(err, field)
    return codes


def _franchise(data: dict) -> tuple[bool, str] | None:
    """(fee_waived, reason) when the body carries a franchise object; None when it does not."""
    raw = data.get("franchise")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _invalid("franchise must be an object with fee_waived and reason.", "franchise")
    waived = _flag(raw, "fee_waived")
    reason = _text(raw, "reason", limit=NOTE_LIMIT) or ""
    return waived, reason


# ---- partners and areas ------------------------------------------------------
def _is_placeholder(partner: Partner | None) -> bool:
    """A seeded stand-in for an area's partner ("Durham partner (to be appointed)"), never a real person."""
    return (partner is not None and partner.name.endswith(PLACEHOLDER_SUFFIX) and not partner.email
            and not partner.users)


def _find_partner(db: Session, email: str) -> Partner | None:
    return db.query(Partner).filter(func.lower(Partner.email) == email).order_by(Partner.id).first()


def _release_placeholder(db: Session, placeholder: Partner) -> None:
    """An area has just left its placeholder partner (flushed); delete the placeholder when nothing else refers
    to it. The areas collection is reloaded first: it was read before the move, and deleting a partner whose
    loaded collection still holds the area would set that area's partner back to nothing."""
    db.expire(placeholder, ["areas", "users"])
    if placeholder.areas or placeholder.users or db.query(Lead).filter(Lead.partner_id == placeholder.id).count():
        return
    db.delete(placeholder)
    db.flush()


def _assign_existing_area(db: Session, partner: Partner, area: Area) -> None:
    if area.is_hq:
        raise _invalid(f"The area {area.slug} is run by head office and cannot be given to a partner.", "area_slug")
    previous = area.partner if area.partner_id not in (None, partner.id) else None
    if previous is not None and not _is_placeholder(previous):
        raise ApiError(409, "area_assigned",
                       f"The area {area.slug} is assigned to another partner; move it in the admin panel first.",
                       area_slug=area.slug)
    area.partner_id = partner.id
    db.add(area)
    db.flush()
    if previous is not None:
        _release_placeholder(db, previous)


def _area_spec(data: dict) -> tuple[str, dict] | None:
    """Which of area_slug, new_area and postcodes the body carries, as (kind, spec); at most one is allowed."""
    given = [k for k in ("area_slug", "new_area", "postcodes") if data.get(k) is not None]
    if not given:
        return None
    if len(given) > 1:
        raise _invalid("Send one of area_slug, new_area or postcodes, not several.", given[1])
    kind = given[0]
    if kind == "area_slug":
        slug = _text(data, "area_slug", limit=60, required=True).lower()
        if not SLUG_RE.match(slug):
            raise _invalid("area_slug may only contain lower-case letters, numbers and single hyphens.", "area_slug")
        return kind, {"slug": slug}
    if kind == "postcodes":
        codes = _postcode_list(data.get("postcodes"), "postcodes")
        # Bare postcodes carry no name, so the area is named after its districts and stays "coming" until head
        # office names it properly and publishes it (see README, Partner API).
        shown = codes[:6]
        name = (", ".join(shown[:-1]) + " and " + shown[-1]) if len(shown) > 1 else shown[0]
        if len(codes) > len(shown):
            name += f" and {len(codes) - len(shown)} more districts"
        return kind, {"slug": _slugify(" ".join(shown)), "name": name, "region": None, "postcodes": codes, "covers": "",
                      "derived": True}
    raw = data.get("new_area")
    if not isinstance(raw, dict):
        raise _invalid("new_area must be an object with slug, name, region, postcodes and optionally covers.", "new_area")
    name = _text(raw, "name", limit=120, required=True)
    slug = (_text(raw, "slug", limit=60) or _slugify(name)).lower()
    if not SLUG_RE.match(slug):
        raise _invalid("new_area.slug may only contain lower-case letters, numbers and single hyphens.", "new_area.slug")
    region = _choice(raw, "region", AREA_REGIONS, required=False)
    codes = _postcode_list(raw.get("postcodes"), "new_area.postcodes")
    covers = _text(raw, "covers", limit=4000) or ""
    return kind, {"slug": slug, "name": name, "region": region, "postcodes": codes, "covers": covers, "derived": False}


def _create_area(db: Session, partner: Partner, spec: dict, partner_status: str) -> Area:
    letters = re.match(r"^[A-Z]{1,2}", spec["postcodes"][0]).group(0)
    region = spec["region"] or region_for(letters)
    if region not in AREA_REGIONS:
        raise _invalid(f"Hello New Home operates in England and Wales; {spec['postcodes'][0]} is in "
                       f"{region.replace('-', ' ').title()}.", "postcodes")
    clash = _postcode_clashes(db, spec["postcodes"], None)
    if clash:
        raise ApiError(409, "postcode_clash", clash)
    # A new area is published with the partner when the caller asks for an active partner; one named after its
    # postcodes (no real name yet) always starts as "coming".
    status = "active" if (partner_status == "active" and not spec["derived"]) else "coming"
    next_sort = int(db.query(func.max(Area.sort)).scalar() or 0) + 10
    area = Area(slug=spec["slug"], name=spec["name"], region=region, role="partner", status=status,
                partner_id=partner.id, covers=spec["covers"], postcodes=spec["postcodes"], places=[],
                content=content.bespoke_area_content(spec["slug"]) or {}, sort=next_sort)
    db.add(area)
    db.flush()
    return area


def _resolve_area(db: Session, partner: Partner, area_spec: tuple[str, dict] | None,
                  partner_status: str) -> tuple[Area | None, bool, str]:
    """(area, created, note). An existing slug (sent as area_slug, or as the slug of a new_area that already
    exists, which is what a repeated call sends) is assigned; anything else is created. A new_area or
    postcodes whose slug exists with different postcodes is refused (409 slug_taken) rather than assigned
    with the body's postcodes silently dropped: the caller's template and head office's territory disagree,
    and one of them has to be put right by a person."""
    if area_spec is None:
        return None, False, ""
    kind, spec = area_spec
    existing = db.query(Area).filter(Area.slug == spec["slug"]).first()
    if existing is not None:
        if kind != "area_slug" and sorted(existing.postcodes or []) != sorted(spec["postcodes"]):
            held = ", ".join(existing.postcodes or []) or "none"
            raise ApiError(409, "slug_taken",
                           f"The area {spec['slug']} already exists with postcodes {held}, not the ones sent; send "
                           f"area_slug to assign it as head office set it, or a different slug.",
                           area_slug=existing.slug, postcodes=list(existing.postcodes or []))
        _assign_existing_area(db, partner, existing)
        return existing, False, ""
    if kind == "area_slug":
        raise _invalid(f"No area has the slug {spec['slug']}. Send new_area to create one.", "area_slug")
    area = _create_area(db, partner, spec, partner_status)
    return area, True, _postcode_nesting(db, spec["postcodes"], area.id)


def _get_partner(db: Session, partner_id: int) -> Partner:
    partner = db.get(Partner, partner_id)
    if partner is None:
        raise ApiError(404, "no_partner", f"No partner has the id {partner_id}.")
    return partner


# ---- routes ------------------------------------------------------------------
@router.post("/partners")
async def upsert_partner(request: Request, db: Session = Depends(get_db),
                         client: ApiClient = Depends(require_api_client("partners:write"))):
    data = await _json_object(request)
    email = _email(data)
    name = _text(data, "name", limit=120, required=True)
    first_name = _text(data, "first_name", limit=60)
    phone = _text(data, "phone", limit=40)
    whatsapp_raw = _text(data, "whatsapp", limit=40)
    whatsapp_display = _text(data, "whatsapp_display", limit=40)
    tier = _choice(data, "tier", PARTNER_TIERS, required=True)
    status = _choice(data, "status", API_PARTNER_STATUSES, required=True)
    create_login = _flag(data, "create_login")
    franchise = _franchise(data)
    area_spec = _area_spec(data)
    whatsapp = None
    if whatsapp_raw is not None:
        whatsapp, err = _whatsapp(whatsapp_raw)
        if err:
            raise _invalid(err, "whatsapp")

    # A login with this address that belongs to someone else (another partner, or an admin) blocks the call before
    # anything is written: the address is the person key, and it cannot point at two people.
    existing_login = db.query(User).filter(func.lower(User.email) == email).first()
    partner = _find_partner(db, email)
    if partner is None and area_spec is not None and area_spec[0] == "area_slug":
        # The territory's seeded placeholder becomes this person, as the seed itself does, rather than lingering.
        seeded = db.query(Area).filter(Area.slug == area_spec[1]["slug"]).first()
        if seeded is not None and _is_placeholder(seeded.partner):
            partner = seeded.partner
    if existing_login is not None and (partner is None or existing_login.partner_id != partner.id):
        raise ApiError(409, "email_in_use", "That email address already belongs to another login.")

    created = partner is None or _is_placeholder(partner)
    if partner is None:
        partner = Partner(name=name)
        db.add(partner)
    partner.name = name
    partner.email = email
    partner.tier = tier
    partner.status = status
    if first_name is not None:
        partner.first_name = first_name
    if phone is not None:
        partner.phone = phone
    if whatsapp is not None:
        partner.whatsapp = whatsapp
    if whatsapp_display is not None:
        partner.whatsapp_display = whatsapp_display
    if franchise is not None:
        partner.franchise_fee_waived, partner.franchise_note = franchise
    db.add(partner)
    db.flush()

    area, area_created, note = _resolve_area(db, partner, area_spec, status)

    login = _partner_login(partner)
    one_time_password = None
    if create_login and login is None:
        one_time_password = generate_password(12)
        login = User(email=email, password_hash=hash_password(one_time_password), role="partner", partner_id=partner.id,
                     is_active=True, must_change_password=True)
        db.add(login)
        db.flush()

    audit(db, None, "partner_api.create" if created else "partner_api.update", f"partner:{partner.id}",
          f"via api client {client.name}: {partner.name}; status {partner.status}; tier {partner.tier}; "
          f"area {(area.slug + (' (created)' if area_created else '')) if area else 'none'}; "
          f"login {'created' if one_time_password else ('present' if login else 'none')}; "
          f"franchise {'fee waived' if partner.franchise_fee_waived else ('fee payable' if partner.franchise_fee_waived is False else 'not recorded')}")
    db.commit()
    db.refresh(partner)
    return _reply({
        "ok": True, "created": created, "partner_id": partner.id, "user_id": login.id if login else None,
        "one_time_password": one_time_password, "area_slug": area.slug if area else None, "area_created": area_created,
        "note": note, "partner": partner_record(partner),
    }, 201 if created else 200)


@router.post("/partners/{partner_id}/status")
async def set_partner_status(partner_id: int, request: Request, db: Session = Depends(get_db),
                             client: ApiClient = Depends(require_api_client("partners:write"))):
    data = await _json_object(request)
    status = _choice(data, "status", API_PARTNER_STATUSES, required=True)
    reason = _text(data, "reason", limit=NOTE_LIMIT) or ""
    partner = _get_partner(db, partner_id)
    previous = partner.status
    if status != previous:
        partner.status = status
        db.add(partner)
        audit(db, None, "partner_api.status", f"partner:{partner.id}",
              f"via api client {client.name}: {partner.name}: {previous} to {status}" + (f"; {reason}" if reason else ""))
        db.commit()
        db.refresh(partner)
    return _reply({"ok": True, "partner_id": partner.id, "status": partner.status, "previous_status": previous,
                   "changed": status != previous, "partner": partner_record(partner)})


@router.get("/partners")
def lookup_partner(email: str = "", db: Session = Depends(get_db),
                   client: ApiClient = Depends(require_api_client("partners:read"))):
    email = normalise_email(email)
    if not EMAIL_RE.match(email):
        raise _invalid("Pass the partner's email address as ?email=.", "email")
    partner = _find_partner(db, email)
    if partner is None:
        return _reply({"ok": True, "found": False, "partner": None})
    return _reply({"ok": True, "found": True, "partner": partner_record(partner)})
