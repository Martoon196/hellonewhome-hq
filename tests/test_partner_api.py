"""The Partner API (/api/partner): client secrets, the three routes and the bootstrap from the environment.

The payloads are the ones Group HQ's connector sends (group-hq/app/services/connectors/hnh.py), so a
change on either side shows up here."""
from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
from fastapi.testclient import TestClient

from app import auth, config, seed as seed_module
from app.main import app
from app.models import ApiClient, Area, AuditLog, Partner, User
from scripts import api_client as cli
from tests.conftest import login, session

CLIENT = "grouphq"


def _make_client(name: str, scopes: str = "partners:write", *, active: bool = True) -> str:
    with session() as db:
        existing = db.query(ApiClient).filter(ApiClient.name == name).first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        client, token = auth.create_api_client(db, name, scopes)
        client.active = active
        db.commit()
    return token


@pytest.fixture(scope="module")
def token(client):
    return _make_client(CLIENT)


def _api(token: str | None, name: str | None = CLIENT, **extra_headers) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    if token is not None:
        c.headers["X-Service-Token"] = token
    if name is not None:
        c.headers["X-Partner-Client"] = name
    c.headers.update(extra_headers)
    return c


def _payload(**overrides) -> dict:
    body = {"email": "mia.mentee@example.test", "name": "Mia Mentee", "tier": "area", "status": "active",
            "create_login": True, "franchise": {"fee_waived": True, "reason": "Group HQ: Mentorship"},
            "new_area": {"slug": "hereford", "name": "Hereford", "region": "england", "postcodes": ["HR1", "HR2", "HR4"],
                         "covers": "Hereford and the villages"}}
    body.update(overrides)
    return body


# ---- authentication ------------------------------------------------------------
def test_no_token_and_unknown_token_are_401(token):
    r = _api(None).post("/api/partner/partners", json=_payload())
    assert r.status_code == 401 and r.json() == {"ok": False, "reason": "missing_token",
                                                 "message": "Send the client secret in the X-Service-Token header or as a bearer token."}
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = _api("not-the-secret-at-all").post("/api/partner/partners", json=_payload())
    assert r.status_code == 401 and r.json()["reason"] == "invalid_client"
    assert "not-the-secret" not in r.text
    # a valid secret under the wrong client name is as unknown as a wrong secret
    r = _api(token, "someone-else").get("/api/partner/partners", params={"email": "x@example.test"})
    assert r.status_code == 401 and r.json()["reason"] == "invalid_client"
    r = _api(token, None).get("/api/partner/partners", params={"email": "x@example.test"})
    assert r.status_code == 401 and r.json()["reason"] == "missing_client"
    with session() as db:
        assert db.query(Partner).filter(Partner.email == "mia.mentee@example.test").count() == 0


def test_bearer_token_is_accepted_as_the_connector_sends_it(token):
    r = _api(None, Authorization=f"Bearer {token}").get("/api/partner/partners", params={"email": "nobody@example.test"})
    assert r.status_code == 200 and r.json() == {"ok": True, "found": False, "partner": None}
    assert r.headers["cache-control"] == "no-store"


def test_inactive_client_is_401(client):
    dead = _make_client("retired-client", active=False)
    r = _api(dead, "retired-client").get("/api/partner/partners", params={"email": "nobody@example.test"})
    assert r.status_code == 401 and r.json()["reason"] == "client_inactive"


def test_wrong_scope_is_403(client):
    reader = _make_client("reader-client", "partners:read")
    r = _api(reader, "reader-client").post("/api/partner/partners", json=_payload())
    assert r.status_code == 403 and r.json() == {"ok": False, "reason": "scope_missing",
                                                 "message": "This client does not have the partners:write scope."}
    # the read scope still serves the lookup, and a write scope covers reads
    assert _api(reader, "reader-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200


def test_any_origin_header_is_403(token):
    for origin in ("https://hellonewhome.co.uk", "null", "https://evil.example"):
        r = _api(token, Origin=origin).post("/api/partner/partners", json=_payload())
        assert r.status_code == 403 and r.json()["reason"] == "origin_not_allowed", origin
    with session() as db:
        assert db.query(Partner).filter(Partner.email == "mia.mentee@example.test").count() == 0


def test_last_used_is_recorded_and_scopes_are_validated(token):
    _api(token).get("/api/partner/partners", params={"email": "nobody@example.test"})
    with session() as db:
        row = db.query(ApiClient).filter(ApiClient.name == CLIENT).one()
        assert row.last_used_at is not None and row.token_hash != token and len(row.token_hash) == 64
    with pytest.raises(ValueError, match="unknown scope"):
        auth.normalise_scopes("partners:write,leads:admin")
    assert auth.scope_allowed(["partners:write"], "partners:read")
    assert not auth.scope_allowed(["partners:read"], "partners:write")
    assert len(auth.generate_api_token()) == 48


# ---- POST /api/partner/partners ------------------------------------------------
def test_create_partner_with_a_new_area_and_login_then_update_idempotently(token):
    api = _api(token)
    r = api.post("/api/partner/partners", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["area_slug"] == "hereford" and body["area_created"]
    assert body["partner_id"] and body["user_id"] and len(body["one_time_password"]) == 12
    record = body["partner"]
    assert record["id"] == body["partner_id"] and record["user_id"] == body["user_id"]
    assert record["franchise"] == {"fee_waived": True, "reason": "Group HQ: Mentorship"}
    assert [a["slug"] for a in record["areas"]] == ["hereford"] and record["login"]["must_change_password"]
    assert r.headers["cache-control"] == "no-store"
    with session() as db:
        partner = db.get(Partner, body["partner_id"])
        assert (partner.name, partner.email, partner.tier, partner.status) == ("Mia Mentee", "mia.mentee@example.test", "area", "active")
        assert partner.franchise_fee_waived is True and partner.franchise_note == "Group HQ: Mentorship"
        area = db.query(Area).filter(Area.slug == "hereford").one()
        assert (area.role, area.status, area.region, area.partner_id) == ("partner", "active", "england", partner.id)
        assert area.postcodes == ["HR1", "HR2", "HR4"] and area.covers == "Hereford and the villages"
        user = db.get(User, body["user_id"])
        assert (user.role, user.partner_id, user.must_change_password, user.is_active) == ("partner", partner.id, True, True)
        entry = db.query(AuditLog).filter(AuditLog.action == "partner_api.create", AuditLog.target == f"partner:{partner.id}").one()
        assert entry.user_id is None and "via api client grouphq" in entry.detail and body["one_time_password"] not in entry.detail
    # the one-time password works once, and the partner must choose their own
    portal = TestClient(app, follow_redirects=False)
    login(portal, "/partners/login", "mia.mentee@example.test", body["one_time_password"])
    r = portal.get("/partners")
    assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1"

    # the same call again (what the outbox resends) updates in place and creates nothing twice
    r = api.post("/api/partner/partners", json=_payload(name="Mia Mentee-Jones", phone="01934 000000"))
    assert r.status_code == 200, r.text
    again = r.json()
    assert not again["created"] and again["partner_id"] == body["partner_id"] and again["user_id"] == body["user_id"]
    assert again["one_time_password"] is None and again["area_slug"] == "hereford" and not again["area_created"]
    with session() as db:
        assert db.query(Partner).filter(Partner.email == "mia.mentee@example.test").count() == 1
        assert db.query(User).filter(User.email == "mia.mentee@example.test").count() == 1
        assert db.query(Area).filter(Area.slug == "hereford").count() == 1
        partner = db.get(Partner, body["partner_id"])
        assert partner.name == "Mia Mentee-Jones" and partner.phone == "01934 000000"
        assert partner.franchise_note == "Group HQ: Mentorship"          # resent unchanged
    # the temporary password from the first call still stands: a resend never resets a login
    assert TestClient(app, follow_redirects=False).post(
        "/partners/login", data={"email": "mia.mentee@example.test", "password": body["one_time_password"]}).status_code == 303


def test_admin_partner_page_shows_the_franchise_fields_read_only(token, admin_client):
    with session() as db:
        partner = db.query(Partner).filter(Partner.email == "mia.mentee@example.test").one()
    page = admin_client.get(f"/admin/partners/{partner.id}")
    assert page.status_code == 200
    assert "Franchise" in page.text and "Waived" in page.text and "Group HQ: Mentorship" in page.text
    assert 'name="franchise' not in page.text                            # shown, never edited here


def test_status_change_pauses_and_reactivates(token):
    api = _api(token)
    with session() as db:
        partner_id = db.query(Partner).filter(Partner.email == "mia.mentee@example.test").one().id
    r = api.post(f"/api/partner/partners/{partner_id}/status", json={"status": "paused", "reason": "lapsed"})
    assert r.status_code == 200 and r.json()["status"] == "paused" and r.json()["previous_status"] == "active"
    assert r.json()["partner"]["id"] == partner_id and r.json()["changed"]
    with session() as db:
        assert db.get(Partner, partner_id).status == "paused"
        assert db.query(Area).filter(Area.slug == "hereford").one().partner_id == partner_id    # the area stays
        entry = db.query(AuditLog).filter(AuditLog.action == "partner_api.status").order_by(AuditLog.id.desc()).first()
        assert "active to paused; lapsed" in entry.detail
    # a paused partner is locked out of the portal
    r = TestClient(app).post("/partners/login", data={"email": "mia.mentee@example.test", "password": "whatever"})
    assert r.status_code in (401, 403)
    r = api.post(f"/api/partner/partners/{partner_id}/status", json={"status": "active", "reason": "back"})
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["changed"]
    r = api.post(f"/api/partner/partners/{partner_id}/status", json={"status": "active"})
    assert r.status_code == 200 and not r.json()["changed"]
    r = api.post(f"/api/partner/partners/{partner_id}/status", json={"status": "left"})
    assert r.status_code == 422 and r.json()["reason"] == "invalid_request" and "pending, active, paused" in r.json()["message"]
    r = api.post("/api/partner/partners/999999/status", json={"status": "paused"})
    assert r.status_code == 404 and r.json()["reason"] == "no_partner"
    r = api.post("/api/partner/partners/not-a-number/status", json={"status": "paused"})
    assert r.status_code == 422 and r.json()["reason"] == "invalid_request"


def test_lookup_returns_the_partner_with_areas_and_login_state(token):
    r = _api(token).get("/api/partner/partners", params={"email": "  Mia.Mentee@Example.test "})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["found"]
    record = body["partner"]
    assert record["email"] == "mia.mentee@example.test" and record["status"] == "active"
    assert [a["slug"] for a in record["areas"]] == ["hereford"] and record["areas"][0]["postcodes"] == ["HR1", "HR2", "HR4"]
    assert record["login"]["is_active"] and record["login"]["must_change_password"] and record["user_id"] == record["login"]["user_id"]
    assert "password" not in str(record["login"]).lower().replace("must_change_password", "")
    r = _api(token).get("/api/partner/partners", params={"email": "not-an-email"})
    assert r.status_code == 422 and r.json()["reason"] == "invalid_request"
    r = _api(token).get("/api/partner/partners")
    assert r.status_code == 422


def _placeholder_area(slug: str, name: str, postcodes: list[str]) -> tuple[int, int]:
    """An "opening soon" area with the seed's stand-in partner, as Durham and Andover ship. Built here rather than
    borrowed from the seed, so the seeded areas stay as other tests expect them. Returns (area_id, placeholder_id)."""
    with session() as db:
        placeholder = Partner(name=f"{name} partner (to be appointed)", first_name="", email="", phone="", whatsapp="",
                              whatsapp_display="", bio="", tier="area", status="pending")
        db.add(placeholder)
        db.flush()
        area = Area(slug=slug, name=name, region="england", role="partner", status="coming", partner_id=placeholder.id,
                    covers=f"{name} and around", postcodes=list(postcodes), places=[], sort=700)
        db.add(area)
        db.commit()
        return area.id, placeholder.id


def test_existing_area_is_assigned_and_a_taken_one_is_refused(token, partner_a):
    api = _api(token)
    # an "opening soon" area with a placeholder partner: the placeholder becomes the mentee
    area_id, placeholder_id = _placeholder_area("testshire", "Testshire", ["TQ1", "TQ2"])
    r = api.post("/api/partner/partners", json=_payload(email="dan.testshire@example.test", name="Dan Testshire", new_area=None,
                                                        area_slug="testshire", create_login=False, status="pending"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["partner_id"] == placeholder_id and body["area_slug"] == "testshire" and not body["area_created"]
    assert body["user_id"] is None and body["one_time_password"] is None
    with session() as db:
        partner = db.get(Partner, placeholder_id)
        assert partner.name == "Dan Testshire" and partner.status == "pending" and partner.franchise_fee_waived is True
        area = db.get(Area, area_id)
        assert area.partner_id == placeholder_id and area.status == "coming"    # an existing area's status is head office's
    # an area that belongs to a real partner is not moved
    r = api.post("/api/partner/partners", json=_payload(email="dan.testshire@example.test", name="Dan Testshire", new_area=None,
                                                        area_slug=partner_a.slug))
    assert r.status_code == 409 and r.json()["reason"] == "area_assigned" and r.json()["area_slug"] == partner_a.slug
    r = api.post("/api/partner/partners", json=_payload(email="dan.testshire@example.test", name="Dan Testshire", new_area=None,
                                                        area_slug="no-such-area"))
    assert r.status_code == 422 and "no-such-area" in r.json()["message"]
    r = api.post("/api/partner/partners", json=_payload(email="dan.testshire@example.test", name="Dan Testshire", new_area=None,
                                                        area_slug="swansea"))
    assert r.status_code == 422 and "head office" in r.json()["message"]
    # an existing partner given another placeholder's area: the area moves, and the placeholder that held it is removed
    other_area_id, other_placeholder_id = _placeholder_area("otherford", "Otherford", ["TQ3"])
    r = api.post("/api/partner/partners", json=_payload(email="dan.testshire@example.test", name="Dan Testshire", new_area=None,
                                                        area_slug="otherford"))
    assert r.status_code == 200, r.text
    assert sorted(a["slug"] for a in r.json()["partner"]["areas"]) == ["otherford", "testshire"]
    with session() as db:
        assert db.get(Area, other_area_id).partner_id == placeholder_id
        assert db.get(Partner, other_placeholder_id) is None
        assert db.query(Area).filter(Area.partner_id == placeholder_id).count() == 2
        # the seeded areas were never touched
        for slug in ("durham", "andover"):
            seeded = db.query(Area).filter(Area.slug == slug).one()
            assert seeded.partner is not None and seeded.partner.name.endswith("(to be appointed)")


def test_postcode_clash_and_bare_postcodes(token, partner_a):
    api = _api(token)
    taken = partner_a.postcodes[0]
    r = api.post("/api/partner/partners", json=_payload(email="clash@example.test", name="Cal Clash",
                                                        new_area={"slug": "clash-town", "name": "Clash Town", "region": "england",
                                                                  "postcodes": [taken, "ZZ1"]}))
    assert r.status_code == 409 and r.json()["reason"] == "postcode_clash" and taken in r.json()["message"]
    r = api.post("/api/partner/partners", json=_payload(email="bad@example.test", name="Bad Codes",
                                                        new_area={"name": "Bad", "postcodes": ["not a postcode"]}))
    assert r.status_code == 422 and "Not valid postcode districts" in r.json()["message"]
    with session() as db:
        assert db.query(Partner).filter(Partner.email.in_(["clash@example.test", "bad@example.test"])).count() == 0
    # bare postcodes (the connector's other form): the area is named after its districts and stays "coming"
    r = api.post("/api/partner/partners", json=_payload(email="pat.postcodes@example.test", name="Pat Postcodes", new_area=None,
                                                        postcodes=["ll11", "LL12"]))
    assert r.status_code == 201, r.text
    assert r.json()["area_slug"] == "ll11-ll12" and r.json()["area_created"]
    with session() as db:
        area = db.query(Area).filter(Area.slug == "ll11-ll12").one()
        assert (area.name, area.region, area.status, area.role) == ("LL11 and LL12", "wales", "coming", "partner")
        assert area.postcodes == ["LL11", "LL12"]
    r = api.post("/api/partner/partners", json=_payload(email="scot@example.test", name="Scot Land", new_area=None, postcodes=["EH1"]))
    assert r.status_code == 422 and "England and Wales" in r.json()["message"]
    # TD straddles the border: Berwick-upon-Tweed (TD15) is England, the rest of TD is Scotland
    r = api.post("/api/partner/partners", json=_payload(email="galashiels@example.test", name="Gala Shiels", new_area=None,
                                                        postcodes=["TD1"]))
    assert r.status_code == 422 and "TD1 is in Scotland" in r.json()["message"]
    # every district is checked, not only the first: Berwick's Scottish neighbours (or the bare area) after TD15
    # must not make an England area that covers Duns or Hawick
    for i, codes in enumerate((["TD15", "TD11"], ["TD15", "TD12", "TD14"], ["TD15", "TD"])):
        r = api.post("/api/partner/partners", json=_payload(email=f"border{i}@example.test", name="Border Patch", new_area=None,
                                                            postcodes=codes))
        assert r.status_code == 422 and f"{codes[1]} is in Scotland" in r.json()["message"], (codes, r.text)
    # a region named by the caller does not skip the check; it may still say Wales for a border district
    r = api.post("/api/partner/partners", json=_payload(email="hawick@example.test", name="Hawick Partner",
                                                        new_area={"name": "Hawick", "region": "england", "postcodes": ["TD9"]}))
    assert r.status_code == 422 and "TD9 is in Scotland" in r.json()["message"], r.text
    with session() as db:       # nothing refused was half-made
        assert not [a.slug for a in db.query(Area).all() if any(c.startswith("TD") for c in a.postcodes or [])]
        assert db.query(Partner).filter(Partner.email.in_(["hawick@example.test"] + [f"border{i}@example.test" for i in range(3)])).count() == 0
    r = api.post("/api/partner/partners", json=_payload(email="deeside@example.test", name="Dee Side",
                                                        new_area={"name": "Deeside", "region": "wales", "postcodes": ["CH5"]}))
    assert r.status_code == 201, r.text
    with session() as db:
        assert db.query(Area).filter(Area.slug == "deeside").one().region == "wales"
    r = api.post("/api/partner/partners", json=_payload(email="berwick@example.test", name="Berwick Partner", new_area=None,
                                                        postcodes=["TD15"]))
    assert r.status_code == 201, r.text
    with session() as db:
        area = db.query(Area).filter(Area.slug == r.json()["area_slug"]).one()
        assert (area.region, area.postcodes) == ("england", ["TD15"])
    r = api.post("/api/partner/partners", json=_payload(email="two@example.test", name="Two Ways", postcodes=["BS1"]))
    assert r.status_code == 422 and "not several" in r.json()["message"]


def test_email_in_use_by_another_login_is_409(token, partner_a):
    r = _api(token).post("/api/partner/partners", json=_payload(email=partner_a.email, name="Impostor", new_area=None))
    assert r.status_code == 200 or r.status_code == 201   # the same person: their own partner record, updated
    assert r.json()["partner_id"] == partner_a.partner_id and r.json()["one_time_password"] is None
    with session() as db:
        p = db.get(Partner, partner_a.partner_id)
        p.email = "moved@example.test"                     # head office re-pointed the record; the login keeps the old address
        db.add(p)
        db.commit()
    r = _api(token).post("/api/partner/partners", json=_payload(email=partner_a.email, name="Impostor", new_area=None))
    assert r.status_code == 409 and r.json()["reason"] == "email_in_use"
    with session() as db:
        p = db.get(Partner, partner_a.partner_id)
        p.email, p.name = partner_a.email, partner_a.name
        db.add(p)
        db.commit()
    r = _api(token).post("/api/partner/partners", json=_payload(email=config.ADMIN_EMAIL, name="Admin Grab", new_area=None))
    assert r.status_code == 409 and r.json()["reason"] == "email_in_use"


def test_malformed_bodies_are_refused_in_the_api_shape(token):
    api = _api(token)
    r = api.post("/api/partner/partners", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_json"
    r = api.post("/api/partner/partners", json=["a list"])
    assert r.status_code == 400
    r = api.post("/api/partner/partners", json=_payload(tier="franchisee"))
    assert r.status_code == 422 and r.json() == {"ok": False, "reason": "invalid_request",
                                                 "message": "tier must be one of introducer, area.", "field": "tier"}
    r = api.post("/api/partner/partners", json=_payload(email="nope"))
    assert r.status_code == 422 and r.json()["field"] == "email"
    r = api.post("/api/partner/partners", json=_payload(whatsapp="12345"))
    assert r.status_code == 422 and "WhatsApp" in r.json()["message"]
    r = api.post("/api/partner/partners", json=_payload(franchise={"fee_waived": "yes"}))
    assert r.status_code == 422 and r.json()["field"] == "fee_waived"
    r = api.post("/api/partner/partners", json=_payload(create_login="true"))
    assert r.status_code == 422 and r.json()["field"] == "create_login"
    r = api.post("/api/partner/partners", content=b"{}" * 40_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["reason"] == "request_too_large"
    # every refusal above is a sentence without an exclamation mark
    refusals = [api.post("/api/partner/partners", content=b"not json", headers={"Content-Type": "application/json"}),
                api.post("/api/partner/partners", json=_payload(tier="franchisee")),
                api.post("/api/partner/partners", json=_payload(email="nope")),
                api.post("/api/partner/partners", json=_payload(whatsapp="12345")),
                api.post("/api/partner/partners", json=_payload(create_login="true")),
                _api(None).post("/api/partner/partners", json=_payload()),
                _api("wrong").post("/api/partner/partners", json=_payload()),
                _api(token, Origin="https://evil.example").post("/api/partner/partners", json=_payload())]
    assert [r.status_code for r in refusals] == [400, 422, 422, 422, 422, 401, 401, 403]
    for r in refusals:
        assert "!" not in r.text, r.text


def test_an_existing_slug_with_other_postcodes_is_refused_not_quietly_reassigned(token, partner_a):
    """A resend after Group HQ changed the template's postcodes must not leave the old territory in place
    without a word: 409 slug_taken names what head office holds. The same slug with the same postcodes is
    assigned as before (the idempotent resend), and area_slug always assigns what exists."""
    api = _api(token)
    spec = {"slug": partner_a.slug, "name": "Elsewhere", "region": "england", "postcodes": ["ZZ1", "ZZ2"]}
    r = api.post("/api/partner/partners", json=_payload(email="slug.clash@example.test", name="Sam Slug", new_area=spec))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["reason"] == "slug_taken" and body["area_slug"] == partner_a.slug
    assert body["postcodes"] == partner_a.postcodes and partner_a.postcodes[0] in body["message"]
    with session() as db:
        assert db.query(Partner).filter(Partner.email == "slug.clash@example.test").count() == 0, "nothing was written"
    # bare postcodes derive their slug, so a different list that derives the same slug is refused the same way
    area_id, _placeholder = _placeholder_area("qq1-qq2", "QQ1 and QQ2", ["QQ1", "QQ2", "QQ3"])
    r = api.post("/api/partner/partners", json=_payload(email="slug.clash@example.test", name="Sam Slug", new_area=None,
                                                        postcodes=["QQ1", "QQ2"]))
    assert r.status_code == 409 and r.json()["reason"] == "slug_taken" and r.json()["postcodes"] == ["QQ1", "QQ2", "QQ3"]
    # the same postcodes, in another order and another case: assigned, as a resend is
    r = api.post("/api/partner/partners", json=_payload(email="slug.clash@example.test", name="Sam Slug",
                                                        new_area={"slug": "qq1-qq2", "name": "Elsewhere", "postcodes": ["qq3", "QQ2", "QQ1"]}))
    assert r.status_code == 201, r.text
    assert r.json()["area_slug"] == "qq1-qq2" and not r.json()["area_created"]
    with session() as db:
        assert db.get(Area, area_id).partner_id == r.json()["partner_id"]


def test_refused_credentials_are_throttled_per_address(token):
    """Defence in depth: after API_FAIL_LIMIT refusals within the window the address is answered 429, even
    with the right secret, and the counter is per address rather than per client name. The throttle is
    cleared between tests by conftest."""
    bad = _api("not-the-secret-at-all")
    for _ in range(auth.API_FAIL_LIMIT):
        assert bad.get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    r = bad.get("/api/partner/partners", params={"email": "nobody@example.test"})
    assert r.status_code == 429 and r.json()["reason"] == "too_many_attempts" and r.headers["retry-after"] == str(auth.API_FAIL_WINDOW)
    assert "!" not in r.text
    r = _api(token).get("/api/partner/partners", params={"email": "nobody@example.test"})
    assert r.status_code == 429, "the address is throttled whatever credential it now presents"
    r = _api(token, Origin="https://evil.example").get("/api/partner/partners", params={"email": "nobody@example.test"})
    assert r.status_code == 403, "an Origin is refused before the throttle is consulted"
    auth._attempts.clear()
    assert _api(token).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    # a missing client name or secret counts too; a scope refusal or a bad body does not
    for _ in range(auth.API_FAIL_LIMIT):
        assert _api(token, None).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    assert _api(token).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 429
    auth._attempts.clear()
    for _ in range(auth.API_FAIL_LIMIT + 1):
        assert _api(token).get("/api/partner/partners", params={"email": "not-an-email"}).status_code == 422
    assert _api(token).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200


# ---- bootstrap from the environment and the CLI --------------------------------
def test_env_bootstrap_creates_rotates_and_never_reactivates(client, monkeypatch):
    secret = "bootstrap-secret-0123456789abcdefghijklmnopqrstuv"      # 48 characters
    with session() as db:
        row = db.query(ApiClient).filter(ApiClient.name == "grouphq").first()
        if row is not None:
            db.delete(row)
            db.commit()
    monkeypatch.setattr(config, "PARTNER_TOKEN_GROUPHQ", secret)
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_client"] == "created"
        row = db.query(ApiClient).filter(ApiClient.name == "grouphq").one()
        assert row.scopes == ["partners:write"] and row.active and row.token_hash == auth.hash_api_token(secret)
        assert secret not in row.token_hash
    assert _api(secret).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_client"] == "unchanged"
    # a new value rotates the secret; a disabled row stays disabled
    monkeypatch.setattr(config, "PARTNER_TOKEN_GROUPHQ", secret[::-1])
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_client"] == "rotated"
    assert _api(secret).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    assert _api(secret[::-1]).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    with session() as db:
        row = db.query(ApiClient).filter(ApiClient.name == "grouphq").one()
        row.active = False
        db.add(row)
        db.commit()
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_client"] == "unchanged"
        assert not db.query(ApiClient).filter(ApiClient.name == "grouphq").one().active
    assert _api(secret[::-1]).get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    # a value too short to be a secret is ignored: the API stays closed rather than weakly guarded
    with session() as db:
        db.delete(db.query(ApiClient).filter(ApiClient.name == "grouphq").one())
        db.commit()
    monkeypatch.setattr(config, "PARTNER_TOKEN_GROUPHQ", "short")
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_client"] is None
        assert db.query(ApiClient).filter(ApiClient.name == "grouphq").count() == 0
    assert _api("short").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401


def test_cli_creates_rotates_disables_and_lists(client):
    with session() as db:
        for row in db.query(ApiClient).filter(ApiClient.name.in_(["cli-client"])).all():
            db.delete(row)
        db.commit()
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["create", "cli-client", "partners:write,partners:read"]) == 0
    lines = out.getvalue().strip().splitlines()
    secret = lines[-1]
    assert len(secret) == 48 and "created with scopes partners:write, partners:read" in lines[0]
    assert _api(secret, "cli-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["list"]) == 0
    listing = out.getvalue()
    assert "cli-client" in listing and "active" in listing and secret not in listing
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["rotate", "cli-client"]) == 0
    rotated = out.getvalue().strip().splitlines()[-1]
    assert rotated != secret and len(rotated) == 48
    assert _api(secret, "cli-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    assert _api(rotated, "cli-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    with redirect_stdout(io.StringIO()):
        assert cli.main(["disable", "cli-client"]) == 0
    assert _api(rotated, "cli-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 401
    with redirect_stdout(io.StringIO()):
        assert cli.main(["enable", "cli-client"]) == 0
    assert _api(rotated, "cli-client").get("/api/partner/partners", params={"email": "nobody@example.test"}).status_code == 200
    with redirect_stdout(io.StringIO()):
        assert cli.main(["create", "cli-client", "partners:write"]) == 2          # already exists
        assert cli.main(["create", "Bad Name", "partners:write"]) == 2
        assert cli.main(["create", "another", "partners:everything"]) == 2
