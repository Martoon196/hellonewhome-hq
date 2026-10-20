"""Property instructions ("Let this property with Hello New Home", plan Phase 3 C2) and the send to Ordris on
instructed (C3): the intake routes, routing by postcode, idempotency, the read-back, the admin and portal
screens, the outbox and its retry.

Ordris is never called for real: `ordris_sync.http_client` is replaced with an httpx client on a MockTransport
whose handler the test controls."""
from __future__ import annotations

import io
import json
import logging
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import auth, config, migrations, seed as seed_module
from app.db import make_engine
from app.main import app
from app.models import ApiClient, Area, AuditLog, Instruction, Outbox, Partner
from app.routers import partner_api
from app.services import instructions, mail, ordris_sync
from scripts import outbox_retry
from tests.conftest import ADMIN_EMAIL, make_partner, session

PP = "propertypredator"
KP = "keyproperties"
ORDRIS_URL = "https://ordris.test"
ORDRIS_TOKEN = "ordris-secret-for-hnh-0123456789abcdefghijklmn"


def _make_client(name: str, scopes: str) -> str:
    with session() as db:
        existing = db.query(ApiClient).filter(ApiClient.name == name).first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        _, token = auth.create_api_client(db, name, scopes)
        db.commit()
    return token


@pytest.fixture(scope="module")
def pp_token(client):
    return _make_client(PP, "properties:write")


@pytest.fixture(scope="module")
def kp_token(client):
    return _make_client(KP, "properties:write")


@pytest.fixture(scope="module")
def reader_token(client):
    return _make_client("status-reader", "properties:read")


@pytest.fixture(scope="module")
def grouphq_token(client):
    return _make_client("grouphq", "partners:write")


def _api(token: str | None, name: str | None = PP, **extra_headers) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    if token is not None:
        c.headers["X-Service-Token"] = token
    if name is not None:
        c.headers["X-Partner-Client"] = name
    c.headers.update(extra_headers)
    return c


def _body(**overrides) -> dict:
    body = {
        "external_ref": "pp:deal-swansea-1",
        "origin_url": "https://propertypredator.com/pipeline/123",
        "introducer": {"person_key": "Investor@Example.test", "pp_account_id": "acct_123"},
        "landlord": {"name": "Lena Landlord", "email": "lena@example.test", "phone": "07700 900900"},
        "consent": {"given": True, "at": "2026-10-20T09:30:00+01:00", "text_version": "hnh-landlord-2026-10"},
        "property": {"address": "12 Mumbles Road, Swansea", "postcode": "sa1 3aa", "bedrooms": "3", "property_type": "Terraced house",
                     "furnished": "unfurnished", "rent_expectation": "£1,150 pcm", "available_from": "2026-12-01",
                     "service_line": "residential"},
        "notes": "Keen to move quickly.",
    }
    body.update(overrides)
    return body


def _mock_ordris(monkeypatch, handler):
    """Point the sender at a fake Ordris: configured, and every HTTP call answered by `handler`."""
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", ORDRIS_TOKEN)
    monkeypatch.setattr(ordris_sync, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def _ordris_ok(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"ok": True, "landlord_client_id": "lc_1", "property_id": "prop_1", "tenancy_id": None})
    return handler


# ---- intake refusals -----------------------------------------------------------
def test_intake_refusals_write_nothing(pp_token, grouphq_token):
    api = _api(pp_token)
    r = _api(None).post("/api/partner/properties", json=_body())
    assert r.status_code == 401 and r.json()["reason"] == "missing_token"
    r = _api("not-the-secret-at-all").post("/api/partner/properties", json=_body())
    assert r.status_code == 401 and r.json()["reason"] == "invalid_client"
    r = _api(grouphq_token, "grouphq").post("/api/partner/properties", json=_body())
    assert r.status_code == 403 and r.json() == {"ok": False, "reason": "scope_missing",
                                                 "message": "This client does not have the properties:write scope."}
    for origin in ("https://propertypredator.com", "null", "https://evil.example"):
        r = _api(pp_token, Origin=origin).post("/api/partner/properties", json=_body())
        assert r.status_code == 403 and r.json()["reason"] == "origin_not_allowed", origin
    # consent: missing, refused, given without the wording version, or not an object
    body = _body()
    del body["consent"]
    r = api.post("/api/partner/properties", json=body)
    assert r.status_code == 422 and r.json()["reason"] == "consent_required" and r.json()["field"] == "consent"
    r = api.post("/api/partner/properties", json=_body(consent={"given": False, "text_version": "v1"}))
    assert r.status_code == 422 and r.json()["reason"] == "consent_required" and r.json()["field"] == "consent.given"
    r = api.post("/api/partner/properties", json=_body(consent={"given": True}))
    assert r.status_code == 422 and r.json()["reason"] == "consent_required" and r.json()["field"] == "consent.text_version"
    r = api.post("/api/partner/properties", json=_body(consent=True))
    assert r.status_code == 422 and r.json()["reason"] == "consent_required"
    r = api.post("/api/partner/properties", json=_body(consent={"given": True, "text_version": "v1", "at": "yesterday"}))
    assert r.status_code == 422 and r.json()["field"] == "consent.at"
    # the rest of the body
    r = api.post("/api/partner/properties", json=_body(landlord={"name": "No Contact"}))
    assert r.status_code == 422 and r.json()["field"] == "landlord"
    r = api.post("/api/partner/properties", json=_body(landlord={"name": "Bad Email", "email": "nope"}))
    assert r.status_code == 422 and r.json()["field"] == "landlord.email"
    r = api.post("/api/partner/properties", json=_body(property={"address": "Somewhere", "postcode": "not a postcode"}))
    assert r.status_code == 422 and r.json()["field"] == "property.postcode"
    r = api.post("/api/partner/properties", json=_body(property={"address": "Somewhere", "postcode": "SA1 1AA", "service_line": "garages"}))
    assert r.status_code == 422 and r.json()["field"] == "property.service_line"
    r = api.post("/api/partner/properties", json=_body(property={"address": "Somewhere", "postcode": "SA1 1AA", "bedrooms": "many"}))
    assert r.status_code == 422 and r.json()["field"] == "property.bedrooms"
    r = api.post("/api/partner/properties", json=_body(origin_system="fax"))
    assert r.status_code == 422 and r.json()["field"] == "origin_system"
    r = api.post("/api/partner/properties", json=_body(origin_url="javascript:alert(1)"))
    assert r.status_code == 422 and r.json()["field"] == "origin_url"
    r = api.post("/api/partner/properties", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_json"
    body = _body()
    del body["external_ref"]
    r = api.post("/api/partner/properties", json=body)
    assert r.status_code == 422 and r.json()["field"] == "external_ref"
    with session() as db:
        assert db.query(Instruction).count() == 0
    for r in (api.post("/api/partner/properties", json=_body(consent=None)),
              _api(None).post("/api/partner/properties", json=_body()),
              _api(pp_token, Origin="https://evil.example").post("/api/partner/properties", json=_body())):
        assert "!" not in r.text


# ---- routing -------------------------------------------------------------------
def test_routing_sa1_to_head_office_ta1_to_somerset_partner_unknown_uncovered(pp_token, kp_token, monkeypatch, caplog):
    sent: list[tuple[str, str, list[str]]] = []
    real_send = mail.send

    def fake_send(subject, body, to):
        sent.append((subject, body, to))
        real_send(subject, body, to)        # SMTP is unset in tests: this only writes the "not sent" log line
        return True
    monkeypatch.setattr(mail, "send", fake_send)
    caplog.set_level(logging.INFO, logger="hnh.mail")
    api = _api(pp_token)
    r = api.post("/api/partner/properties", json=_body())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["ref"].startswith("HNH-") and len(body["ref"]) == 10
    record = body["instruction"]
    assert record["ref"] == body["ref"] and record["external_ref"] == "pp:deal-swansea-1" and record["origin_system"] == "pp"
    assert record["status"] == "new" and record["covered"] and record["area"]["slug"] == "swansea" and record["area"]["hq"]
    assert record["partner_first"] == "the head office team"
    assert record["status_history"] == [{"at": record["status_history"][0]["at"], "from": None, "to": "new", "by": f"api:{PP}"}]
    assert record["ordris"] == {"landlord_client_id": None, "property_id": None, "tenancy_id": None}
    assert "landlord" not in record and "payload" not in record and "introducer" not in str(record)
    assert r.headers["cache-control"] == "no-store"
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-swansea-1").one()
        assert inst.ref == Instruction.make_ref(inst.id) == body["ref"]
        assert inst.area.slug == "swansea" and inst.partner_id is None          # a head-office area has no partner
        assert (inst.postcode, inst.outward) == ("SA1 3AA", "SA1")
        assert (inst.landlord_name, inst.landlord_email, inst.landlord_phone) == ("Lena Landlord", "lena@example.test", "07700 900900")
        assert inst.consent_at.isoformat() == "2026-10-20T08:30:00" and inst.consent_text_version == "hnh-landlord-2026-10"
        assert inst.consent_at.tzinfo is None
        assert (inst.bedrooms, inst.rent_expectation, inst.furnished, inst.service_line) == (3, 1150, "unfurnished", "residential")
        assert inst.available_from.isoformat() == "2026-12-01" and inst.property_type == "Terraced house"
        assert inst.introducer_person_key == "investor@example.test" and inst.introducer_pp_account_id == "acct_123"
        assert inst.origin_url == "https://propertypredator.com/pipeline/123" and inst.notes == "Keen to move quickly."
        assert inst.payload["landlord"]["name"] == "Lena Landlord"
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst.id}").one()
        assert entry.action == "partner_api.property.create" and f"via api client {PP}" in entry.detail
    # head office hears about it; the head-office area adds no partner address
    assert len(sent) == 1
    subject, text, to = sent[0]
    assert subject == f"[HNH property] {body['ref']}, SA1 3AA (Swansea)" and to == [config.LEAD_NOTIFY_TO]
    assert "Lena Landlord" not in subject                                   # the subject is what the mail log quotes
    assert "Landlord: Lena Landlord" in text and "Rent expectation: £1,150 a month" in text and f"/admin/instructions/" in text

    # TA1 is Somerset, whose partner is active with an address: routed to them and copied in
    r = _api(kp_token, KP).post("/api/partner/properties", json=_body(
        external_ref="kp:deal-77", origin_url="https://portal.keypropertiesuk.co.uk/deals/77",
        introducer={"person_key": "sourcer@example.test", "kp_user_id": "42"},
        landlord={"name": "Tom Taunton", "phone": "07700 900901"},
        property={"address": "4 Castle Green, Taunton", "postcode": "TA1 4AA", "bedrooms": 2, "service_line": "hmo"}))
    assert r.status_code == 201, r.text
    record = r.json()["instruction"]
    assert record["origin_system"] == "kp" and record["area"]["slug"] == "somerset" and record["partner_first"] == "Martin"
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "kp:deal-77").one()
        somerset = db.query(Area).filter(Area.slug == "somerset").one()
        assert inst.area_id == somerset.id and inst.partner_id == somerset.partner_id and inst.partner.first_name == "Martin"
        assert inst.introducer_kp_user_id == "42" and inst.landlord_email == "" and inst.rent_expectation is None
        assert inst.service_line == "hmo"
    assert sent[-1][2] == [config.LEAD_NOTIFY_TO, "martin.howard1984@gmail.com"]

    # a postcode nobody covers is still taken, uncovered, for head office to place
    r = api.post("/api/partner/properties", json=_body(external_ref="pp:deal-aberdeen", property={"address": "1 Union Street, Aberdeen", "postcode": "AB10 1AA"}))
    assert r.status_code == 201, r.text
    record = r.json()["instruction"]
    assert record["area"] is None and not record["covered"] and record["partner_first"] == "the head office team"
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-aberdeen").one()
        assert inst.area_id is None and inst.partner_id is None and inst.outward == "AB10"
    assert sent[-1][2] == [config.LEAD_NOTIFY_TO]
    # Log lines carry references and postcodes, not names (data sharing agreement, section 8).
    assert f"[HNH property] {body['ref']}" in caplog.text
    for name in ("Lena Landlord", "Tom Taunton", "lena@example.test", "07700 900"):
        assert name not in caplog.text, name
    assert mail.log_label("[HNH lead] Rent review: Jane Smith (SA1 1AA)") == "[HNH lead] Rent review"
    assert mail.log_label("Jane Smith wrote") == "message"


# ---- idempotency ---------------------------------------------------------------
def test_same_external_ref_returns_the_existing_reference(pp_token, kp_token):
    api = _api(pp_token)
    with session() as db:
        before = db.query(Instruction).count()
        existing = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-swansea-1").one()
        existing_ref = existing.ref
    r = api.post("/api/partner/properties", json=_body(landlord={"name": "Changed Name", "phone": "07700 900000"}))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "created": False, "ref": existing_ref, "instruction": r.json()["instruction"]}
    assert r.json()["instruction"]["external_ref"] == "pp:deal-swansea-1"
    with session() as db:
        assert db.query(Instruction).count() == before
        assert db.get(Instruction, existing.id).landlord_name == "Lena Landlord"       # a resend changes nothing
    # the same reference from another system is someone else's, and is refused rather than answered
    r = _api(kp_token, KP).post("/api/partner/properties", json=_body())
    assert r.status_code == 409 and r.json()["reason"] == "external_ref_taken" and r.json()["external_ref"] == "pp:deal-swansea-1"
    # a resend that fails validation is still answered with the existing reference: the record already stands
    r = api.post("/api/partner/properties", json={"external_ref": "pp:deal-swansea-1"})
    assert r.status_code == 200 and r.json()["ref"] == existing_ref


def test_daily_cap_counts_new_instructions_only(pp_token, monkeypatch):
    monkeypatch.setattr(partner_api, "PROPERTIES_DAILY_LIMIT", 2)
    api = _api(pp_token)
    for i in range(2):
        r = api.post("/api/partner/properties", json=_body(external_ref=f"pp:cap-{i}"))
        assert r.status_code == 201, r.text
    r = api.post("/api/partner/properties", json=_body(external_ref="pp:cap-over"))
    assert r.status_code == 429 and r.json()["reason"] == "daily_limit" and r.headers["retry-after"] == str(partner_api.PROPERTIES_WINDOW)
    assert "!" not in r.text
    # resends are answered, and a bad body never uses a slot
    assert api.post("/api/partner/properties", json=_body(external_ref="pp:cap-0")).status_code == 200
    assert api.post("/api/partner/properties", json=_body(external_ref="pp:cap-bad", consent=None)).status_code == 422
    with session() as db:
        assert db.query(Instruction).filter(Instruction.external_ref.like("pp:cap-%")).count() == 2
    # the cap is per client: Key Properties is unaffected by Property Predator's day
    auth._attempts.clear()


# ---- read-back -----------------------------------------------------------------
def test_status_read_back(pp_token, reader_token, admin_client):
    r = _api(reader_token, "status-reader").get("/api/partner/properties/pp:deal-swansea-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["found"] and body["ref"].startswith("HNH-")
    record = body["instruction"]
    assert record["status"] == "new" and record["area"]["slug"] == "swansea" and record["partner_first"] == "the head office team"
    assert record["ordris"]["property_id"] is None and len(record["status_history"]) == 1
    assert "landlord" not in record and "payload" not in record
    r = _api(reader_token, "status-reader").get("/api/partner/properties/pp:nobody-sent-this")
    assert r.status_code == 200 and r.json() == {"ok": True, "found": False, "instruction": None}
    # a write scope reads too; the read scope cannot write
    assert _api(pp_token).get("/api/partner/properties/pp:deal-swansea-1").status_code == 200
    assert _api(reader_token, "status-reader").post("/api/partner/properties", json=_body()).status_code == 403
    # head office moves it on; the sender sees the change and who made it
    with session() as db:
        inst_id = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-swansea-1").one().id
    r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "contacted", "notes": "Called 20 Oct", "area_id": ""})
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.headers["location"]
    record = _api(pp_token).get("/api/partner/properties/pp:deal-swansea-1").json()["instruction"]
    assert record["status"] == "contacted" and record["area"] is None
    assert record["status_history"][-1]["from"] == "new" and record["status_history"][-1]["to"] == "contacted"
    # Who moved it is a role, never a Hello New Home email address; the stored history keeps the email for the audit screen.
    assert record["status_history"][-1]["by"] == "head office"
    assert "@" not in json.dumps(record["status_history"])
    with session() as db:
        assert db.get(Instruction, inst_id).status_history[-1]["by"] == f"admin:{ADMIN_EMAIL}"
    assert instructions.public_by("partner:Martin") == "partner" and instructions.public_by("api:keyproperties") == "api:keyproperties"
    assert instructions.public_by("someone@hellonewhome.co.uk") == "head office"
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.notes == "Called 20 Oct" and inst.area_id is None and inst.partner_id is None
    # put the area back for the tests that follow
    with session() as db:
        swansea_id = db.query(Area).filter(Area.slug == "swansea").one().id
    assert admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "contacted", "notes": "Called 20 Oct", "area_id": str(swansea_id)}).status_code == 303


# ---- admin and portal pages ------------------------------------------------------
def test_admin_pages_list_filter_and_detail(pp_token, admin_client):
    with session() as db:
        swansea = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-swansea-1").one()
        taunton = db.query(Instruction).filter(Instruction.external_ref == "kp:deal-77").one()
        somerset_id = db.query(Area).filter(Area.slug == "somerset").one().id
        swansea_ref, taunton_ref, swansea_id = swansea.ref, taunton.ref, swansea.id
    page = admin_client.get("/admin/instructions")
    assert page.status_code == 200 and swansea_ref in page.text and taunton_ref in page.text and "Instructions" in page.text
    assert "Lena Landlord" in page.text and "Tom Taunton" in page.text
    page = admin_client.get("/admin/instructions", params={"status": "new"})
    assert taunton_ref in page.text and swansea_ref not in page.text            # Swansea is contacted now
    page = admin_client.get("/admin/instructions", params={"area": somerset_id})
    assert taunton_ref in page.text and swansea_ref not in page.text
    page = admin_client.get("/admin/instructions", params={"origin": "pp"})
    assert swansea_ref in page.text and taunton_ref not in page.text
    page = admin_client.get("/admin/instructions", params={"q": "sa1"})
    assert swansea_ref in page.text and taunton_ref not in page.text
    page = admin_client.get("/admin/instructions", params={"area": "none"})
    assert "pp:deal-aberdeen" not in page.text and "Aberdeen" in page.text
    detail = admin_client.get(f"/admin/instructions/{swansea_id}")
    assert detail.status_code == 200
    for text in (swansea_ref, "Lena Landlord", "lena@example.test", "hnh-landlord-2026-10", "pp:deal-swansea-1", "investor@example.test",
                 "acct_123", "£1,150 a month", "Everything the sender posted", "Called 20 Oct", "new to contacted"):
        assert text in detail.text, text
    assert "Send to Ordris" not in detail.text                                  # not instructed yet
    assert "Property Predator" in detail.text
    assert admin_client.get("/admin/instructions/999999").status_code == 404
    r = admin_client.post(f"/admin/instructions/{swansea_id}", data={"status": "bogus"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    # the admin nav and the audit log carry it
    assert 'href="/admin/instructions"' in admin_client.get("/admin").text
    assert f"instruction:{swansea_id}" in admin_client.get("/admin/audit").text


def test_portal_pages_are_scoped_and_money_free(pp_token, client, partner_a, partner_b, partner_a_client, partner_b_client, admin_client):
    r = _api(pp_token).post("/api/partner/properties", json=_body(
        external_ref="pp:deal-teesside", introducer={"person_key": "investor@example.test", "pp_account_id": "acct_999"},
        landlord={"name": "Tess Teesside", "phone": "07700 900777", "email": "tess@example.test"},
        property={"address": "9 Linthorpe Road, Middlesbrough", "postcode": "TS1 2AB", "bedrooms": 4, "rent_expectation": 950,
                  "furnished": "part"},
        fee_note="purchase price 180000"))
    assert r.status_code == 201, r.text
    ref = r.json()["ref"]
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-teesside").one()
        inst_id = inst.id
        assert inst.partner_id == partner_a.partner_id and inst.payload["fee_note"] == "purchase price 180000"
        other_id = db.query(Instruction).filter(Instruction.external_ref == "kp:deal-77").one().id
    page = partner_a_client.get("/partners/instructions")
    assert page.status_code == 200 and ref in page.text and "Tess Teesside" in page.text and "Tom Taunton" not in page.text
    assert "Instructions" in page.text and 'href="/partners/instructions"' in partner_a_client.get("/partners").text
    assert partner_b_client.get("/partners/instructions").status_code == 200 and ref not in partner_b_client.get("/partners/instructions").text
    assert partner_a_client.get(f"/partners/instructions/{other_id}").status_code == 404
    assert partner_b_client.get(f"/partners/instructions/{inst_id}").status_code == 404
    detail = partner_a_client.get(f"/partners/instructions/{inst_id}")
    assert detail.status_code == 200
    for text in ("Tess Teesside", "tess@example.test", "wa.me/447700900777", "9 Linthorpe Road", "£950 a month", "Part furnished", "Property Predator"):
        assert text in detail.text, text
    # money and introducer details stay with head office
    for hidden in ("purchase price", "180000", "acct_999", "investor@example.test", "pp:deal-teesside", "Everything the sender posted", "£495", "12%"):
        assert hidden not in detail.text, hidden
    # the partner moves it on and leaves a note; partner B cannot, even blind
    r = partner_b_client.post(f"/partners/instructions/{inst_id}", data={"status": "lost"})
    assert r.status_code == 404
    r = partner_a_client.post(f"/partners/instructions/{inst_id}", data={"status": "appraised", "note": "Visited, £950 is fair"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.status == "appraised" and "Visited" in inst.notes and "Alice" in inst.notes
        assert inst.status_history[-1]["by"] == "partner:Alice" and inst.status_history[-1]["from"] == "new"
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst_id}", AuditLog.action == "instruction.update").one()
        assert entry.user_id == partner_a.user_id and "status new to appraised" in entry.detail
    r = partner_a_client.post(f"/partners/instructions/{inst_id}", data={"status": "appraised"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    page = partner_a_client.get("/partners/instructions", params={"status": "appraised"})
    assert ref in page.text
    # admins see everything in the portal view too
    assert admin_client.get(f"/partners/instructions/{other_id}").status_code == 200
    # anonymous visitors are sent to the login
    anon = client.get(f"/partners/instructions/{inst_id}")
    assert anon.status_code == 303 and anon.headers["location"].startswith("/partners/login")


# ---- instructed sends to Ordris ----------------------------------------------------
def test_instructed_sends_to_ordris_and_stores_the_ids(pp_token, admin_client, monkeypatch):
    calls: list[httpx.Request] = []
    _mock_ordris(monkeypatch, _ordris_ok(calls))
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-swansea-1").one()
        inst_id, ref = inst.id, inst.ref
    r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "instructed", "notes": "Signed today", "area_id": str(inst.area_id)})
    assert r.status_code == 303, r.text
    assert "ok=" in r.headers["location"] and "Sent+to+Ordris" in r.headers["location"], r.headers["location"]
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == f"{ORDRIS_URL}/api/partner/hnh/instructions" and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {ORDRIS_TOKEN}" and req.headers["x-service-token"] == ORDRIS_TOKEN
    assert req.headers["x-partner-client"] == "hnh" and "origin" not in req.headers
    assert json.loads(req.content) == {
        "external_ref": ref,
        "landlord": {"name": "Lena Landlord", "email": "lena@example.test", "phone": "07700 900900"},
        "property": {"name": "12 Mumbles Road", "address": "12 Mumbles Road, Swansea", "postcode": "SA1 3AA", "bedrooms": 3,
                     "property_type": "Terraced house"},
        "tenancy": None,
    }
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert (inst.status, inst.ordris_landlord_client_id, inst.ordris_property_id, inst.ordris_tenancy_id) == ("instructed", "lc_1", "prop_1", "")
        row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
        assert (row.kind, row.state, row.attempts, row.last_error) == ("ordris.instruction", "sent", 1, "") and row.sent_at is not None
        assert row.payload["external_ref"] == ref and ORDRIS_TOKEN not in json.dumps(row.payload)
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst_id}", AuditLog.action == "instruction.ordris").one()
        assert "lc_1" in entry.detail and ORDRIS_TOKEN not in entry.detail
    # the sender reads the ids back; the admin page shows them and offers no second send
    record = _api(pp_token).get("/api/partner/properties/pp:deal-swansea-1").json()["instruction"]
    assert record["ordris"] == {"landlord_client_id": "lc_1", "property_id": "prop_1", "tenancy_id": None} and record["status"] == "instructed"
    detail = admin_client.get(f"/admin/instructions/{inst_id}")
    assert "In Ordris" in detail.text and "lc_1" in detail.text and "Send to Ordris" not in detail.text
    assert ORDRIS_TOKEN not in detail.text
    # saving the same status again, or moving on to let, never sends twice
    assert admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "let", "notes": "Signed today", "area_id": str(inst.area_id)}).status_code == 303
    assert len(calls) == 1
    r = admin_client.post(f"/admin/instructions/{inst_id}/send")
    assert r.status_code == 303 and "already+in+Ordris" in r.headers["location"]
    assert len(calls) == 1


def test_failed_send_leaves_an_outbox_row_that_the_retry_command_sends(pp_token, admin_client, monkeypatch, caplog):
    answers = [httpx.Response(503, json={"ok": False, "reason": "not_configured", "message": "ORDRIS_PARTNER_TOKEN_HNH is not set, so this route is switched off."})]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if answers:
            return answers.pop(0)
        return httpx.Response(200, json={"ok": True, "ids": {"landlordClientId": "lc_2", "propertyId": "prop_2", "tenancyId": "ten_2"}})

    _mock_ordris(monkeypatch, handler)
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "kp:deal-77").one()
        inst_id = inst.id
    with caplog.at_level("INFO"):
        r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "instructed", "notes": "", "area_id": str(inst.area_id)})
    assert r.status_code == 303 and "err=" in r.headers["location"] and "Not+sent+to+Ordris+yet" in r.headers["location"]
    assert "!" not in r.headers["location"]
    assert ORDRIS_TOKEN not in caplog.text
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.status == "instructed" and not inst.sent_to_ordris          # the status change stands; the send waits
        row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
        assert (row.state, row.attempts) == ("failed", 1)
        assert row.last_error.startswith("Ordris answered 503") and "switched off" in row.last_error and ORDRIS_TOKEN not in row.last_error
        row_id = row.id
    detail = admin_client.get(f"/admin/instructions/{inst_id}")
    assert "Not in Ordris yet" in detail.text and "Resend" in detail.text and "Ordris answered 503" in detail.text
    assert "Send to Ordris" not in detail.text                                  # a queued message is resent, not duplicated
    assert admin_client.get("/admin/instructions").text.count("still waiting or failed") == 1

    # the retry command sends it (the --list form only reports)
    out = io.StringIO()
    with redirect_stdout(out):
        assert outbox_retry.main(["--list"]) == 1
    assert f"#{row_id}" in out.getvalue() and "failed" in out.getvalue() and ORDRIS_TOKEN not in out.getvalue()
    out = io.StringIO()
    with redirect_stdout(out):
        assert outbox_retry.main([]) == 0
    assert "1 sent, 0 still waiting" in out.getvalue() and ORDRIS_TOKEN not in out.getvalue()
    assert len(calls) == 2 and json.loads(calls[1].content)["landlord"]["name"] == "Tom Taunton"
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert (inst.ordris_landlord_client_id, inst.ordris_property_id, inst.ordris_tenancy_id) == ("lc_2", "prop_2", "ten_2")
        row = db.get(Outbox, row_id)
        assert (row.state, row.attempts, row.last_error) == ("sent", 2, "") and row.sent_at is not None
    out = io.StringIO()
    with redirect_stdout(out):
        assert outbox_retry.main([]) == 0
    assert "nothing waiting" in out.getvalue()
    record = _api(pp_token).get("/api/partner/properties/kp:deal-77").json()["instruction"]
    assert record["ordris"] == {"landlord_client_id": "lc_2", "property_id": "prop_2", "tenancy_id": "ten_2"}


def test_resend_button_and_an_unreachable_or_unconfigured_ordris(pp_token, admin_client, partner_a, partner_a_client, monkeypatch, caplog):
    # a partner marking a property instructed triggers the send too; here Ordris is unreachable
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_ordris(monkeypatch, down)
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-teesside").one()
        inst_id = inst.id
    with caplog.at_level("INFO"):
        r = partner_a_client.post(f"/partners/instructions/{inst_id}", data={"status": "instructed", "note": "Signed at the kitchen table"})
    assert r.status_code == 303 and "ok=" in r.headers["location"] and "Head+office+will+set" in r.headers["location"]
    assert ORDRIS_TOKEN not in caplog.text
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.status == "instructed" and inst.status_history[-1]["by"] == "partner:Alice"
        row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
        assert row.state == "failed" and row.last_error.startswith("Could not reach Ordris: ConnectError") and ORDRIS_TOKEN not in row.last_error
        row_id = row.id
    # the partner's own page says nothing about the failure; the admin's offers Resend
    assert "Resend" not in partner_a_client.get(f"/partners/instructions/{inst_id}").text
    detail = admin_client.get(f"/admin/instructions/{inst_id}")
    assert "Resend" in detail.text and "Could not reach Ordris" in detail.text

    # Ordris not configured at all: Resend keeps the row pending and says what to set; nothing is attempted
    calls: list[httpx.Request] = []
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", "")
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", "")
    monkeypatch.setattr(ordris_sync, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(_ordris_ok(calls))))
    r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
    assert r.status_code == 303 and "err=" in r.headers["location"] and "not+configured" in r.headers["location"]
    assert calls == []
    with session() as db:
        row = db.get(Outbox, row_id)
        assert (row.state, row.attempts) == ("pending", 1) and row.last_error == ordris_sync.NOT_CONFIGURED
    out = io.StringIO()
    with redirect_stdout(out):
        assert outbox_retry.main([]) == 1
    assert "left waiting" in out.getvalue() and calls == []
    assert "Ordris is not configured" in admin_client.get("/admin/instructions").text

    # configured again: the Resend button delivers it
    _mock_ordris(monkeypatch, _ordris_ok(calls))
    r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
    assert r.status_code == 303 and "ok=" in r.headers["location"] and "Sent+to+Ordris" in r.headers["location"]
    assert len(calls) == 1 and json.loads(calls[0].content)["property"]["postcode"] == "TS1 2AB"
    with session() as db:
        inst = db.get(Instruction, inst_id)
        assert inst.ordris_property_id == "prop_1"
        row = db.get(Outbox, row_id)
        assert (row.state, row.attempts) == ("sent", 2)
    r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
    assert r.status_code == 303 and "already+sent" in r.headers["location"] and len(calls) == 1
    assert admin_client.post(f"/admin/instructions/{inst_id}/outbox/999999/resend").status_code == 404
    # "Send to Ordris" is refused before the landlord has instructed
    r = _api(pp_token).post("/api/partner/properties", json=_body(external_ref="pp:deal-early", property={"address": "1 Early Close", "postcode": "TS2 1AA"}))
    with session() as db:
        early_id = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-early").one().id
    r = admin_client.post(f"/admin/instructions/{early_id}/send")
    assert r.status_code == 303 and "instructed+first" in r.headers["location"] and len(calls) == 1


def test_send_to_ordris_button_queues_and_sends(pp_token, admin_client, monkeypatch):
    """An instruction marked instructed while Ordris was unconfigured (or before this feature) is sent by the button."""
    calls: list[httpx.Request] = []
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", "")
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", "")
    r = _api(pp_token).post("/api/partner/properties", json=_body(external_ref="pp:deal-button", property={"address": "2 Button Row, Swansea", "postcode": "SA2 0AA"}))
    assert r.status_code == 201
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-button").one()
        inst_id = inst.id
    r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "instructed", "notes": "", "area_id": str(inst.area_id)})
    assert r.status_code == 303 and "not+configured" in r.headers["location"]
    with session() as db:
        row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
        assert row.state == "pending" and row.attempts == 0
        db.delete(row)                      # as if the feature arrived after the status was set
        db.commit()
    detail = admin_client.get(f"/admin/instructions/{inst_id}")
    assert "Send to Ordris" in detail.text
    _mock_ordris(monkeypatch, _ordris_ok(calls))
    r = admin_client.post(f"/admin/instructions/{inst_id}/send")
    assert r.status_code == 303 and "Sent+to+Ordris" in r.headers["location"] and len(calls) == 1
    with session() as db:
        assert db.get(Instruction, inst_id).ordris_landlord_client_id == "lc_1"
        assert db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one().state == "sent"


def test_ordris_answer_without_ids_or_with_ok_false_is_a_failure(pp_token, admin_client, monkeypatch):
    answers = [httpx.Response(200, json={"ok": True}), httpx.Response(200, json={"ok": False, "reason": "no_org", "message": "ORDRIS_HNH_ORG_ID is unset."})]
    _mock_ordris(monkeypatch, lambda request: answers.pop(0))
    r = _api(pp_token).post("/api/partner/properties", json=_body(external_ref="pp:deal-noids", property={"address": "3 Empty Row, Swansea", "postcode": "SA3 1AA"}))
    assert r.status_code == 201
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:deal-noids").one()
        inst_id, area_id = inst.id, inst.area_id
    r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "instructed", "notes": "", "area_id": str(area_id)})
    assert r.status_code == 303 and "without+a+landlord+or+property+id" in r.headers["location"]
    with session() as db:
        row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
        assert row.state == "failed"
        row_id = row.id
    r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
    assert r.status_code == 303 and "ok+false" in r.headers["location"] and "no_org" in r.headers["location"]
    with session() as db:
        assert db.get(Outbox, row_id).attempts == 2 and not db.get(Instruction, inst_id).sent_to_ordris
    assert ordris_sync.parse_ids({"landlord_client": {"id": 7}, "property": {"id": "p9"}}) == {"landlord_client_id": "7", "property_id": "p9", "tenancy_id": ""}
    assert ordris_sync.parse_ids("nonsense") == {}


# ---- bootstrap and migration ------------------------------------------------------
def test_env_bootstrap_creates_the_two_sender_clients(client, monkeypatch):
    secret = "pp-bootstrap-secret-0123456789abcdefghijklmnopqr"
    with session() as db:
        for row in db.query(ApiClient).filter(ApiClient.name.in_([PP, KP])).all():
            db.delete(row)
        db.commit()
    monkeypatch.setattr(config, "PARTNER_TOKEN_PROPERTYPREDATOR", secret)
    monkeypatch.setattr(config, "PARTNER_TOKEN_KEYPROPERTIES", "short")
    with session() as db:
        report = seed_module.seed(db, quiet=True)
        assert report["api_clients"] == {PP: "created"} and report["api_client"] is None
        row = db.query(ApiClient).filter(ApiClient.name == PP).one()
        assert row.scopes == ["properties:write"] and row.active and row.token_hash == auth.hash_api_token(secret)
        assert db.query(ApiClient).filter(ApiClient.name == KP).count() == 0       # too short: ignored, API stays closed
    r = _api(secret).post("/api/partner/properties", json=_body(external_ref="pp:deal-bootstrap", property={"address": "5 Boot Lane, Swansea", "postcode": "SA4 1AA"}))
    assert r.status_code == 201, r.text
    assert _api(secret).post("/api/partner/partners", json={"email": "x@example.test", "name": "X", "tier": "area", "status": "active"}).status_code == 403
    with session() as db:
        assert seed_module.seed(db, quiet=True)["api_clients"] == {PP: "unchanged"}
    # the CLI knows the new scopes
    with session() as db:
        for row in db.query(ApiClient).filter(ApiClient.name == "props-cli").all():
            db.delete(row)
        db.commit()
    from scripts import api_client as cli
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["create", "props-cli", "properties:write"]) == 0
    assert "properties:write" in out.getvalue()


def test_migration_0003_adds_and_removes_both_tables(tmp_path: Path):
    eng = make_engine(f"sqlite:///{tmp_path / 'instr.db'}")
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    names = set(inspect(eng).get_table_names())
    assert {"instructions", "outbox"} <= names
    cols = {c["name"] for c in inspect(eng).get_columns("instructions")}
    assert {"ref", "external_ref", "origin_system", "origin_url", "introducer_person_key", "introducer_pp_account_id",
            "introducer_kp_user_id", "landlord_name", "landlord_email", "landlord_phone", "consent_at", "consent_text_version",
            "address", "postcode", "outward", "bedrooms", "property_type", "furnished", "rent_expectation", "available_from",
            "service_line", "area_id", "partner_id", "status", "status_history", "ordris_landlord_client_id", "ordris_property_id",
            "ordris_tenancy_id", "payload", "notes", "created_at", "updated_at"} <= cols
    assert {"id", "kind", "target", "payload", "attempts", "last_error", "state", "created_at", "sent_at"} == {c["name"] for c in inspect(eng).get_columns("outbox")}
    unique = {i["name"] for i in inspect(eng).get_indexes("instructions") if i["unique"]}
    assert {"ix_instructions_ref", "ix_instructions_external_ref"} <= unique
    with eng.begin() as conn:
        command.downgrade(migrations.alembic_config(conn), "0002_api_clients")
    assert not {"instructions", "outbox"} & set(inspect(eng).get_table_names())
    assert migrations.run_migrations(eng, quiet=True) == "upgraded" and migrations.current_revision(eng) == migrations.head_revision()
    eng.dispose()
