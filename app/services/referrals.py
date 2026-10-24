"""Partner referrals (revision 0006): every partner's referral code and link, the cookie that remembers who sent a
visitor, and the referring partner recorded on leads and instructions.

How a referral is recorded:

* ?ref=<code> on any public page (anything rendered through public.render_public: the home page, every brand page,
  the area pages, the commercial pages, even the 404 page) sets the first-party cookie `hnh_ref` for 90 days, but only
  when the code is an active partner's. An unknown code, or a paused or departed partner's, changes nothing, so a
  stale link never overwrites a live one. A newer valid link replaces an older one (the last partner whose link was
  followed is the one credited). A code head office replaces is kept for its partner (retired_referral_codes) and
  never given to anyone else, so an old link or a live cookie carrying it credits nobody rather than a stranger.
* Every lead the public forms post to /api/lead (rent review, short-let appraisal, commercial appraisal, contact,
  property alert, partner application) records the partner the cookie names as the lead's referrer, provided that
  partner is still active when the lead arrives: a paused or departed partner's introductions stop, as the heads of
  terms say they do when mentee status lapses. What was recorded before stays.
* A property instruction from Property Predator or Key Properties records the partner whose email is the
  introducer's email (introducer.person_key), on the same active-partner rule, when the sender vouches for that email
  (partner_api.introducer_vouched); an unvouched match is shown to head office to credit by hand. An instruction head
  office makes from a lead (Admin -> the lead -> Create instruction) carries the lead's referrer. Head office can set
  or clear the referrer by hand on the lead and instruction pages (for an introduction made by phone or WhatsApp).
* ?seller=<code> on the partner-programme page (Group HQ's franchise seller code: the tracked link Group HQ gives
  sellers is /partners-programme?seller=<code>) sets `hnh_seller` for 90 days; a partner-programme application keeps
  it in leads.seller_code. Any other page ignores ?seller=. Hello New Home forwards nothing to Group HQ, so head
  office copies the code onto the franchise opportunity there by hand.

Nothing here is a fee: Hello New Home holds no commission figures. Head office works out introducer pay by hand from
Admin -> Referrals (a CSV of leads and instructions by referring partner and month), and the partner portal shows a
partner their introductions with their status only.
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config
from ..models import REFERRAL_CODE_MAX, Instruction, Lead, Partner, RetiredReferralCode
from .postcodes import normalise as normalise_postcode
from .times import fmt_uk

REF_COOKIE = "hnh_ref"
SELLER_COOKIE = "hnh_seller"
COOKIE_DAYS = 90
COOKIE_MAX_AGE = COOKIE_DAYS * 24 * 60 * 60
REF_PARAM = "ref"
SELLER_PARAM = "seller"
# The one page whose ?seller= is remembered: Group HQ's tracked link for franchise sellers points here, and only a
# partner-programme application keeps the code.
SELLER_PAGE = "/partners-programme"

CODE_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9-]{1,18})[A-Z0-9]$")
# Group HQ's rule for a seller code (commission.code_problem): letters, digits, dashes and underscores, at most 24.
SELLER_CODE_MAX = 24
SELLER_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,23}$")

VIA_LINK = "link"                    # the hnh_ref cookie set by the partner's link
VIA_INTRODUCER = "introducer"        # Property Predator or Key Properties named the partner as introducer
VIA_LEAD = "lead"                    # an instruction head office made from a referred lead
VIA_HEAD_OFFICE = "head office"      # set by hand on the lead or instruction page
VIA_LABELS = {VIA_LINK: "their link", VIA_INTRODUCER: "introducer email", VIA_LEAD: "from the lead",
              VIA_HEAD_OFFICE: "set by head office", "": ""}


# ---- codes and links ------------------------------------------------------------------------------------------------
def normalise_code(raw) -> str:
    """The stored form of a referral code: trimmed and upper-case."""
    return str(raw or "").strip().upper()


def code_problem(code: str) -> str:
    """Why a code typed by head office is refused, or "" when it can be used in a link."""
    if not code:
        return "A referral code is required."
    if len(code) < 3 or len(code) > REFERRAL_CODE_MAX:
        return f"A referral code is 3 to {REFERRAL_CODE_MAX} characters."
    if not CODE_RE.match(code):
        return "A referral code is letters, digits and hyphens only, starting and ending with a letter or digit."
    return ""


def code_taken(db: Session, code: str, *, exclude_id: int | None = None) -> str:
    """Why `code` cannot be given to the partner `exclude_id` (None for a new one), or "" when it is free: another
    partner holds it, or held it until head office changed it. A code given up stays with its partner for good, so
    a link or a cookie that still carries it can never credit someone else; the partner may take it back."""
    code = normalise_code(code)
    holder = db.query(Partner.id).filter(func.upper(Partner.referral_code) == code)
    retired = db.query(RetiredReferralCode.id).filter(func.upper(RetiredReferralCode.code) == code)
    if exclude_id is not None:
        holder = holder.filter(Partner.id != exclude_id)
        retired = retired.filter(RetiredReferralCode.partner_id != exclude_id)
    if holder.first() is not None:
        return f"The referral code {code} belongs to another partner."
    if retired.first() is not None:
        return (f"The referral code {code} was another partner's until it was changed, so it stays theirs: links "
                "they shared with it must never credit someone else.")
    return ""


def change_code(db: Session, partner: Partner, code: str) -> str:
    """Give `partner` the (already checked) code `code` and keep the one it replaces as theirs. Returns the old code,
    or "" when there was none or nothing changed."""
    code = normalise_code(code)
    before = partner.referral_code or ""
    if code == before:
        return ""
    own = (db.query(RetiredReferralCode)
           .filter(func.upper(RetiredReferralCode.code) == code, RetiredReferralCode.partner_id == partner.id).first())
    if own is not None:
        db.delete(own)                        # the partner takes an old code of theirs back: it is live again
    if before:
        db.add(RetiredReferralCode(code=before, partner_id=partner.id))
    partner.referral_code = code
    return before


def referral_link(code: str | None, path: str = "/") -> str:
    """The public link that credits `code`: the home page by default, or any public path."""
    if not code:
        return ""
    return f"{config.BASE_URL}{path}?{REF_PARAM}={code}"


def portal_url() -> str:
    return f"{config.BASE_URL}/partners/login"


def admin_url(partner_id: int) -> str:
    return f"{config.BASE_URL}/admin/partners/{partner_id}"


def primary_area(partner: Partner):
    """The partner's first area in the admin's order (sort, then id), or None while head office has not assigned one."""
    areas = sorted(partner.areas, key=lambda a: (a.sort, a.id))
    return areas[0] if areas else None


# ---- who is credited ------------------------------------------------------------------------------------------------
def active_partner_for_code(db: Session, code: str) -> Partner | None:
    code = normalise_code(code)
    if not code or code_problem(code):
        return None
    return (db.query(Partner).filter(Partner.referral_code == code, Partner.status == "active")
            .order_by(Partner.id).first())


def active_partner_for_email(db: Session, email: str) -> Partner | None:
    """The active partner whose email is `email` (compared lower-cased): an introducer named by a sending system."""
    email = (email or "").strip().lower()
    if not email:
        return None
    return (db.query(Partner).filter(func.lower(Partner.email) == email, Partner.status == "active")
            .order_by(Partner.id).first())


def active_partners_by_email(db: Session) -> dict[str, Partner]:
    """Every active partner by lower-cased email (the first by id when two share one), for the admin's hint on an
    instruction whose introducer email is a partner's but was not credited (unverified, or cleared by hand)."""
    out: dict[str, Partner] = {}
    for partner in db.query(Partner).filter(Partner.status == "active").order_by(Partner.id).all():
        key = (partner.email or "").strip().lower()
        if key and key not in out:
            out[key] = partner
    return out


def referrer_from_cookie(request: Request, db: Session) -> Partner | None:
    """The partner the visitor's hnh_ref cookie names, when that partner is active now; otherwise None."""
    return active_partner_for_code(db, request.cookies.get(REF_COOKIE, ""))


def seller_from_cookie(request: Request) -> str:
    raw = str(request.cookies.get(SELLER_COOKIE, "") or "").strip()
    return raw if SELLER_CODE_RE.match(raw) else ""


def remember(request: Request, db: Session, response: Response) -> None:
    """Set the referral cookies from ?ref= (any public page) and ?seller= (the partner-programme page only). A ref is
    remembered only when it is an active partner's code; a seller code only when it has the shape Group HQ issues.
    Anything else is ignored and leaves an existing cookie alone."""
    if request.method not in ("GET", "HEAD"):
        return
    params = request.query_params
    ref = params.get(REF_PARAM)
    if ref:
        partner = active_partner_for_code(db, ref)
        if partner is not None:
            _set(response, REF_COOKIE, partner.referral_code)
    seller = str(params.get(SELLER_PARAM) or "").strip()
    if seller and SELLER_CODE_RE.match(seller) and request.url.path == SELLER_PAGE:
        _set(response, SELLER_COOKIE, seller)


def _set(response: Response, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
                        secure=config.SECURE_COOKIES, path="/")


def credit(record: Lead | Instruction, partner: Partner | None, via: str) -> None:
    """Record `partner` as the referrer of a lead or instruction (None clears it)."""
    record.referrer_partner_id = partner.id if partner is not None else None
    record.referrer_via = via if partner is not None else ""


def describe(record: Lead | Instruction) -> str:
    """"Name (CODE), their link" for the admin screens; "" when nobody referred it."""
    partner = record.referrer
    if partner is None:
        return ""
    via = VIA_LABELS.get(record.referrer_via or "", record.referrer_via or "")
    return f"{partner.name} ({partner.referral_code})" + (f", {via}" if via else "")


# ---- the admin export and the portal list ---------------------------------------------------------------------------
def month_of(value: datetime | None) -> str:
    """YYYY-MM in UK time: the month a lead or instruction counts in."""
    return fmt_uk(value, "%Y-%m") if value else ""


def parse_month(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", raw) else ""


def referred_records(db: Session, *, partner_id: int | None = None, month_from: str = "", month_to: str = "") -> list[dict]:
    """Every referred lead and instruction as one row each, ordered by referring partner, month and time: what the
    Referrals CSV holds and the Referrals page counts. Months are compared in UK time, so a lead at 00:30 on 1 November
    (UK) counts in November whatever the UTC date."""
    rows: list[dict] = []
    leads = db.query(Lead).filter(Lead.referrer_partner_id.isnot(None))
    instructions_q = db.query(Instruction).filter(Instruction.referrer_partner_id.isnot(None))
    if partner_id is not None:
        leads = leads.filter(Lead.referrer_partner_id == partner_id)
        instructions_q = instructions_q.filter(Instruction.referrer_partner_id == partner_id)
    for lead in leads.all():
        rows.append({"record": "lead", "id": lead.id, "ref": f"lead {lead.id}", "created_at": lead.created_at,
                     "month": month_of(lead.created_at), "referrer": lead.referrer, "via": lead.referrer_via,
                     "what": lead.kind, "area": lead.area.name if lead.area else "",
                     "postcode": normalise_postcode(lead.postcode) or "",
                     "status": lead.status, "routed_to": lead.partner.name if lead.partner else "head office",
                     "seller_code": lead.seller_code or ""})
    for inst in instructions_q.all():
        rows.append({"record": "instruction", "id": inst.id, "ref": inst.ref or "", "created_at": inst.created_at,
                     "month": month_of(inst.created_at), "referrer": inst.referrer, "via": inst.referrer_via,
                     "what": inst.service_line, "area": inst.area.name if inst.area else "", "postcode": inst.outward,
                     "status": inst.status, "routed_to": inst.partner.name if inst.partner else "head office",
                     "seller_code": ""})
    if month_from:
        rows = [r for r in rows if r["month"] >= month_from]
    if month_to:
        rows = [r for r in rows if r["month"] <= month_to]
    rows.sort(key=lambda r: ((r["referrer"].name or "").lower(), r["referrer"].id, r["month"], r["created_at"], r["record"], r["id"]))
    return rows


def monthly_summary(rows: list[dict]) -> list[dict]:
    """One line per referring partner and month: leads, instructions, and how many instructions are instructed or let
    (the ones a fee could follow). Counts only."""
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r["referrer"].id, r["month"])
        line = out.setdefault(key, {"partner": r["referrer"], "month": r["month"], "leads": 0, "instructions": 0,
                                    "instructed": 0})
        if r["record"] == "lead":
            line["leads"] += 1
        else:
            line["instructions"] += 1
            if r["status"] in ("instructed", "let"):
                line["instructed"] += 1
    return list(out.values())


def introductions(db: Session, partner_id: int) -> tuple[list[Lead], list[Instruction]]:
    """What the partner portal lists as the partner's introductions, newest first."""
    leads = (db.query(Lead).filter(Lead.referrer_partner_id == partner_id)
             .order_by(Lead.created_at.desc(), Lead.id.desc()).all())
    insts = (db.query(Instruction).filter(Instruction.referrer_partner_id == partner_id)
             .order_by(Instruction.created_at.desc(), Instruction.id.desc()).all())
    return leads, insts
