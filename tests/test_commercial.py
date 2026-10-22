"""Commercial lettings (plan, Phase 5): the fee sheet (approved, quoted plus VAT), the public pages, the commercial appraisal lead, the
routing rule for leads and instructions, the partner flag and what the portal shows, the seeded training page and
migration 0004.

Somerset is the seeded partner area (TA1, Martin Howard, active, no commercial clearance until this module sets it);
Swansea is the head-office area. The module leaves Martin exactly as it found him."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import auth, config, migrations, seed as seed_module
from app.auth import hash_password
from app.db import make_engine
from app.main import app
from app.models import ApiClient, Area, AuditLog, Instruction, Lead, Partner, Resource, Setting, User
from app.services import kit, mail, routing, settings as site_settings
from tests.conftest import PARTNER_PASSWORD, login, session

MARTIN = "martin.howard1984@gmail.com"
SENT_REFS = ("pp:commercial-1", "pp:commercial-2", "pp:residential-1")      # the instructions this module creates
FEE_FIGURES = ("£1,500", "£750", "£350", "8% of rent collected", "10% of the first year")
COMPLIANCE = ("No deposit scheme", "No Renters' Rights Act", "No Right to Rent", "Landlord and Tenant Act 1954", "contracting out",
              "MEES", "rating of E", "since 1 April 2023", "C by 2027", "B by 2030", "Business rates and empty-rates relief",
              "VAT option to tax", "a deposit attracts VAT only when it is drawn on", "Code for Leasing Business Premises 2020",
              # the summary names the two nations it does not describe, and the residential comparison names both it does
              "Scotland and Northern Ireland have their own commercial property law",
              "assured tenancies in England, occupation contracts in Wales", "Renting Homes (Wales) Act 2016 does the same in Wales")
SERVICE = ("Appraisal", "Marketing", "Heads of terms", "referenced", "your solicitor", "Rent collection, management and rent reviews")


def _visible(html: str) -> str:
    """The page without the doctype and comments, so a check for a stray character only sees what a reader sees."""
    return re.sub(r"<!--.*?-->", "", html.replace("<!doctype html>", ""), flags=re.S)


def _martin(db) -> Partner:
    return db.query(Partner).filter(Partner.email == MARTIN).one()


def _somerset(db) -> Area:
    return db.query(Area).filter(Area.slug == "somerset").one()


def _set_commercial(admin_client: TestClient, enabled: bool) -> None:
    """Toggle Martin's clearance through the admin partner form, exactly as head office would."""
    with session() as db:
        p = _martin(db)
        form = {"name": p.name, "first_name": p.first_name, "email": p.email, "phone": p.phone, "whatsapp": p.whatsapp,
                "whatsapp_display": p.whatsapp_display, "bio": p.bio, "tier": p.tier, "status": p.status,
                "units_managed": str(p.units_managed), "notes": p.notes, "areas": [str(a.id) for a in p.areas]}
        pid = p.id
    if enabled:
        form["commercial_enabled"] = "1"
    r = admin_client.post(f"/admin/partners/{pid}", data=form)
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.text
    with session() as db:
        assert _martin(db).commercial_enabled is enabled


def _lead(**fields) -> dict:
    body = {"kind": "commercial_appraisal", "name": "Carla Commercial", "phone": "07700 900555", "email": "carla@example.test",
            "postcode": "TA1 1AA", "address": "Unit 4, Castle Street, Taunton", "business_name": "Castle Street Bakery Ltd",
            "property_use": "retail", "size_sqft": "1200", "lease_status": "vacant", "asking_rent": "£18,000 a year",
            "_page": "/commercial"}
    body.update(fields)
    return body


@pytest.fixture(scope="module", autouse=True)
def _leave_things_as_found(client):
    """Martin keeps no clearance and no login, and the instructions this module sent are removed: the instruction
    tests that run later start from an empty instructions table."""
    yield
    with session() as db:
        p = _martin(db)
        p.commercial_enabled = False
        db.add(p)
        for u in db.query(User).filter(User.partner_id == p.id).all():
            db.query(AuditLog).filter(AuditLog.user_id == u.id).delete()      # the login's own audit rows go with it
            db.delete(u)
        for inst in db.query(Instruction).filter(Instruction.external_ref.in_(SENT_REFS)).all():
            db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst.id}").delete()
            db.delete(inst)
        db.commit()


@pytest.fixture(scope="module")
def martin_client(client) -> TestClient:
    with session() as db:
        p = _martin(db)
        assert not db.query(User).filter(User.partner_id == p.id).count()
        db.add(User(email=MARTIN, password_hash=hash_password(PARTNER_PASSWORD), role="partner", partner_id=p.id, is_active=True))
        db.commit()
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", MARTIN, PARTNER_PASSWORD)
    return c


@pytest.fixture(scope="module")
def sender_token(client) -> str:
    with session() as db:
        existing = db.query(ApiClient).filter(ApiClient.name == "commercial-sender").first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        _, token = auth.create_api_client(db, "commercial-sender", "properties:write")
        db.commit()
    return token


def _api(token: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.headers["X-Service-Token"] = token
    c.headers["X-Partner-Client"] = "commercial-sender"
    return c


def _instruction(**overrides) -> dict:
    body = {
        "external_ref": "pp:commercial-1", "origin_system": "pp",
        "landlord": {"name": "Olive Owner", "phone": "07700 900556"},
        "consent": {"given": True, "text_version": "hnh-landlord-2026-10"},
        "property": {"address": "Unit 4, Castle Street, Taunton", "postcode": "TA1 1AA", "service_line": "commercial",
                     "property_type": "Retail unit"},
    }
    body.update(overrides)
    return body


# ---- fee sheet: approved, quoted plus VAT, hidden again if head office unticks it -----------------
def test_commercial_fees_are_shown_plus_vat_and_hidden_when_unticked(client, admin_client):
    with session() as db:
        current = site_settings.get_all(db)
    # the founder approved the sheet: the seed revision ticked the box once, and the note is "plus VAT"
    assert current["fees_commercial_confirmed"] is True
    assert site_settings.commercial_fees(current)["vat_note"] == "plus VAT"
    assert config.FEES_COMMERCIAL_CONFIRMED is False
    page = client.get("/commercial").text
    assert site_settings.FEES_PENDING_SENTENCE not in page
    for figure in FEE_FIGURES:
        assert figure in page, figure
    # every commercial fee figure carries "plus VAT", in the table and in the hero
    for cell in ("10% of the first year&#39;s rent plus VAT, minimum £1,500 plus VAT", "8% of rent collected plus VAT",
                 "£750 plus VAT", "£350 plus VAT", "letting fee of the first year&#39;s rent, plus VAT", "All fees plus VAT."):
        assert cell in page, cell
    assert "registers for VAT" not in page
    admin_page = admin_client.get("/admin/settings").text
    assert "[CONFIRM]" not in admin_page and 'name="fees_commercial_confirmed"' in admin_page and 'value="£1,500"' in admin_page
    assert 'value="plus VAT"' in admin_page
    # unticked in the admin, the figures leave the public pages again and the [CONFIRM] badge returns
    with session() as db:
        site_settings.put(db, "fees_commercial_confirmed", False)
        db.commit()
    try:
        page = client.get("/commercial").text
        assert site_settings.FEES_PENDING_SENTENCE in page
        for figure in FEE_FIGURES:
            assert figure not in page, figure
        assert site_settings.commercial_fees(dict(current, fees_commercial_confirmed=False)) is None
        assert "[CONFIRM]" in admin_client.get("/admin/settings").text
    finally:
        with session() as db:
            site_settings.put(db, "fees_commercial_confirmed", True)
            db.commit()
    assert site_settings.FEES_PENDING_SENTENCE not in client.get("/commercial").text


def test_env_override_confirms_the_commercial_sheet_on_boot(client, monkeypatch):
    with session() as db:                       # start from an unticked box, as head office might leave it
        site_settings.put(db, "fees_commercial_confirmed", False)
        db.commit()
    monkeypatch.setattr(config, "FEES_COMMERCIAL_CONFIRMED", True)
    seed_module.seed(quiet=True)
    with session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is True
        site_settings.put(db, "fees_commercial_confirmed", False)
        db.commit()
    monkeypatch.setattr(config, "FEES_COMMERCIAL_CONFIRMED", False)
    seed_module.seed(quiet=True)
    with session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is False
        site_settings.put(db, "fees_commercial_confirmed", True)       # leave the approved sheet as the seed left it
        db.commit()


# ---- public pages -----------------------------------------------------------------------
def test_commercial_page_lists_the_compliance_points_and_the_service(client):
    page = client.get("/commercial")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    text = page.text
    for item in COMPLIANCE + SERVICE:
        assert item in text, item
    assert "Scotland" in text and "Northern Ireland" in text
    assert "assured shorthold" not in text and "rent-rise cap" not in text and "on the rent and the deposit" not in text
    assert 'name="kind" value="commercial_appraisal"' in text and 'data-subject="Commercial appraisal request"' in text
    assert 'name="website"' in text                                   # the same honeypot as the other forms
    for field in ("business_name", "property_use", "size_sqft", "lease_status", "asking_rent", "postcode", "address", "name", "phone", "email"):
        assert f'name="{field}"' in text, field
    assert '"@type": "FAQPage"' in text and '"@type": "BreadcrumbList"' in text
    assert f'<link rel="canonical" href="{config.BASE_URL}/commercial">' in text
    assert "The head office team" in text                             # no area: head office is the contact
    assert "The head office team will visit if you would like" in text  # a sentence start, not a lower-cased token
    # linked from the home page services list, the footer and the area hub
    assert 'href="/commercial"' in client.get("/").text
    assert 'href="/areas/somerset/commercial"' in client.get("/areas/somerset").text
    assert client.get("/commercial.html").status_code == 301


def test_area_commercial_page_and_404(client, admin_client):
    page = client.get("/areas/somerset/commercial")
    assert page.status_code == 200
    text = page.text
    assert "Commercial property" in text and "Somerset" in text
    for item in COMPLIANCE:
        assert item in text, item
    assert "Scotland" in text and "Northern Ireland" in text
    assert f'<link rel="canonical" href="{config.BASE_URL}/areas/somerset/commercial">' in text
    assert "Commercial lettings" in text and 'href="/areas/somerset/short-lets"' in text
    # Martin is not cleared, so the page offers head office, not him
    assert "The head office team" in text and "WhatsApp the head office team" in text
    assert "WhatsApp Martin" not in text
    assert client.get("/areas/nowhere/commercial").status_code == 404
    assert client.get("/areas/swansea/commercial").status_code == 200
    _set_commercial(admin_client, True)
    try:
        text = client.get("/areas/somerset/commercial").text
        assert "WhatsApp Martin" in text and "Martin Howard" in text and "Martin is cleared to take commercial instructions" in text
        assert "Martin will visit if you would like" in text
        # a partner's first name is shown as stored: no filter lower-cases the rest of McKenzie or Jean-Paul
        with session() as db:
            martin = _martin(db)
            martin.first_name = "McKenzie"
            db.add(martin)
            db.commit()
        try:
            text = client.get("/areas/somerset/commercial").text
            assert "McKenzie is cleared to take commercial instructions" in text and "McKenzie will visit" in text
            assert "Mckenzie" not in text
        finally:
            with session() as db:
                martin = _martin(db)
                martin.first_name = "Martin"
                db.add(martin)
                db.commit()
    finally:
        _set_commercial(admin_client, False)


def test_sitemap_and_areas_js_carry_the_commercial_pages(client):
    sitemap = client.get("/sitemap.xml").text
    assert f"<loc>{config.BASE_URL}/commercial</loc>" in sitemap
    assert f"<loc>{config.BASE_URL}/areas/somerset/commercial</loc>" in sitemap
    assert f"<loc>{config.BASE_URL}/areas/swansea/commercial</loc>" in sitemap
    assert sitemap.count(f"<loc>{config.BASE_URL}/commercial</loc>") == 1
    js = client.get("/areas.js").text
    assert '"commercial": "/areas/somerset/commercial"' in js
    by_slug = {a["slug"]: a for a in client.get("/api/areas").json()}
    assert by_slug["somerset"]["pages"] == {"hub": "/areas/somerset", "lettings": "/areas/somerset/lettings",
                                            "short_lets": "/areas/somerset/short-lets", "commercial": "/areas/somerset/commercial"}


def test_new_copy_has_no_exclamation_marks_or_em_dashes(client, admin_client, martin_client):
    pages = [client.get("/commercial").text, client.get("/areas/somerset/commercial").text,
             Path(config.CONTENT_DIR, "resources", "commercial-lettings.html").read_text(encoding="utf-8"),
             kit.commercial_checklist_text()]
    with session() as db:
        res = db.query(Resource).filter(Resource.title == "Commercial lettings: what is different").one()
        pages.append(res.body + res.description)
        rid = res.id
    pages.append(martin_client.get(f"/partners/resources/{rid}").text)
    for text in pages:
        body = _visible(text)
        assert "!" not in body
        assert chr(0x2014) not in body          # an em dash, spelt so the test file carries none itself
        assert "[CONFIRM" not in body           # the editorial marker belongs to the admin fee sheet only


# ---- routing: leads -----------------------------------------------------------------
def test_commercial_lead_routes_to_head_office_until_martin_is_cleared(client, admin_client, monkeypatch):
    sent: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(mail, "configured", lambda: True)       # the lead notifier only composes a message when SMTP is set
    monkeypatch.setattr(mail, "send", lambda subject, body, to: sent.append((subject, body, to)) or True)
    with session() as db:
        somerset = _somerset(db)
        somerset_id, martin_id = somerset.id, somerset.partner_id
        assert not _martin(db).commercial_enabled
    r = client.post("/api/lead", json=_lead())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["area"]["slug"] == "somerset"
    with session() as db:
        lead = db.get(Lead, body["id"])
        assert lead.kind == "commercial_appraisal" and lead.area_id == somerset_id and lead.partner_id is None
        assert lead.postcode == "TA1" and lead.name == "Carla Commercial"
        payload = lead.payload
        assert payload["business_name"] == "Castle Street Bakery Ltd" and payload["property_use"] == "retail"
        assert payload["size_sqft"] == 1200 and payload["lease_status"] == "vacant"
        assert payload["asking_rent"] == 18000 and payload["asking_rent_as_typed"] == "£18,000 a year"
        assert routing.is_commercial_lead(lead)
    subject, text, to = sent[-1]
    assert subject == "[HNH lead] Commercial appraisal request (TA1)"
    assert "commercial" in subject.lower() and "Carla" not in subject and "Bakery" not in subject
    assert to == [config.LEAD_NOTIFY_TO]                                   # Martin is not copied in
    assert "Routed to: head office" in text and mail.log_label(subject) == "[HNH lead] Commercial appraisal request"
    hq_lead_id = body["id"]

    # a residential lead at the same postcode still goes to Martin
    r = client.post("/api/lead", json={"kind": "rent_review", "name": "Rita Residential", "phone": "07700 900557",
                                       "postcode": "TA1 1AA", "address": "5 Castle Street, Taunton"})
    assert r.status_code == 200
    with session() as db:
        lead = db.get(Lead, r.json()["id"])
        assert lead.kind == "rent_review" and lead.area_id == somerset_id and lead.partner_id == martin_id
    assert sent[-1][0].startswith("[HNH lead] Free rent review request: Rita Residential") and sent[-1][2] == [config.LEAD_NOTIFY_TO, MARTIN]

    _set_commercial(admin_client, True)
    try:
        with session() as db:
            entry = (db.query(AuditLog).filter(AuditLog.target == f"partner:{martin_id}", AuditLog.action == "partner.update")
                     .order_by(AuditLog.id.desc()).first())
            assert entry is not None and "commercial instructions on (was off)" in entry.detail
        assert admin_client.get("/admin/partners").text.count("commercial") >= 1
        r = client.post("/api/lead", json=_lead(business_name="Second Bakery Ltd"))
        assert r.status_code == 200
        with session() as db:
            lead = db.get(Lead, r.json()["id"])
            assert lead.area_id == somerset_id and lead.partner_id == martin_id
        assert sent[-1][2] == [config.LEAD_NOTIFY_TO, MARTIN] and sent[-1][0] == "[HNH lead] Commercial appraisal request (TA1)"
        # head office hands the earlier lead over by reassigning the area: the rule runs again, now to Martin
        r = admin_client.post(f"/admin/leads/{hq_lead_id}", data={"status": "", "notes": "", "area_id": str(somerset_id)})
        assert r.status_code == 303 and "ok=" in r.headers["location"]
        with session() as db:
            assert db.get(Lead, hq_lead_id).partner_id == martin_id
    finally:
        _set_commercial(admin_client, False)
    # once the clearance is withdrawn a reassignment sends it back to head office
    r = admin_client.post(f"/admin/leads/{hq_lead_id}", data={"status": "", "notes": "", "area_id": str(somerset_id)})
    assert r.status_code == 303
    with session() as db:
        assert db.get(Lead, hq_lead_id).partner_id is None
    # the admin lead list knows the kind
    page = admin_client.get("/admin/leads", params={"kind": "commercial_appraisal"})
    assert page.status_code == 200 and "Castle Street Bakery Ltd" not in page.text and "Carla Commercial" in page.text


def test_commercial_lead_validation_and_honeypot(client):
    with session() as db:
        before = db.query(Lead).count()
    assert client.post("/api/lead", json=_lead(property_use="castle")).status_code == 422
    assert client.post("/api/lead", json=_lead(lease_status="haunted")).status_code == 422
    assert client.post("/api/lead", json=_lead(size_sqft="big")).status_code == 422
    assert client.post("/api/lead", json=_lead(asking_rent="a lot")).status_code == 422
    assert client.post("/api/lead", json=_lead(name="")).status_code == 422
    assert client.post("/api/lead", json=_lead(website="bot")).json() == {"ok": True}
    with session() as db:
        assert db.query(Lead).count() == before
    # optional fields may be blank, and the lease status accepts the spelling the form sends
    r = client.post("/api/lead", json=_lead(size_sqft="", asking_rent="", business_name="", lease_status="Lease ending"))
    assert r.status_code == 200
    with session() as db:
        payload = db.get(Lead, r.json()["id"]).payload
        assert payload["size_sqft"] is None and payload["asking_rent"] is None and payload["lease_status"] == "lease_ending"
        assert "asking_rent_as_typed" not in payload
    for r in (client.post("/api/lead", json=_lead(property_use="castle")), client.post("/api/lead", json=_lead(size_sqft="big"))):
        assert "!" not in r.text


# ---- routing: instructions through the Partner API ---------------------------------------
def test_commercial_instruction_routes_like_the_lead(client, admin_client, sender_token, monkeypatch):
    sent: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(mail, "send", lambda subject, body, to: sent.append((subject, body, to)) or True)
    api = _api(sender_token)
    with session() as db:
        somerset = _somerset(db)
        somerset_id, martin_id = somerset.id, somerset.partner_id
    r = api.post("/api/partner/properties", json=_instruction())
    assert r.status_code == 201, r.text
    record = r.json()["instruction"]
    assert record["service_line"] == "commercial" and record["area"]["slug"] == "somerset" and record["covered"]
    assert record["partner_first"] == "the head office team"      # the sender is told head office has it, not Martin
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "pp:commercial-1").one()
        assert inst.area_id == somerset_id and inst.partner_id is None and routing.is_commercial_instruction(inst)
        first_id = inst.id
        entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst.id}").one()
        assert "commercial" in entry.detail and "partner head office" in entry.detail
    assert sent[-1][0].startswith(f"[HNH property commercial] {record['ref']}, TA1 1AA") and sent[-1][2] == [config.LEAD_NOTIFY_TO]
    # a residential instruction at the same postcode goes to Martin as before
    r = api.post("/api/partner/properties", json=_instruction(external_ref="pp:residential-1", property={
        "address": "5 Castle Street, Taunton", "postcode": "TA1 1AA", "service_line": "residential"}))
    assert r.status_code == 201, r.text
    with session() as db:
        assert db.query(Instruction).filter(Instruction.external_ref == "pp:residential-1").one().partner_id == martin_id
    assert sent[-1][2] == [config.LEAD_NOTIFY_TO, MARTIN] and sent[-1][0].startswith("[HNH property] ")

    _set_commercial(admin_client, True)
    try:
        r = api.post("/api/partner/properties", json=_instruction(external_ref="pp:commercial-2"))
        assert r.status_code == 201, r.text
        assert r.json()["instruction"]["partner_first"] == "Martin"
        with session() as db:
            inst = db.query(Instruction).filter(Instruction.external_ref == "pp:commercial-2").one()
            assert inst.area_id == somerset_id and inst.partner_id == martin_id
        assert sent[-1][2] == [config.LEAD_NOTIFY_TO, MARTIN]
        # the admin reassignment runs the same rule
        r = admin_client.post(f"/admin/instructions/{first_id}", data={"status": "", "notes": "", "area_id": str(somerset_id)})
        assert r.status_code == 303 and "ok=" in r.headers["location"]
        with session() as db:
            assert db.get(Instruction, first_id).partner_id == martin_id
    finally:
        _set_commercial(admin_client, False)
    r = admin_client.post(f"/admin/instructions/{first_id}", data={"status": "", "notes": "", "area_id": str(somerset_id)})
    assert r.status_code == 303
    with session() as db:
        assert db.get(Instruction, first_id).partner_id is None
        ref_commercial = db.get(Instruction, first_id).ref
        ref_residential = db.query(Instruction).filter(Instruction.external_ref == "pp:residential-1").one().ref
    # Admin -> Instructions filters by service line
    page = admin_client.get("/admin/instructions", params={"service": "commercial"})
    assert page.status_code == 200 and ref_commercial in page.text and ref_residential not in page.text
    page = admin_client.get("/admin/instructions", params={"service": "residential"})
    assert ref_residential in page.text and ref_commercial not in page.text
    assert 'value="commercial" selected' in admin_client.get("/admin/instructions", params={"service": "commercial"}).text


# ---- the portal --------------------------------------------------------------------------
def test_portal_shows_commercial_items_only_to_a_cleared_partner(client, admin_client, martin_client, sender_token):
    with session() as db:
        commercial = db.query(Instruction).filter(Instruction.external_ref == "pp:commercial-1").one()
        residential = db.query(Instruction).filter(Instruction.external_ref == "pp:residential-1").one()
        c_id, c_ref, r_id, r_ref = commercial.id, commercial.ref, residential.id, residential.ref
        c_lead_id = db.query(Lead).filter(Lead.kind == "commercial_appraisal", Lead.name == "Carla Commercial").order_by(Lead.id).first().id
        r_lead_id = db.query(Lead).filter(Lead.name == "Rita Residential").one().id
        assert not _martin(db).commercial_enabled
    page = martin_client.get("/partners/instructions")
    assert page.status_code == 200 and r_ref in page.text and c_ref not in page.text
    assert martin_client.get(f"/partners/instructions/{c_id}").status_code == 404
    assert martin_client.get(f"/partners/instructions/{r_id}").status_code == 200
    assert martin_client.post(f"/partners/instructions/{c_id}", data={"status": "lost"}).status_code == 404
    leads = martin_client.get("/partners/leads")
    assert "Rita Residential" in leads.text and "Carla Commercial" not in leads.text
    assert martin_client.get(f"/partners/leads/{c_lead_id}").status_code == 404
    assert martin_client.get(f"/partners/leads/{r_lead_id}").status_code == 200
    assert "Carla Commercial" not in martin_client.get("/partners").text
    kit_page = martin_client.get("/partners/kit", params={"area": "somerset"})
    assert kit_page.status_code == 200 and "Commercial appraisal checklist" not in kit_page.text
    assert "Commercial appraisal checklist" not in martin_client.get("/partners/kit", params={"area": "somerset", "tab": "commercial"}).text

    _set_commercial(admin_client, True)
    try:
        page = martin_client.get("/partners/instructions")
        assert c_ref in page.text and r_ref in page.text
        detail = martin_client.get(f"/partners/instructions/{c_id}")
        assert detail.status_code == 200 and "Commercial" in detail.text and "Olive Owner" in detail.text
        for figure in FEE_FIGURES:
            assert figure not in detail.text, figure
        assert "Carla Commercial" in martin_client.get("/partners/leads").text
        assert martin_client.get(f"/partners/leads/{c_lead_id}").status_code == 200
        kit_page = martin_client.get("/partners/kit", params={"area": "somerset", "tab": "commercial"})
        assert kit_page.status_code == 200 and "Commercial appraisal checklist" in kit_page.text
        for item, _ in kit.COMMERCIAL_APPRAISAL_CHECKLIST:
            assert item in kit_page.text, item
        for figure in FEE_FIGURES:
            assert figure not in kit_page.text, figure
    finally:
        _set_commercial(admin_client, False)
    assert "Commercial appraisal checklist" not in martin_client.get("/partners/kit", params={"area": "somerset", "tab": "commercial"}).text
    # head office sees the tab regardless
    assert "Commercial appraisal checklist" in admin_client.get("/partners/kit", params={"area": "somerset", "tab": "commercial"}).text


# ---- the seeded training page --------------------------------------------------------
def test_training_page_is_seeded_by_revision(client, martin_client):
    title = "Commercial lettings: what is different"
    with session() as db:
        res = db.query(Resource).filter(Resource.title == title).one()
        assert res.kind == "page" and not res.is_placeholder and res.visibility == "all" and res.category == "Training"
        assert "Landlord and Tenant Act 1954" in res.body and "Code for Leasing Business Premises 2020" in res.body
        assert "Scotland" in res.body and "Northern Ireland" in res.body and "[CONFIRM" not in res.body
        assert "assured shorthold" not in res.body and "since 1 April 2023" in res.body
        assert "<h2>The short checklist for the visit</h2>" in res.body
        # the sheet is approved: commercial fees are published and always quoted plus VAT
        assert "quoted plus VAT" in res.body and "registers for VAT" not in res.body
        rid = res.id
        revisions = db.get(Setting, "seed_revisions").value
        assert revisions["resource:commercial-training"] == 3
    # every partner can read it, whether or not they are cleared for commercial work
    page = martin_client.get(f"/partners/resources/{rid}")
    assert page.status_code == 200 and "contracting out" in page.text and "No Right to Rent" in page.text
    assert "Scotland" in page.text and "Northern Ireland" in page.text
    assert title in martin_client.get("/partners/resources").text
    # a second boot changes nothing; head office deleting it is respected until the seed's revision is bumped
    seed_module.seed(quiet=True)
    with session() as db:
        assert db.query(Resource).filter(Resource.title == title).count() == 1
        db.delete(db.get(Resource, rid))
        db.commit()
    seed_module.seed(quiet=True)
    with session() as db:
        assert db.query(Resource).filter(Resource.title == title).count() == 0
        revisions = dict(db.get(Setting, "seed_revisions").value)
        revisions["resource:commercial-training"] = 0
        site_settings.put(db, "seed_revisions", revisions)
        db.commit()
    report = seed_module.seed(quiet=True)
    assert report["resources"] == 1
    with session() as db:
        res = db.query(Resource).filter(Resource.title == title).one()
        assert "<h2>The short checklist for the visit</h2>" in res.body
        assert db.get(Setting, "seed_revisions").value["resource:commercial-training"] == 3
        # a live row is refreshed in place when the revision rises, and head office's edits survive between bumps
        res.body = "<p>edited by head office</p>"
        db.add(res)
        db.commit()
    seed_module.seed(quiet=True)
    with session() as db:
        assert db.query(Resource).filter(Resource.title == title).one().body == "<p>edited by head office</p>"
        revisions = dict(db.get(Setting, "seed_revisions").value)
        revisions["resource:commercial-training"] = 0
        site_settings.put(db, "seed_revisions", revisions)
        db.commit()
    report = seed_module.seed(quiet=True)
    assert report["refreshed"] == 1
    with session() as db:
        res = db.query(Resource).filter(Resource.title == title).one()
        assert "Landlord and Tenant Act 1954" in res.body
        # the bump replaced head office's edit, and the audit log keeps the text it replaced, whole
        entry = (db.query(AuditLog).filter(AuditLog.action == "seed.resource_refresh", AuditLog.target == f"resource:{res.id}")
                 .order_by(AuditLog.id.desc()).first())
        assert entry is not None and entry.user_id is None
        assert entry.detail == f"{title}: replaced by seed revision 3. The text it replaced:\n<p>edited by head office</p>"
        db.delete(entry)
        db.commit()


# ---- migration 0004 --------------------------------------------------------------------
def test_migration_0004_adds_the_flag_with_a_false_default_and_removes_it(tmp_path: Path):
    eng = make_engine(f"sqlite:///{tmp_path / 'commercial.db'}")
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "0003_instructions")
    assert "commercial_enabled" not in {c["name"] for c in inspect(eng).get_columns("partners")}
    from sqlalchemy import text
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO partners (name, first_name, email, phone, whatsapp, whatsapp_display, bio, photo_path, "
                          "tier, status, units_managed, notes, created_at) VALUES ('Pat Partner', '', 'pat@example.test', '', "
                          "'', '', '', '', 'area', 'active', 0, '', '2026-01-01 00:00:00')"))
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "0004_commercial")
    assert migrations.current_revision(eng) == "0004_commercial"
    column = next(c for c in inspect(eng).get_columns("partners") if c["name"] == "commercial_enabled")
    assert not column["nullable"]
    with eng.connect() as conn:
        assert conn.execute(text("SELECT commercial_enabled FROM partners")).scalar() in (0, False)
    with eng.begin() as conn:
        command.downgrade(migrations.alembic_config(conn), "0003_instructions")
    assert "commercial_enabled" not in {c["name"] for c in inspect(eng).get_columns("partners")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM partners")).scalar() == "Pat Partner"
    eng.dispose()
