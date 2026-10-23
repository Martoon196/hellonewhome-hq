"""Partner referrals (revision 0006): referral codes, the ?ref= and ?seller= cookies, the referring partner on leads and
instructions, contract 3 of the mentee-earnings packet on the Partner API, the admin Referrer column, filter, by-hand
select and Referrals export, an instruction made from a lead, and the partner portal's Introductions page.

Every visitor here is a fresh TestClient: the suite's shared anonymous client must never carry a referral cookie into
another module's leads. Every test makes the leads and instructions it asserts on, so each one passes on its own
(`-k`) and in any order. The whole file runs on SQLite and, with HNH_TEST_DATABASE_URL, on Postgres; the migration
tests run on throwaway SQLite files, and on a throwaway Postgres schema when that variable is set."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, text

from app import auth, config, migrations, models
from app.db import make_engine
from app.main import app
from app.models import ApiClient, AuditLog, Instruction, Lead, Partner, RetiredReferralCode
from app.routers import partner_api
from app.services import mail, referrals
from tests.conftest import TEST_DATABASE_URL, make_partner, partner_client, session

GROUPHQ = "intro-grouphq"
SENDER = "intro-sender"
SENDER_KP = "intro-kp"            # stands in for a client whose sign-in proves the email (Key Properties)
SELLER = "JA-7K3M"
RENT_REVIEW = {"_subject": "Free rent review request", "name": "Lara Landlord", "phone": "07700 900321",
               "email": "lara.landlord@example.test", "postcode": "CV1 2AB", "address": "4 Spon Street, Coventry",
               "type": "House", "bedrooms": "3", "interest": "Full management", "_page": "/valuation"}
CODE_SHAPE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$")


# ---- fixtures and helpers ----------------------------------------------------------------
@pytest.fixture(scope="module")
def rita(client):
    """The referring partner: her area is Nottingham, and she sends landlords anywhere."""
    return make_partner(name="Rita Referrer", first_name="Rita", email="rita.referrer@example.test", slug="nottingham-ref",
                        area_name="Nottingham", postcodes=["NG1", "NG2"], whatsapp="447700900601",
                        whatsapp_display="07700 900601", sort=600)


@pytest.fixture(scope="module")
def cal(client):
    """The partner who serves the area Rita's landlord is in."""
    return make_partner(name="Cal Coventry", first_name="Cal", email="cal.coventry@example.test", slug="coventry-ref",
                        area_name="Coventry", postcodes=["CV1", "CV2"], whatsapp="447700900602",
                        whatsapp_display="07700 900602", sort=610)


@pytest.fixture(scope="module")
def paula(client):
    """A paused partner: her link and her email no longer credit anyone."""
    p = make_partner(name="Paula Paused", first_name="Paula", email="paula.paused@example.test", slug="wolverhampton-ref",
                     area_name="Wolverhampton", postcodes=["WV1"], whatsapp="", whatsapp_display="", sort=620)
    with session() as db:
        db.get(Partner, p.partner_id).status = "paused"
        db.commit()
    return p


@pytest.fixture(scope="module")
def sender(client) -> TestClient:
    """A Partner API client with properties:write, sending as Property Predator does (origin pp)."""
    return _api(_make_client(SENDER, "properties:write"), SENDER)


def _code(partner) -> str:
    with session() as db:
        return db.get(Partner, partner.partner_id).referral_code


def _visitor() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _post_lead(c: TestClient, **fields) -> Lead:
    r = c.post("/api/lead", json=fields)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    with session() as db:
        lead = db.get(Lead, r.json()["id"])
        db.expunge(lead)
    return lead


def _make_client(name: str, scopes: str) -> str:
    with session() as db:
        existing = db.query(ApiClient).filter(ApiClient.name == name).first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        _, token = auth.create_api_client(db, name, scopes)
        db.commit()
    return token


def _api(token: str, name: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.headers.update({"X-Service-Token": token, "X-Partner-Client": name})
    return c


def _property(external_ref: str, introducer_email: str, postcode: str = "CV2 1AA", verified: bool | None = True) -> dict:
    """An instruction as Property Predator sends it; `verified` is its introducer.email_verified (None leaves it out)."""
    introducer = {"person_key": introducer_email, "pp_account_id": "acct_ref"}
    if verified is not None:
        introducer["email_verified"] = verified
    return {"external_ref": external_ref, "origin_system": "pp", "introducer": introducer,
            "landlord": {"name": "Peter Property", "email": "peter.property@example.test", "phone": "07700 900654"},
            "consent": {"given": True, "text_version": "hnh-landlord-2026-10"},
            "property": {"address": "8 Far Gosford Street, Coventry", "postcode": postcode, "bedrooms": 2,
                         "rent_expectation": 875}}


def _send(api: TestClient, external_ref: str, introducer_email: str, **kw) -> int:
    """Send an instruction (or resend it: the same ref answers 200) and return its id."""
    r = api.post("/api/partner/properties", json=_property(external_ref, introducer_email, **kw))
    assert r.status_code in (200, 201), r.text
    with session() as db:
        return db.query(Instruction.id).filter(Instruction.external_ref == external_ref).scalar()


def _instruct(admin_client: TestClient, lead: Lead) -> int:
    """Head office presses Create instruction on the lead; the id of the instruction it opens."""
    r = admin_client.post(f"/admin/leads/{lead.id}/instruction")
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/instructions/"), r.text
    return int(r.headers["location"].split("/")[3].split("?")[0])


def _flash(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def _set_cookie(r) -> str:
    return "; ".join(r.headers.get_list("set-cookie"))


def _partner_form(p: Partner, **overrides) -> dict:
    form = {"name": p.name, "first_name": p.first_name, "email": p.email, "phone": p.phone, "whatsapp": p.whatsapp,
            "whatsapp_display": p.whatsapp_display, "bio": p.bio, "tier": p.tier, "status": p.status,
            "units_managed": str(p.units_managed), "notes": p.notes, "areas": [str(a.id) for a in p.areas],
            "referral_code": p.referral_code or ""}
    form.update(overrides)
    return form


def _csv(text_body: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text_body.lstrip("﻿"))))


# ---- (a) referral codes --------------------------------------------------------------------
def test_every_partner_has_a_unique_referral_code(client, rita, cal, paula):
    with session() as db:
        codes = [p.referral_code for p in db.query(Partner).all()]
        assert codes and all(codes) and len(set(codes)) == len(codes)
        assert all(referrals.code_problem(c) == "" for c in codes)
        assert CODE_SHAPE.match(db.get(Partner, rita.partner_id).referral_code)
        # any path that creates a partner gets one when the row is written, several in one flush included
        a, b = Partner(name="Flush One"), Partner(name="Flush Two")
        db.add_all([a, b])
        db.flush()
        assert CODE_SHAPE.match(a.referral_code) and CODE_SHAPE.match(b.referral_code) and a.referral_code != b.referral_code
        db.rollback()


def test_head_office_sets_and_changes_the_code_on_the_partner_page(admin_client, rita, cal):
    # a new partner left blank is given a code; one typed in lower case is stored upper case
    r = admin_client.post("/admin/partners/new", data={"name": "Nadia New", "email": "nadia.new@example.test", "tier": "area",
                                                        "status": "pending", "units_managed": "0", "referral_code": ""})
    assert r.status_code == 303, r.text
    nadia_id = int(r.headers["location"].split("/")[3].split("?")[0])
    with session() as db:
        nadia = db.get(Partner, nadia_id)
        assert CODE_SHAPE.match(nadia.referral_code)
        r = admin_client.post(f"/admin/partners/{nadia_id}", data=_partner_form(nadia, referral_code=" nadia-cv "))
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.text
    with session() as db:
        nadia = db.get(Partner, nadia_id)
        assert nadia.referral_code == "NADIA-CV"
        entry = (db.query(AuditLog).filter(AuditLog.target == f"partner:{nadia_id}", AuditLog.action == "partner.update")
                 .order_by(AuditLog.id.desc()).first())
        assert "referral code NADIA-CV (was " in entry.detail
        form = _partner_form(nadia)
    # refused: another partner's code in any case, a bad shape, and a blank on an existing partner
    for typed, words in ((_code(rita).lower(), "belongs to another partner"), ("a", "3 to 20"), ("NO SPACES", "letters, digits"),
                         ("-ABC", "letters, digits"), ("", "required")):
        r = admin_client.post(f"/admin/partners/{nadia_id}", data=dict(form, referral_code=typed))
        assert r.status_code == 400 and words in r.text, typed
    # a page rendered before the field existed posts none, and the code stays as it is
    stale = {k: v for k, v in form.items() if k != "referral_code"}
    r = admin_client.post(f"/admin/partners/{nadia_id}", data=stale)
    assert r.status_code == 303
    with session() as db:
        assert db.get(Partner, nadia_id).referral_code == "NADIA-CV"
    page = admin_client.get(f"/admin/partners/{nadia_id}").text
    assert 'name="referral_code"' in page and 'value="NADIA-CV"' in page
    assert f"{config.BASE_URL}/?ref=NADIA-CV" in page and 'data-copy-target="#partner-ref-link"' in page
    assert "NADIA-CV" in admin_client.get("/admin/partners").text


def test_a_code_given_up_stays_with_its_partner_and_never_credits_another(client, admin_client):
    rhea = make_partner(name="Rhea Retires", first_name="Rhea", email="rhea.retires@example.test", slug="lincoln-ref",
                        area_name="Lincoln", postcodes=["LN1"], whatsapp="", whatsapp_display="", sort=630)
    carl = make_partner(name="Carl Claims", first_name="Carl", email="carl.claims@example.test", slug="peterborough-ref",
                        area_name="Peterborough", postcodes=["PE1"], whatsapp="", whatsapp_display="", sort=640)
    old = _code(rhea)
    visitor = _visitor()
    visitor.get("/", params={"ref": old})                       # a live 90-day cookie carrying Rhea's first code
    with session() as db:
        form = _partner_form(db.get(Partner, rhea.partner_id), referral_code="RHEA-NEW")
    assert admin_client.post(f"/admin/partners/{rhea.partner_id}", data=form).status_code == 303
    with session() as db:
        assert db.get(Partner, rhea.partner_id).referral_code == "RHEA-NEW"
        assert [(r.code, r.partner_id) for r in db.query(RetiredReferralCode).filter(RetiredReferralCode.partner_id == rhea.partner_id)] == [(old, rhea.partner_id)]
        carl_form = _partner_form(db.get(Partner, carl.partner_id), referral_code=old.lower())
    # nobody else may be given it, on an existing partner or a new one
    r = admin_client.post(f"/admin/partners/{carl.partner_id}", data=carl_form)
    assert r.status_code == 400 and "was another partner&#39;s" in r.text, r.text[:400]
    r = admin_client.post("/admin/partners/new", data={"name": "Nell Newcomer", "email": "nell.newcomer@example.test",
                                                        "tier": "area", "status": "pending", "units_managed": "0",
                                                        "referral_code": old})
    assert r.status_code == 400 and "was another partner&#39;s" in r.text
    with session() as db:
        assert db.get(Partner, carl.partner_id).referral_code != old
        # the generator never draws a retired code either
        draws = iter(list(old) + list("ZZZZZ9"))
        original = models.secrets.choice
        models.secrets.choice = lambda _alphabet: next(draws)
        try:
            assert models.new_referral_code(db) == "ZZZZZ9"
        finally:
            models.secrets.choice = original
    # the old code credits nobody: not Rhea (the change stops her old links) and never a stranger
    lead = _post_lead(visitor, **dict(RENT_REVIEW, name="Olive Oldlink"))
    assert lead.referrer_partner_id is None
    assert referrals.REF_COOKIE not in _set_cookie(_visitor().get("/", params={"ref": old}))
    # Rhea may take her old code back; the one she gives up is then kept for her in its place
    with session() as db:
        form = _partner_form(db.get(Partner, rhea.partner_id), referral_code=old)
    assert admin_client.post(f"/admin/partners/{rhea.partner_id}", data=form).status_code == 303
    with session() as db:
        assert db.get(Partner, rhea.partner_id).referral_code == old
        assert {r.code for r in db.query(RetiredReferralCode).filter(RetiredReferralCode.partner_id == rhea.partner_id)} == {"RHEA-NEW"}
        entry = (db.query(AuditLog).filter(AuditLog.target == f"partner:{rhea.partner_id}", AuditLog.action == "partner.update")
                 .order_by(AuditLog.id.desc()).first())
        assert f"referral code {old} (was RHEA-NEW)" in entry.detail


# ---- (c) contract 3 on the Partner API -------------------------------------------------------
def test_contract_3_fields_on_upsert_status_and_lookup(client):
    api = _api(_make_client(GROUPHQ, "partners:write"), GROUPHQ)
    body = {"email": "mo.mentee@example.test", "name": "Mo Mentee", "tier": "area", "status": "active", "create_login": True,
            "franchise": {"fee_waived": True, "reason": "Group HQ: Mentorship"}}
    r = api.post("/api/partner/partners", json=body)
    assert r.status_code == 201, r.text
    record = r.json()["partner"]
    pid, code = record["id"], record["referral_code"]
    assert CODE_SHAPE.match(code)
    expected = {"referral_code": code, "referral_link": f"{config.BASE_URL}/?ref={code}", "area": None, "tier": "area",
                "status": "active", "portal_url": f"{config.BASE_URL}/partners/login",
                "admin_url": f"{config.BASE_URL}/admin/partners/{pid}"}
    assert {k: record[k] for k in expected} == expected
    # the lookup answers the same; an area assigned by head office then shows as {slug, name}
    found = api.get("/api/partner/partners", params={"email": "MO.MENTEE@example.test"}).json()
    assert found["found"] and {k: found["partner"][k] for k in expected} == expected
    r = api.post("/api/partner/partners", json=dict(body, new_area={"slug": "mo-town", "name": "Mo Town", "postcodes": ["DY1"]}))
    assert r.status_code == 200 and r.json()["partner"]["referral_code"] == code          # an update never re-draws it
    assert r.json()["partner"]["area"] == {"slug": "mo-town", "name": "Mo Town"}
    r = api.post(f"/api/partner/partners/{pid}/status", json={"status": "paused"})
    assert r.json()["partner"]["status"] == "paused" and r.json()["partner"]["referral_code"] == code
    assert r.json()["partner"]["area"] == {"slug": "mo-town", "name": "Mo Town"}
    # what Group HQ's connector reads (partner_info): every contract 3 key sits on the partner record, money never does
    assert not {k for k in found["partner"] if re.search(r"pence|fee_|commission|amount", k)}


# ---- (b) the ?ref= cookie and leads ------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/valuation", "/serviced-accommodation", "/partners-programme", "/commercial",
                                  "/areas/coventry-ref", "/areas/coventry-ref/lettings", "/areas/coventry-ref/commercial",
                                  "/no-such-page"])
def test_ref_on_any_public_page_sets_a_90_day_cookie(client, rita, cal, path):
    code = _code(rita)
    r = _visitor().get(path, params={"ref": code.lower()})
    assert r.status_code in (200, 404)
    cookie = _set_cookie(r)
    assert f"{referrals.REF_COOKIE}={code}" in cookie and "Max-Age=7776000" in cookie
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower() and "Path=/" in cookie


def test_the_cookies_are_secure_when_the_site_is(client, rita, monkeypatch):
    monkeypatch.setattr(config, "SECURE_COOKIES", True)
    for path, params, name in (("/", {"ref": _code(rita)}, referrals.REF_COOKIE),
                               ("/partners-programme", {"seller": SELLER}, referrals.SELLER_COOKIE)):
        cookie = next(c for c in _visitor().get(path, params=params).headers.get_list("set-cookie") if c.startswith(f"{name}="))
        assert "Secure" in cookie and "HttpOnly" in cookie, cookie


def test_unknown_or_paused_codes_set_nothing_and_a_newer_link_wins(client, rita, cal, paula):
    v = _visitor()
    assert referrals.REF_COOKIE not in _set_cookie(v.get("/", params={"ref": "NOPE99"}))
    assert referrals.REF_COOKIE not in _set_cookie(v.get("/", params={"ref": _code(paula)}))
    assert referrals.REF_COOKIE not in _set_cookie(v.get("/", params={"ref": "<script>"}))
    v.get("/", params={"ref": _code(rita)})
    v.get("/valuation", params={"ref": "NOPE99"})                      # a dead link never overwrites a live one
    assert v.cookies.get(referrals.REF_COOKIE) == _code(rita)
    v.get("/valuation", params={"ref": _code(cal)})
    assert v.cookies.get(referrals.REF_COOKIE) == _code(cal)
    # admin and portal pages never set it
    assert referrals.REF_COOKIE not in _set_cookie(_visitor().get("/admin/login", params={"ref": _code(rita)}))


@pytest.mark.parametrize("fields,kind", [
    (RENT_REVIEW, "rent_review"),
    ({"_subject": "Short-let appraisal request", "name": "Sid Shortlet", "phone": "07700 900322", "postcode": "CV1 3BB"}, "sa_appraisal"),
    ({"kind": "commercial_appraisal", "name": "Cora Commercial", "phone": "07700 900323", "postcode": "CV1 4CC",
      "property_use": "retail", "lease_status": "vacant"}, "commercial_appraisal"),
    ({"_subject": "Local Partner application", "name": "Pat Applicant", "phone": "07700 900324", "email": "pat.app@example.test",
      "area": "Leicester", "why": "I know it"}, "partner_application"),
    ({"_subject": "General enquiry", "name": "Gail General", "phone": "07700 900325", "postcode": "CV2 2DD"}, "contact"),
])
def test_every_lead_form_records_the_referring_partner(client, rita, cal, fields, kind):
    v = _visitor()
    v.get("/areas/nottingham-ref", params={"ref": _code(rita)})
    lead = _post_lead(v, **fields)
    assert lead.kind == kind and lead.referrer_partner_id == rita.partner_id and lead.referrer_via == "link"
    if kind in ("rent_review", "sa_appraisal", "contact"):
        assert lead.partner_id == cal.partner_id             # served by the area's partner, credited to the referrer
    # without the cookie nobody is credited
    assert _post_lead(_visitor(), **fields).referrer_partner_id is None


def test_a_partner_paused_after_the_click_is_not_credited(client, rita, paula):
    v = _visitor()
    with session() as db:
        db.get(Partner, paula.partner_id).status = "active"
        db.commit()
    v.get("/", params={"ref": _code(paula)})
    assert v.cookies.get(referrals.REF_COOKIE) == _code(paula)
    with session() as db:
        db.get(Partner, paula.partner_id).status = "paused"
        db.commit()
    assert _post_lead(v, **RENT_REVIEW).referrer_partner_id is None


# ---- (b) ?seller= on the partner-programme application -----------------------------------------
def test_seller_code_is_kept_on_a_partner_application_and_told_to_head_office(client, admin_client, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(mail, "configured", lambda: True)
    monkeypatch.setattr(mail, "send", lambda subject, body, to: sent.append((subject, body, to)) or True)
    v = _visitor()
    r = v.get("/partners-programme", params={"seller": SELLER})
    cookie = _set_cookie(r)
    assert f"{referrals.SELLER_COOKIE}={SELLER}" in cookie and "Max-Age=7776000" in cookie and "HttpOnly" in cookie
    lead = _post_lead(v, _subject="Local Partner application", name="Sally Seller-Referred", phone="07700 900330",
                      email="sally.app@example.test", area="Derby", why="Lived there for years")
    assert lead.kind == "partner_application" and lead.seller_code == SELLER
    assert any(f"Group HQ seller code: {SELLER}" in body for _, body, _ in sent)
    # only the application keeps it
    assert _post_lead(v, **RENT_REVIEW).seller_code == ""
    page = admin_client.get(f"/admin/leads/{lead.id}").text
    assert SELLER in page and "Group HQ seller" in page
    rows = _csv(admin_client.get("/admin/leads/export.csv", params={"q": "Sally Seller-Referred"}).text)
    assert rows and rows[0]["seller_code"] == SELLER
    # only the partner-programme page remembers it, the page Group HQ's tracked link opens
    for path in ("/", "/valuation", "/areas/coventry-ref"):
        assert referrals.SELLER_COOKIE not in _set_cookie(_visitor().get(path, params={"seller": SELLER})), path
    # anything that is not a seller code's shape is ignored
    w = _visitor()
    assert referrals.SELLER_COOKIE not in _set_cookie(w.get("/partners-programme", params={"seller": "bad code!"}))
    assert referrals.SELLER_COOKIE not in _set_cookie(w.get("/partners-programme", params={"seller": "X" * 25}))


# ---- (b) instructions --------------------------------------------------------------------------
def test_instruction_from_pp_credits_an_active_partner_named_as_introducer(client, admin_client, sender, rita, cal, paula):
    r = sender.post("/api/partner/properties", json=_property("pp:ref-1", "Rita.Referrer@Example.test"))
    assert r.status_code == 201, r.text
    for ref, email in (("pp:ref-2", "paula.paused@example.test"), ("pp:ref-3", "someone.else@example.test"), ("pp:ref-4", "")):
        assert sender.post("/api/partner/properties", json=_property(ref, email)).status_code == 201
    with session() as db:
        by_ref = {i.external_ref: i for i in db.query(Instruction).filter(Instruction.external_ref.like("pp:ref-%")).all()}
        assert by_ref["pp:ref-1"].referrer_partner_id == rita.partner_id and by_ref["pp:ref-1"].referrer_via == "introducer"
        assert by_ref["pp:ref-1"].partner_id == cal.partner_id
        assert all(by_ref[k].referrer_partner_id is None for k in ("pp:ref-2", "pp:ref-3", "pp:ref-4"))
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{by_ref['pp:ref-1'].id}").one()
        assert f"referred by partner {rita.partner_id}" in entry.detail
    # the sender's read-back carries nothing about the referral
    assert not [k for k in r.json()["instruction"] if "referr" in k] and str(rita.partner_id) not in json.dumps(r.json()["instruction"]["partner_first"])


def test_an_unverified_introducer_email_is_not_credited_but_shown_to_head_office(client, admin_client, sender, rita, monkeypatch):
    # Property Predator says email_verified false for a password-only account, and another sender may say nothing:
    # neither credits the partner whose email it is
    for ref, verified in (("pp:unverified-1", False), ("pp:unverified-2", None)):
        assert sender.post("/api/partner/properties", json=_property(ref, "rita.referrer@example.test", verified=verified)).status_code == 201
    with session() as db:
        rows = {i.external_ref: i for i in db.query(Instruction).filter(Instruction.external_ref.like("pp:unverified-%")).all()}
        assert all(i.referrer_partner_id is None and i.referrer_via == "" for i in rows.values())
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{rows['pp:unverified-1'].id}").one()
        assert f"introducer email is partner {rita.partner_id}'s but the sender did not verify it" in entry.detail
        first = rows["pp:unverified-1"]
    # head office sees the match on the instruction page and in the Referrer column, to credit it by hand
    detail = admin_client.get(f"/admin/instructions/{first.id}").text
    assert "The introducer email is" in detail and "Rita Referrer" in detail and "was not credited" in detail
    listing = admin_client.get("/admin/instructions", params={"q": first.ref}).text
    assert "Rita Referrer? (not credited)" in listing
    # a client whose sign-in proves the email (Key Properties) is believed without the flag, but not against it
    monkeypatch.setattr(partner_api, "EMAIL_PROVING_CLIENTS", {SENDER_KP})
    kp = _api(_make_client(SENDER_KP, "properties:write"), SENDER_KP)
    for ref, verified, credited in (("kp:proved-1", None, rita.partner_id), ("kp:proved-2", False, None)):
        body = dict(_property(ref, "rita.referrer@example.test", verified=verified), origin_system="kp")
        assert kp.post("/api/partner/properties", json=body).status_code == 201
        with session() as db:
            assert db.query(Instruction).filter(Instruction.external_ref == ref).one().referrer_partner_id == credited, ref


def test_head_office_references_are_closed_to_every_sender(client, admin_client, sender, rita):
    v = _visitor()
    v.get("/", params={"ref": _code(rita)})
    lead = _post_lead(v, **dict(RENT_REVIEW, name="Nina Namespace"))
    inst_id = _instruct(admin_client, lead)
    # a sender holding properties:write can neither read head office's instruction ...
    for ref in (f"lead:{lead.id}", f"LEAD:{lead.id}", f" lead:{lead.id}"):
        r = sender.get(f"/api/partner/properties/{ref}")
        assert r.status_code == 200 and r.json() == {"ok": True, "found": False, "instruction": None}, ref
    # ... nor take a lead's reference before head office makes its instruction
    later = _post_lead(v, **dict(RENT_REVIEW, name="Lewis Later"))
    for ref in (f"lead:{later.id}", f"Lead:{later.id}"):
        r = sender.post("/api/partner/properties", json=dict(_property(ref, ""), origin_system="web"))
        assert r.status_code == 422 and r.json()["reason"] == "invalid_request" and r.json()["field"] == "external_ref", r.text
    with session() as db:
        assert db.query(Instruction).filter(func.lower(Instruction.external_ref) == f"lead:{later.id}").count() == 0
    later_id = _instruct(admin_client, later)
    with session() as db:
        made = db.get(Instruction, later_id)
        assert (made.external_ref, made.origin_system, made.landlord_name) == (f"lead:{later.id}", "web", "Lewis Later")
        assert db.get(Instruction, inst_id).payload["from_lead"] == lead.id


def test_an_instruction_made_from_a_referred_lead_keeps_the_referrer(client, admin_client, rita, cal):
    v = _visitor()
    v.get("/", params={"ref": _code(rita)})
    lead = _post_lead(v, **dict(RENT_REVIEW, name="Ivy Instructs", email="ivy.instructs@example.test"))
    page = admin_client.get(f"/admin/leads/{lead.id}").text
    assert "Create instruction" in page and "Rita Referrer" in page
    r = admin_client.post(f"/admin/leads/{lead.id}/instruction")
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/instructions/"), r.text
    inst_id = int(r.headers["location"].split("/")[3].split("?")[0])
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert (inst.external_ref, inst.origin_system, inst.status) == (f"lead:{lead.id}", "web", "new")
        assert (inst.referrer_partner_id, inst.referrer_via) == (rita.partner_id, "lead")
        assert (inst.partner_id, inst.area_id, inst.service_line) == (cal.partner_id, cal.area_id, "residential")
        assert (inst.postcode, inst.outward, inst.bedrooms, inst.landlord_name) == ("CV1 2AB", "CV1", 3, "Ivy Instructs")
        assert inst.consent_text_version == "hnh-web-enquiry" and inst.ref == Instruction.make_ref(inst.id)
        assert db.query(AuditLog).filter(AuditLog.action == "instruction.from_lead", AuditLog.target == f"instruction:{inst_id}").count() == 1
    # pressing it again opens the same instruction; the lead page links to it
    r = admin_client.post(f"/admin/leads/{lead.id}/instruction")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/instructions/{inst_id}")
    with session() as db:
        assert db.query(Instruction).filter(Instruction.external_ref == f"lead:{lead.id}").count() == 1
    page = admin_client.get(f"/admin/leads/{lead.id}").text
    assert f"/admin/instructions/{inst_id}" in page and "Create instruction" not in page
    detail = admin_client.get(f"/admin/instructions/{inst_id}").text
    assert f"/admin/leads/{lead.id}" in detail and "Rita Referrer" in detail
    # a short-let enquiry becomes a short-let instruction; a property alert cannot become one
    sa = _post_lead(v, _subject="Short-let appraisal request", name="Sam Shortlet", phone="07700 900340", postcode="NG1 5AA")
    r = admin_client.post(f"/admin/leads/{sa.id}/instruction")
    with session() as db:
        made = db.query(Instruction).filter(Instruction.external_ref == f"lead:{sa.id}").one()
        assert made.service_line == "sa" and made.partner_id == rita.partner_id and made.referrer_partner_id == rita.partner_id
    alert = _post_lead(_visitor(), _subject="Property alert registration", name="Al Alert", email="al.alert@example.test",
                       postcode="CV1 1AA")
    r = admin_client.post(f"/admin/leads/{alert.id}/instruction")
    assert r.status_code == 303 and "Only a landlord" in _flash(r.headers["location"])["err"]
    nowhere = _post_lead(_visitor(), _subject="General enquiry", name="No Postcode", phone="07700 900341")
    r = admin_client.post(f"/admin/leads/{nowhere.id}/instruction")
    assert "no UK postcode" in _flash(r.headers["location"])["err"]


def test_a_second_create_instruction_racing_the_first_opens_the_existing_one(client, admin_client, rita, monkeypatch):
    lead = _post_lead(_visitor(), **dict(RENT_REVIEW, name="Dora Double"))
    original = referrals.credit
    raced: list[int] = []

    def credit_after_a_rival(record, partner, via):
        # Between this press's check for an existing instruction and its insert, another press commits one.
        if isinstance(record, Instruction) and not raced:
            with session() as db:
                rival = Instruction(external_ref=f"lead:{lead.id}", origin_system="web", landlord_name="Dora Double",
                                    consent_text_version="hnh-web-enquiry", address="", postcode="CV1 2AB", outward="CV1",
                                    status="new", status_history=[], payload={"from_lead": lead.id})
                db.add(rival)
                db.flush()
                rival.ref = Instruction.make_ref(rival.id)
                db.commit()
                raced.append(rival.id)
        return original(record, partner, via)

    monkeypatch.setattr(referrals, "credit", credit_after_a_rival)
    r = admin_client.post(f"/admin/leads/{lead.id}/instruction")
    assert raced and r.status_code == 303, r.text
    assert r.headers["location"].startswith(f"/admin/instructions/{raced[0]}")
    assert "already instruction" in _flash(r.headers["location"])["ok"]
    with session() as db:
        assert db.query(Instruction).filter(Instruction.external_ref == f"lead:{lead.id}").count() == 1


# ---- (d) head office: referrer column, filter, by-hand select and the export -------------------
def test_admin_lists_filter_and_show_the_referrer(client, admin_client, sender, rita, cal):
    v = _visitor()
    v.get("/", params={"ref": _code(rita)})
    referred = _post_lead(v, **dict(RENT_REVIEW, name="Fiona Filter"))
    plain = _post_lead(_visitor(), **dict(RENT_REVIEW, name="Fred Unfiltered"))
    _send(sender, "pp:ref-list", "rita.referrer@example.test")
    page = admin_client.get("/admin/leads", params={"referrer": str(rita.partner_id)}).text
    assert "<th>Referrer</th>" in page and "Fiona Filter" in page and "Fred Unfiltered" not in page
    page = admin_client.get("/admin/leads", params={"referrer": "none", "q": "Fred Unfiltered"}).text
    assert "Fred Unfiltered" in page
    assert "Fiona Filter" in admin_client.get("/admin/leads", params={"referrer": "any", "q": "Fiona"}).text
    assert "Fiona Filter" not in admin_client.get("/admin/leads", params={"referrer": "none", "q": "Fiona"}).text
    rows = _csv(admin_client.get("/admin/leads/export.csv", params={"referrer": str(rita.partner_id)}).text)
    assert rows and {r["referrer"] for r in rows} == {"Rita Referrer"} and {r["referrer_code"] for r in rows} == {_code(rita)}
    assert any(r["name"] == "Fiona Filter" and r["referred_via"] == "link" for r in rows)
    page = admin_client.get("/admin/instructions", params={"referrer": str(rita.partner_id)}).text
    assert "<th>Referrer</th>" in page and "8 Far Gosford Street" in page
    assert "8 Far Gosford Street" not in admin_client.get("/admin/instructions", params={"referrer": "none", "q": "pp:ref-list"}).text
    assert plain.referrer_partner_id is None and referred.referrer_partner_id == rita.partner_id


def test_head_office_sets_and_clears_the_referrer_by_hand(client, admin_client, sender, rita, cal):
    lead = _post_lead(_visitor(), **dict(RENT_REVIEW, name="Wanda WhatsApp"))
    base = {"status": "new", "notes": "", "area_id": str(cal.area_id)}
    r = admin_client.post(f"/admin/leads/{lead.id}", data=dict(base, referrer_id=str(rita.partner_id)))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        saved = db.get(Lead, lead.id)
        assert (saved.referrer_partner_id, saved.referrer_via) == (rita.partner_id, "head office")
        entry = db.query(AuditLog).filter(AuditLog.target == f"lead:{lead.id}", AuditLog.action == "lead.update").one()
        assert f"referrer to Rita Referrer ({_code(rita)})" in entry.detail
    # a page without the field leaves it alone; an unknown partner is refused; Nobody ("none") clears it
    admin_client.post(f"/admin/leads/{lead.id}", data=dict(base, notes="called back"))
    r = admin_client.post(f"/admin/leads/{lead.id}", data=dict(base, referrer_id="999999"))
    assert "no longer exists" in _flash(r.headers["location"])["err"]
    with session() as db:
        assert db.get(Lead, lead.id).referrer_partner_id == rita.partner_id
    admin_client.post(f"/admin/leads/{lead.id}", data=dict(base, notes="called back", referrer_id=""))
    with session() as db:
        assert db.get(Lead, lead.id).referrer_partner_id == rita.partner_id
    admin_client.post(f"/admin/leads/{lead.id}", data=dict(base, notes="called back", referrer_id="none"))
    with session() as db:
        assert db.get(Lead, lead.id).referrer_partner_id is None and db.get(Lead, lead.id).referrer_via == ""
    # the same select on an instruction nobody was credited with
    inst_id = _send(sender, "pp:ref-hand", "someone.else@example.test")
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.referrer_partner_id is None
    r = admin_client.post(f"/admin/instructions/{inst.id}", data={"status": inst.status, "notes": inst.notes,
                                                                   "area_id": str(inst.area_id), "referrer_id": str(cal.partner_id)})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        assert (db.get(Instruction, inst.id).referrer_partner_id, db.get(Instruction, inst.id).referrer_via) == (cal.partner_id, "head office")
    page = admin_client.get(f"/admin/instructions/{inst.id}").text
    assert 'name="referrer_id"' in page and "Cal Coventry" in page


def test_referrals_page_and_csv_by_partner_and_month(client, admin_client, sender, rita, cal):
    # one of each way a referral is recorded: the link, the introducer email, and an instruction made from a lead
    v = _visitor()
    v.get("/", params={"ref": _code(rita)})
    _post_lead(v, **RENT_REVIEW)
    _send(sender, "pp:ref-csv", "rita.referrer@example.test")
    _instruct(admin_client, _post_lead(v, **dict(RENT_REVIEW, name="Ivy Instructs", email="ivy.instructs@example.test")))
    page = admin_client.get("/admin/referrals").text
    assert "Rita Referrer" in page and _code(rita) in page and 'href="/admin/referrals"' in page
    r = admin_client.get("/admin/referrals/export.csv", params={"partner": str(rita.partner_id)})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.headers["cache-control"] == "no-store"
    rows = _csv(r.text)
    assert list(rows[0]) == ["month", "referrer_id", "referrer", "referral_code", "record", "reference", "created", "type",
                             "area", "postcode", "status", "routed_to", "referred_via", "seller_code"]
    assert {row["referrer"] for row in rows} == {"Rita Referrer"} and {row["record"] for row in rows} == {"lead", "instruction"}
    assert all(re.fullmatch(r"\d{4}-\d{2}", row["month"]) for row in rows)
    inst_rows = [row for row in rows if row["record"] == "instruction"]
    assert any(row["reference"].startswith("HNH-") and row["routed_to"] == "Cal Coventry" for row in inst_rows)
    assert {row["referred_via"] for row in rows} <= {"link", "introducer", "lead", "head office"}
    assert {"link", "introducer", "lead"} <= {row["referred_via"] for row in rows}
    # nothing in it is a person's name or contact detail, a full address or a figure about money
    flat = r.text.lower()
    for hidden in ("lara landlord", "ivy instructs", "peter property", "lara.landlord@example.test", "ivy.instructs@example.test",
                   "peter.property@example.test", "07700 900321", "07700 900654", "spon street", "far gosford", "cv1 2ab", "£"):
        assert hidden not in flat, hidden
    month = rows[0]["month"]
    assert len(_csv(admin_client.get("/admin/referrals/export.csv", params={"from": month, "to": month}).text)) >= len(rows)
    assert _csv(admin_client.get("/admin/referrals/export.csv", params={"from": "2001-01", "to": "2001-12"}).text) == []
    with session() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "referrals.export").count() >= 3
    assert admin_client.get("/admin/referrals", params={"partner": str(cal.partner_id)}).status_code == 200
    anon = _visitor().get("/admin/referrals/export.csv")
    assert anon.status_code == 303 and anon.headers["location"].startswith("/admin/login")


# ---- (e) the partner portal ---------------------------------------------------------------------
def test_portal_shows_the_referral_link_and_status_only_introductions(client, sender, rita, cal):
    v = _visitor()
    v.get("/", params={"ref": _code(rita)})
    _post_lead(v, **RENT_REVIEW)
    _send(sender, "pp:ref-portal", "rita.referrer@example.test")
    rita_portal, cal_portal = partner_client(rita), partner_client(cal)
    code = _code(rita)
    assert 'href="/partners/introductions"' in rita_portal.get("/partners").text
    page = rita_portal.get("/partners/introductions")
    assert page.status_code == 200
    html = page.text
    assert f'value="{config.BASE_URL}/?ref={code}"' in html and 'data-copy-target="#ref-link-url"' in html
    assert f"{config.BASE_URL}/areas/nottingham-ref?ref={code}" in html and f"&amp;ref={code}" in html
    assert "the later link is the one that counts" in html and "recorded by head office" in html
    assert "Rent review" in html and "Coventry" in html and "CV1" in html and "HNH-" in html
    for hidden in ("Lara Landlord", "lara.landlord@example.test", "07700 900321", "Spon Street", "CV1 2AB", "Peter Property",
                   "peter.property@example.test", "Far Gosford", "£875", "acct_ref"):
        assert hidden not in html, hidden
    # Cal serves those leads but did not introduce them: his introductions page does not list them
    other = cal_portal.get("/partners/introductions").text
    assert f"?ref={_code(cal)}" in other and "Rent review" not in other


# ---- revision 0006 ---------------------------------------------------------------------------------
# Instructions on record before the revision: (introducer email, origin, introducer.email_verified as sent). Only
# mig-0 (Property Predator vouched for an active partner's email) and mig-6 (Key Properties, saying nothing) are credited.
MIGRATION_INSTRUCTIONS = (("Early@Example.test", "pp", True), ("paused@example.test", "pp", True),
                          ("late@example.test", "pp", True), ("stranger@example.test", "pp", True), ("", "pp", None),
                          ("early@example.test", "pp", False), ("early@example.test", "kp", None),
                          ("early@example.test", "pp", None))


def _seed_0005(eng) -> None:
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "0005_partner_services")
    with eng.begin() as conn:
        for name, email, status, created in (("Active Early", "early@example.test", "active", "2026-01-01 00:00:00"),
                                             ("Paused One", "paused@example.test", "paused", "2026-01-01 00:00:00"),
                                             ("Active Late", "late@example.test", "active", "2026-12-01 00:00:00")):
            conn.execute(text("INSERT INTO partners (name, first_name, email, phone, whatsapp, whatsapp_display, bio, photo_path, "
                              "tier, status, units_managed, notes, created_at) VALUES (:n, '', :e, '', '', '', '', '', 'area', "
                              ":s, 0, '', :c)"), {"n": name, "e": email, "s": status, "c": created})
        conn.execute(text("INSERT INTO leads (created_at, kind, name, phone, email, postcode, address, region, status, payload, "
                          "notes, source_page, updated_at) VALUES ('2026-10-01 09:00:00', 'rent_review', 'Old Lead', '', '', "
                          "'TA1', '', 'england', 'new', '{}', '', '', '2026-10-01 09:00:00')"))
        # (introducer email, origin, introducer.email_verified in the body the sender posted; None leaves it out)
        payload_sql = "CAST(:p AS json)" if eng.dialect.name == "postgresql" else ":p"
        for i, (key, origin, verified) in enumerate(MIGRATION_INSTRUCTIONS):
            body = {"introducer": {"person_key": key, **({} if verified is None else {"email_verified": verified})}}
            conn.execute(text(
                "INSERT INTO instructions (ref, external_ref, origin_system, origin_url, introducer_person_key, "
                "introducer_pp_account_id, introducer_kp_user_id, landlord_name, landlord_email, landlord_phone, consent_at, "
                "consent_text_version, address, postcode, outward, property_type, furnished, service_line, status, "
                "status_history, ordris_landlord_client_id, ordris_property_id, ordris_tenancy_id, payload, notes, created_at, "
                "updated_at) VALUES (:r, :x, :o, '', :k, '', '', 'L', '', '', '2026-10-20 00:00:00', 'v1', '', 'TA1 1AA', "
                f"'TA1', '', '', 'residential', 'new', '[]', '', '', '', {payload_sql}, '', '2026-10-20 00:00:00', "
                "'2026-10-20 00:00:00')"),
                {"r": f"HNH-90000{i}", "x": f"pp:mig-{i}", "k": key, "o": origin, "p": json.dumps(body)})


def _check_0006(eng) -> None:
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "0006_partner_referrals")
    assert migrations.current_revision(eng) == "0006_partner_referrals"
    with eng.connect() as conn:
        codes = dict(conn.execute(text("SELECT email, referral_code FROM partners")).all())
        credited = dict(conn.execute(text("SELECT external_ref, referrer_partner_id FROM instructions")).all())
        via = dict(conn.execute(text("SELECT external_ref, referrer_via FROM instructions")).all())
        early = conn.execute(text("SELECT id FROM partners WHERE email = 'early@example.test'")).scalar()
        lead = conn.execute(text("SELECT referrer_partner_id, referrer_via, seller_code FROM leads")).one()
    assert len(codes) == 3 and len(set(codes.values())) == 3 and all(CODE_SHAPE.match(c) for c in codes.values())
    assert credited == {"pp:mig-0": early, "pp:mig-1": None, "pp:mig-2": None, "pp:mig-3": None, "pp:mig-4": None,
                        "pp:mig-5": None, "pp:mig-6": early, "pp:mig-7": None}
    assert via["pp:mig-0"] == via["pp:mig-6"] == "introducer" and via["pp:mig-1"] == via["pp:mig-5"] == ""
    assert tuple(lead) == (None, "", "")
    insp = inspect(eng)
    assert {"ix_partners_referral_code"} <= {i["name"] for i in insp.get_indexes("partners") if i["unique"]}
    assert {"ix_retired_referral_codes_code"} <= {i["name"] for i in insp.get_indexes("retired_referral_codes") if i["unique"]}
    assert any(fk["constrained_columns"] == ["partner_id"] and fk["referred_table"] == "partners"
               for fk in insp.get_foreign_keys("retired_referral_codes"))
    for table in ("leads", "instructions"):
        assert any(fk["constrained_columns"] == ["referrer_partner_id"] and fk["referred_table"] == "partners"
                   for fk in insp.get_foreign_keys(table)), table
        assert f"ix_{table}_referrer_partner_id" in {i["name"] for i in insp.get_indexes(table)}
    # the unique index holds; a raw insert that names no code is allowed (the app always gives one)
    with pytest.raises(Exception):
        with eng.begin() as conn:
            conn.execute(text("UPDATE partners SET referral_code = :c WHERE email = 'late@example.test'"),
                         {"c": codes["early@example.test"]})
    with eng.begin() as conn:
        command.downgrade(migrations.alembic_config(conn), "-1")
    assert migrations.current_revision(eng) == "0005_partner_services"
    insp = inspect(eng)
    assert "retired_referral_codes" not in insp.get_table_names()
    assert "referral_code" not in {c["name"] for c in insp.get_columns("partners")}
    assert not {"referrer_partner_id", "referrer_via", "seller_code"} & {c["name"] for c in insp.get_columns("leads")}
    assert not {"referrer_partner_id", "referrer_via"} & {c["name"] for c in insp.get_columns("instructions")}
    with eng.connect() as conn:
        assert [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("partners", "leads", "instructions")] == [3, 1, len(MIGRATION_INSTRUCTIONS)]
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "head")
    assert migrations.current_revision(eng) == migrations.head_revision()


def test_migration_0006_backfills_codes_and_introducers_and_comes_back_out(tmp_path: Path):
    eng = make_engine(f"sqlite:///{tmp_path / 'referrals.db'}")
    _seed_0005(eng)
    _check_0006(eng)
    eng.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="HNH_TEST_DATABASE_URL names no throwaway Postgres database")
def test_migration_0006_on_postgres():
    """The same round trip on Postgres, in a schema of its own inside the throwaway test database."""
    schema = "hnh_mig_0006"
    admin = make_engine(TEST_DATABASE_URL)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    eng = make_engine(TEST_DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        assert eng.dialect.name == "postgresql"
        _seed_0005(eng)
        _check_0006(eng)
    finally:
        eng.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()
