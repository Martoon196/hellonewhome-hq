"""End-to-end tests for the public site, the lead API, the admin panel and the partner portal."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.models import Area, AuditLog, Lead, Partner, Resource, User
from app.services.postcodes import locate, normalise, region_for
from tests.conftest import ADMIN_EMAIL, TEMP_DATA_DIR, TEMP_UPLOAD_DIR, login, make_partner, session

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n%test\n" + b"0" * 128


# ---- helpers ---------------------------------------------------------------
def areas_js(client) -> list[dict]:
    r = client.get("/areas.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    prefix = "window.HNH_AREAS = "
    assert r.text.startswith(prefix)
    data = json.loads(r.text[len(prefix):].rstrip().rstrip(";"))
    assert isinstance(data, list)
    return data


def sitemap_urls(client) -> list[str]:
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text for loc in root.findall("sm:url/sm:loc", ns)]


def post_lead(client, **fields) -> dict:
    r = client.post("/api/lead", json=fields)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    return body


def flash_of(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def area_form(**overrides) -> dict:
    base = {"name": "", "slug": "", "region": "england", "role": "partner", "status": "coming", "partner_id": "",
            "covers": "", "sort": "600", "postcodes": "", "places": "", "demand": "", "licensing": "", "shortlet": "",
            "rent_avg": "", "rent_change": "", "rent_note": "", "phone": "", "whatsapp": "", "whatsapp_display": "",
            "content": "", "action": "save"}
    base.update(overrides)
    return base


# ---- environment -----------------------------------------------------------
def test_environment_is_isolated():
    assert Path(TEMP_DATA_DIR).is_dir() and config.DATA_DIR == Path(TEMP_DATA_DIR)
    assert Path(TEMP_UPLOAD_DIR).is_dir() and config.UPLOAD_DIR == Path(TEMP_UPLOAD_DIR)
    assert config.DATABASE_URL.startswith("sqlite:///") and TEMP_DATA_DIR in config.DATABASE_URL
    assert config.SMTP_HOST == ""


def test_seed_revision_refreshes_an_existing_area_and_its_placeholder_partner(client, monkeypatch):
    """A later deploy can widen an area or attach the real partner by bumping the row's "revision"; the same
    revision is applied once, so head-office edits made afterwards survive every boot."""
    from app import seed as seed_module
    from app.services import content, settings as site_settings
    rows = content.areas_seed()
    original = next(r for r in rows if r["slug"] == "durham")
    bumped = dict(original, revision=int(original.get("revision") or 0) + 1, name="County Durham", status="active",
                  covers="All of County Durham", postcodes=["DH", "DL"],
                  partner="Test Person", partner_first="Test", partner_email="Test.Person@example.com",
                  partner_phone="07700 900123", whatsapp="447700900123", whatsapp_display="07700 900123",
                  partner_status="active", partner_bio="A real bio.")
    monkeypatch.setattr(content, "areas_seed", lambda: [bumped if r["slug"] == "durham" else r for r in rows])
    area_keys = ("name", "status", "covers", "postcodes", "content")
    partner_keys = ("name", "first_name", "email", "phone", "status", "tier", "whatsapp", "whatsapp_display", "bio")
    with session() as db:
        before = db.query(Area).filter(Area.slug == "durham").one()
        partner_id = before.partner_id
        assert before.partner.name.endswith("(to be appointed)") and before.status == "coming"
        saved_area = {k: getattr(before, k) for k in area_keys}
        saved_partner = {k: getattr(before.partner, k) for k in partner_keys}
        saved_revisions = site_settings.get(db, "seed_revisions")
    try:
        assert seed_module.seed(quiet=True)["refreshed"] == 1
        with session() as db:
            area = db.query(Area).filter(Area.slug == "durham").one()
            assert area.name == "County Durham" and area.status == "active" and area.postcodes == ["DH", "DL"]
            assert area.partner_id == partner_id and area.partner.name == "Test Person"
            assert area.partner.email == "test.person@example.com" and area.partner.status == "active"
            assert area.partner.whatsapp == "447700900123" and area.partner.bio == "A real bio."
            assert site_settings.get(db, "seed_revisions")["durham"] == bumped["revision"]
            area.name = "Durham (edited by head office)"
            db.add(area)
            db.commit()
        assert seed_module.seed(quiet=True)["refreshed"] == 0
        with session() as db:
            assert db.query(Area).filter(Area.slug == "durham").one().name == "Durham (edited by head office)"
    finally:
        with session() as db:
            area = db.query(Area).filter(Area.slug == "durham").one()
            for k, v in saved_area.items():
                setattr(area, k, v)
            for k, v in saved_partner.items():
                setattr(area.partner, k, v)
            db.add(area)
            db.add(area.partner)
            site_settings.put(db, "seed_revisions", saved_revisions or {})
            db.commit()


def test_seed_created_admin_and_areas(client):
    with session() as db:
        admin = db.query(User).filter(User.email == ADMIN_EMAIL).one()
        assert admin.role == "admin" and admin.is_active and not admin.must_change_password
        slugs = {a.slug: a for a in db.query(Area).all()}
    assert {"swansea", "durham", "andover", "london"} <= set(slugs)
    assert slugs["swansea"].role == "hq" and slugs["swansea"].status == "active"
    assert slugs["durham"].status == "coming" and slugs["durham"].partner_id is not None


# ---- public site -----------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/", "/landlords", "/tenants", "/to-let", "/valuation", "/serviced-accommodation", "/areas", "/contact",
    "/partners-programme", "/fees", "/about", "/compliance", "/privacy", "/news", "/hmo-management",
    "/areas/swansea", "/areas/swansea/lettings", "/areas/swansea/short-lets", "/areas/durham",
])
def test_public_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>" in r.text


def test_public_crawl_files_and_health(client):
    assert client.get("/healthz").json() == {"ok": True}
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /admin" in robots.text and "Disallow: /partners" in robots.text
    assert f"Sitemap: {config.BASE_URL}/sitemap.xml" in robots.text
    assert client.get("/no-such-page").status_code == 404
    assert client.get("/areas/no-such-area").status_code == 404
    # old static-site links are redirected, not broken
    r = client.get("/area-swansea.html")
    assert r.status_code == 301 and r.headers["location"] == "/areas/swansea"


def test_sitemap_lists_area_urls(client, partner_a):
    urls = sitemap_urls(client)
    base = config.BASE_URL
    assert f"{base}/" in urls or base in urls
    for slug in ("swansea", "durham", partner_a.slug):
        assert f"{base}/areas/{slug}" in urls
        assert f"{base}/areas/{slug}/lettings" in urls
        assert f"{base}/areas/{slug}/short-lets" in urls
    assert len(urls) == len(set(urls))


def test_areas_js_parses_and_matches_api(client, partner_a):
    data = areas_js(client)
    by_slug = {a["slug"]: a for a in data}
    assert "swansea" in by_slug and by_slug["swansea"]["role"] == "hq"
    assert by_slug[partner_a.slug]["partner"] == partner_a.name
    assert by_slug[partner_a.slug]["postcodes"] == partner_a.postcodes
    assert by_slug[partner_a.slug]["whatsapp"] == "447700900001"
    for item in data:
        assert {"slug", "name", "region", "status", "postcodes", "whatsapp", "whatsapp_display"} <= set(item)
        assert item["status"] in ("active", "coming")
    api = client.get("/api/areas")
    assert api.status_code == 200
    assert api.json() == data


# ---- postcode routing ------------------------------------------------------
def test_normalise_and_region():
    assert normalise("dh1 3le") == "DH1"
    assert normalise("SW1A 1AA") == "SW1A"
    assert normalise("G1 1AA") == "G1"
    assert normalise("SE1") == "SE1"
    assert normalise("nonsense") is None
    assert normalise("") is None
    assert region_for("SA") == "wales"
    assert region_for("EH") == "scotland"
    assert region_for("BT") == "northern-ireland"
    assert region_for("DH") == "england"


def test_locate_routes_to_areas(db, partner_a, partner_b):
    loc = locate(db, "sa1 1aa")
    assert loc.covered and loc.area.slug == "swansea" and loc.region == "wales" and loc.outward == "SA1"
    loc = locate(db, "TS2 1AB")
    assert loc.area.id == partner_a.area_id and loc.region == "england"
    loc = locate(db, "yo24 4ab")
    assert loc.area.id == partner_b.area_id
    # a whole postcode area (London lists "SE") covers every district in it
    assert locate(db, "SE5 8AA").area.slug == "london"
    # "coming" areas are included by default and excluded on request
    assert locate(db, "DH1 3LE").area.slug == "durham"
    assert locate(db, "DH1 3LE", include_coming=False).area is None
    assert locate(db, "not a postcode") is None


def test_locate_exact_district_beats_whole_area(db):
    """SE1 listed on its own area must win over London's whole-area SE entry."""
    with session() as s:
        s.add(Area(slug="southwark-test", name="Southwark test", region="england", role="partner", status="active",
                   postcodes=["SE1"], sort=5))
        s.commit()
    try:
        assert locate(db, "SE1 7PB").area.slug == "southwark-test"
        assert locate(db, "SE5 8AA").area.slug == "london"
    finally:
        with session() as s:
            s.query(Area).filter(Area.slug == "southwark-test").delete()
            s.commit()


def test_locate_scotland_and_northern_ireland(db):
    loc = locate(db, "EH1 1YZ")
    assert loc.region == "scotland" and loc.area is None and not loc.covered
    loc = locate(db, "G12 8QQ")
    assert loc.region == "scotland" and loc.area_code == "G"
    loc = locate(db, "BT1 5GS")
    assert loc.region == "northern-ireland" and loc.area is None


def test_api_locate(client, partner_a):
    r = client.get("/api/locate", params={"postcode": "TS1 2AB"})
    body = r.json()
    assert body["ok"] and body["covered"] and body["area"]["slug"] == partner_a.slug
    assert body["area"]["whatsapp"] == "447700900001"
    r = client.get("/api/locate", params={"postcode": "EH1 1YZ"})
    assert r.json() == {"ok": True, "outward": "EH1", "region": "scotland", "covered": False, "area": None}
    assert client.get("/api/locate", params={"postcode": "zzz"}).json()["ok"] is False


# ---- lead API --------------------------------------------------------------
def test_api_lead_routes_to_area_and_partner(client, partner_a):
    body = post_lead(client, kind="rent_review", name="Lead Routing Test", phone="07700 900999",
                     email="landlord@example.test", postcode="ts1 2ab", _page="/valuation", _subject="ignored")
    assert body["area"]["slug"] == partner_a.slug and body["area"]["partner"] == partner_a.name
    with session() as db:
        lead = db.get(Lead, body["id"])
        assert lead.kind == "rent_review" and lead.status == "new"
        assert lead.area_id == partner_a.area_id and lead.partner_id == partner_a.partner_id
        assert lead.postcode == "TS1" and lead.region == "england"
        assert lead.source_page == "/valuation"
        assert "_page" not in lead.payload and "_subject" not in lead.payload
        assert lead.payload["phone"] == "07700 900999"


def test_api_lead_head_office_area_has_no_partner(client):
    body = post_lead(client, name="Swansea Lead", postcode="SA1 1AA", kind="contact")
    assert body["area"]["slug"] == "swansea"
    with session() as db:
        lead = db.get(Lead, body["id"])
        assert lead.area.role == "hq" and lead.partner_id is None and lead.region == "wales"


def test_api_lead_form_post_uses_subject_and_address(client, partner_a):
    r = client.post("/api/lead", data={"_subject": "Short-let appraisal request", "name": "Form Lead",
                                       "address": "12 High Street, Middlesbrough TS3 6AB", "_page": "/serviced-accommodation"})
    assert r.status_code == 200 and r.json()["ok"] is True
    with session() as db:
        lead = db.get(Lead, r.json()["id"])
        assert lead.kind == "sa_appraisal"
        assert lead.postcode == "TS3" and lead.area_id == partner_a.area_id


def test_api_lead_uncovered_postcode_falls_back_to_area_slug(client, partner_b):
    body = post_lead(client, name="Uncovered Lead", postcode="EH1 1YZ")
    assert body["area"] is None
    with session() as db:
        lead = db.get(Lead, body["id"])
        assert lead.area_id is None and lead.region == "scotland" and lead.postcode == "EH1"
    # no postcode at all but the page carried the area slug: routed to that area
    body = post_lead(client, name="Slug Lead", area=partner_b.slug)
    assert body["area"]["slug"] == partner_b.slug
    with session() as db:
        assert db.get(Lead, body["id"]).partner_id == partner_b.partner_id


def test_api_lead_honeypot_ignored(client):
    with session() as db:
        before = db.query(Lead).count()
    r = client.post("/api/lead", json={"name": "Spam Bot", "postcode": "TS1 2AB", "website": "http://spam.example"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post("/api/lead", data={"name": "Spam Bot", "postcode": "TS1 2AB", "website": "x"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with session() as db:
        assert db.query(Lead).count() == before


def test_api_lead_missing_name_422(client):
    with session() as db:
        before = db.query(Lead).count()
    r = client.post("/api/lead", json={"postcode": "TS1 2AB", "phone": "07700 900000"})
    assert r.status_code == 422
    assert r.json() == {"ok": False, "error": "Name is required"}
    assert client.post("/api/lead", json=["not", "a", "dict"]).status_code == 400
    with session() as db:
        assert db.query(Lead).count() == before


# ---- auth guards -----------------------------------------------------------
def test_unauthenticated_admin_and_portal_redirect_to_login(client):
    r = client.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login?next=/admin"
    r = client.get("/admin/leads")
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")
    r = client.get("/partners")
    assert r.status_code == 303 and r.headers["location"] == "/partners/login?next=/partners"
    r = client.get("/partners/leads")
    assert r.status_code == 303 and r.headers["location"].startswith("/partners/login")
    r = client.get("/files/1")
    assert r.status_code == 303 and r.headers["location"].startswith("/partners/login")
    assert client.get("/admin/login").status_code == 200
    assert client.get("/partners/login").status_code == 200


def test_partner_cannot_open_admin(partner_a_client):
    assert partner_a_client.get("/admin").status_code == 403
    assert partner_a_client.get("/admin/areas").status_code == 403
    assert partner_a_client.post("/admin/areas/new", data=area_form(name="Nope")).status_code == 403
    assert partner_a_client.get("/partners").status_code == 200


def test_admin_can_use_both_panels(admin_client):
    assert admin_client.get("/admin").status_code == 200
    assert admin_client.get("/partners").status_code == 200
    r = admin_client.get("/admin/login")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_bad_credentials_rejected(client, partner_a):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": "wrong-password"})
    assert r.status_code == 401 and "not recognised" in r.text
    # a partner login is not an admin login
    r = c.post("/admin/login", data={"email": partner_a.email, "password": partner_a.password})
    assert r.status_code == 401
    r = c.post("/partners/login", data={"email": partner_a.email, "password": "wrong-password"})
    assert r.status_code == 401
    assert c.get("/partners").status_code == 303


@pytest.mark.parametrize("bad_next", ["//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)"])
def test_login_next_cannot_redirect_off_site(client, admin_client, partner_a, partner_a_client, bad_next):
    # already logged in: the login page bounces to the requested page, which must stay on this site
    r = admin_client.get("/admin/login", params={"next": bad_next})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    r = partner_a_client.get("/partners/login", params={"next": bad_next})
    assert r.status_code == 303 and r.headers["location"] == "/partners"
    # the hidden next field posted with the credentials gets the same treatment
    c = TestClient(app, follow_redirects=False)
    r = c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": "test-admin-pass", "next": bad_next})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    r = c.post("/partners/login", data={"email": partner_a.email, "password": partner_a.password, "next": bad_next})
    assert r.status_code == 303 and r.headers["location"] == "/partners"
    # and the form never echoes the hostile value for the next submit
    assert bad_next not in client.get("/partners/login", params={"next": bad_next}).text
    # a local path still works
    r = partner_a_client.get("/partners/login", params={"next": "/partners/kit"})
    assert r.status_code == 303 and r.headers["location"] == "/partners/kit"


def test_logout_clears_session(client, partner_b):
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", partner_b.email, partner_b.password)
    assert c.get("/partners").status_code == 200
    r = c.post("/partners/logout")
    assert r.status_code == 303 and r.headers["location"] == "/partners/login"
    assert c.get("/partners").status_code == 303


# ---- admin: areas ----------------------------------------------------------
def test_admin_creates_area_with_postcodes(admin_client, partner_a):
    r = admin_client.post("/admin/areas/new", data=area_form(
        name="Harrogate", covers="Harrogate, Knaresborough and Ripon", status="coming",
        postcodes="hg1, hg2\nHG3 hg1;hg4", places="Harrogate\nKnaresborough\n\nRipon",
    ))
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert re.match(r"^/admin/areas/\d+\?ok=", location)
    assert flash_of(location)["ok"] == "Area saved."
    area_id = int(location.split("/")[3].split("?")[0])
    with session() as db:
        area = db.get(Area, area_id)
        assert area.slug == "harrogate" and area.name == "Harrogate"
        assert area.postcodes == ["HG1", "HG2", "HG3", "HG4"]
        assert area.places == ["Harrogate", "Knaresborough", "Ripon"]
        assert area.status == "coming" and area.region == "england" and area.partner_id is None
        entry = db.query(AuditLog).filter(AuditLog.target == f"area:{area_id}", AuditLog.action == "area.create").one()
        assert "Harrogate" in entry.detail
    page = admin_client.get(f"/admin/areas/{area_id}")
    assert page.status_code == 200 and "HG1, HG2, HG3, HG4" in page.text
    assert "Harrogate" in admin_client.get("/admin/areas").text


def test_admin_rejects_postcode_overlap_and_bad_codes(admin_client, partner_a):
    with session() as db:
        before = db.query(Area).count()
    r = admin_client.post("/admin/areas/new", data=area_form(name="Teesside overlap", postcodes="TS9, TS1"))
    assert r.status_code == 400
    assert "Postcode clash" in r.text and f"TS1 is already in {partner_a.area_name}" in r.text
    r = admin_client.post("/admin/areas/new", data=area_form(name="Bad codes", postcodes="ABC123, 1A"))
    assert r.status_code == 400 and "Not valid postcode districts" in r.text
    r = admin_client.post("/admin/areas/new", data=area_form(name="Slug clash", slug=partner_a.slug, postcodes="TS9"))
    assert r.status_code == 400 and "already used" in r.text
    with session() as db:
        assert db.query(Area).count() == before


def test_activating_area_publishes_it(admin_client, client):
    r = admin_client.post("/admin/areas/new", data=area_form(name="Scarborough", status="paused", postcodes="YO11, YO12"))
    assert r.status_code == 303, r.text
    area_id = int(r.headers["location"].split("/")[3].split("?")[0])

    assert "scarborough" not in {a["slug"] for a in areas_js(client)}
    assert f"{config.BASE_URL}/areas/scarborough" not in sitemap_urls(client)
    paused = client.get("/areas/scarborough")           # reachable for anyone holding the link, but not indexed
    assert paused.status_code == 200 and 'name="robots" content="noindex"' in paused.text

    r = admin_client.post(f"/admin/areas/{area_id}/status", data={"status": "active"})
    assert r.status_code == 303 and flash_of(r.headers["location"])["ok"] == "Scarborough is now active."
    assert "scarborough" in {a["slug"] for a in areas_js(client)}
    urls = sitemap_urls(client)
    assert f"{config.BASE_URL}/areas/scarborough" in urls and f"{config.BASE_URL}/areas/scarborough/lettings" in urls
    live = client.get("/areas/scarborough")
    assert live.status_code == 200 and 'content="noindex"' not in live.text
    assert client.get("/api/locate", params={"postcode": "YO12 4AB"}).json()["area"]["slug"] == "scarborough"

    r = admin_client.post(f"/admin/areas/{area_id}/status", data={"status": "hidden"})
    assert r.status_code == 303
    assert "scarborough" not in {a["slug"] for a in areas_js(client)}
    assert client.get("/areas/scarborough").status_code == 404
    with session() as db:
        actions = [a.action for a in db.query(AuditLog).filter(AuditLog.target == f"area:{area_id}").all()]
    assert actions.count("area.status") == 2


# ---- portal: leads ---------------------------------------------------------
def test_partner_sees_own_leads_only(client, partner_a, partner_b, partner_a_client, partner_b_client):
    lead_a = post_lead(client, name="Lead Alpha Teesside", postcode="TS2 1AB", phone="07700 900111")["id"]
    lead_b = post_lead(client, name="Lead Bravo York", postcode="YO10 5DD", phone="07700 900222")["id"]

    page = partner_a_client.get("/partners/leads")
    assert page.status_code == 200
    assert "Lead Alpha Teesside" in page.text and "Lead Bravo York" not in page.text
    assert partner_a_client.get(f"/partners/leads/{lead_a}").status_code == 200
    assert partner_a_client.get(f"/partners/leads/{lead_b}").status_code == 404
    assert partner_b_client.get(f"/partners/leads/{lead_a}").status_code == 404
    assert partner_b_client.get(f"/partners/leads/{lead_b}").status_code == 200
    dash = partner_a_client.get("/partners")
    assert dash.status_code == 200 and "Lead Alpha Teesside" in dash.text and "Lead Bravo York" not in dash.text

    # partner B cannot touch A's lead, even blind
    r = partner_b_client.post(f"/partners/leads/{lead_a}", data={"status": "lost"})
    assert r.status_code == 404
    with session() as db:
        assert db.get(Lead, lead_a).status == "new"

    # partner A updates their own lead
    r = partner_a_client.post(f"/partners/leads/{lead_a}", data={"status": "contacted", "note": "Called, viewing booked"})
    assert r.status_code == 303 and flash_of(r.headers["location"])["ok"] == "Saved."
    with session() as db:
        lead = db.get(Lead, lead_a)
        assert lead.status == "contacted"
        assert "Called, viewing booked" in lead.notes and "Alice" in lead.notes
        entry = db.query(AuditLog).filter(AuditLog.target == f"lead:{lead_a}", AuditLog.action == "lead_update").one()
        assert entry.user_id == partner_a.user_id and "status new to contacted" in entry.detail
    detail = partner_a_client.get(f"/partners/leads/{lead_a}")
    assert detail.status_code == 200 and "wa.me/447700900111" in detail.text
    r = partner_a_client.post(f"/partners/leads/{lead_a}", data={"status": "bogus"})
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_admin_lead_detail_and_export(admin_client, client, partner_a):
    lead_id = post_lead(client, name="Export Me", postcode="TS3 9ZZ", phone="=cmd()")["id"]
    assert "Export Me" in admin_client.get("/admin/leads").text
    assert admin_client.get(f"/admin/leads/{lead_id}").status_code == 200
    r = admin_client.get("/admin/leads/export.csv", params={"q": "Export Me"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Export Me" in r.text and "'=cmd()" in r.text and r.text.startswith("﻿")


# ---- portal: profile -------------------------------------------------------
def test_partner_profile_normalises_whatsapp(partner_a, partner_a_client):
    form = {"first_name": "Alice", "name": "Alice Archer", "phone": "01642 000 000", "whatsapp": "07700 900123",
            "whatsapp_display": "", "bio": "Local since 2010."}
    r = partner_a_client.post("/partners/profile", data=form)
    assert r.status_code == 303 and flash_of(r.headers["location"])["ok"] == "Profile saved."
    with session() as db:
        p = db.get(Partner, partner_a.partner_id)
        assert p.whatsapp == "447700900123"
        assert p.whatsapp_display == "07700 900123"
        assert p.phone == "01642 000 000" and p.bio == "Local since 2010."
        assert db.query(AuditLog).filter(AuditLog.target == f"partner:{p.id}", AuditLog.action == "profile_update").count() == 1
    page = partner_a_client.get("/partners/profile")
    assert page.status_code == 200 and 'value="447700900123"' in page.text

    r = partner_a_client.post("/partners/profile", data=dict(form, whatsapp="+44 7700 900124", whatsapp_display="07700 900 124"))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        p = db.get(Partner, partner_a.partner_id)
        assert p.whatsapp == "447700900124" and p.whatsapp_display == "07700 900 124"

    r = partner_a_client.post("/partners/profile", data=dict(form, whatsapp="12"))
    assert r.status_code == 303 and "WhatsApp" in flash_of(r.headers["location"])["err"]
    r = partner_a_client.post("/partners/profile", data=dict(form, name=""))
    assert r.status_code == 303 and "err=" in r.headers["location"]
    with session() as db:
        assert db.get(Partner, partner_a.partner_id).whatsapp == "447700900124"
    # the public area page now shows the partner's number
    hub = partner_a_client.get(f"/areas/{partner_a.slug}")
    assert hub.status_code == 200 and "wa.me/447700900124" in hub.text


def test_partner_photo_upload_and_public_photo_route(client, partner_b, partner_b_client):
    form = {"first_name": "Ben", "name": "Ben Brook", "phone": "", "whatsapp": "07700 900002", "whatsapp_display": "", "bio": ""}
    r = partner_b_client.post("/partners/profile", data=form, files={"photo": ("me.png", PNG_BYTES, "image/png")})
    assert r.status_code == 303 and flash_of(r.headers["location"])["ok"] == "Profile saved."
    with session() as db:
        rel = db.get(Partner, partner_b.partner_id).photo_path
    assert rel == f"photos/{partner_b.partner_id}.png"
    assert (config.UPLOAD_DIR / rel).read_bytes() == PNG_BYTES
    r = client.get(f"/uploads/{rel}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG_BYTES
    assert partner_b_client.get("/partners/profile/photo").status_code == 200
    assert f"/uploads/{rel}" in client.get(f"/areas/{partner_b.slug}").text
    # mismatched content, wrong type and path tricks are refused
    r = partner_b_client.post("/partners/profile", data=form, files={"photo": ("fake.png", b"not an image at all", "image/png")})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = partner_b_client.post("/partners/profile", data=form, files={"photo": ("script.svg", b"<svg/>", "image/svg+xml")})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert client.get("/uploads/photos/..%2Fhq.db").status_code == 404
    assert client.get(f"/uploads/photos/{partner_b.partner_id}.txt").status_code == 404
    assert client.get("/uploads/photos/does-not-exist.png").status_code == 404


# ---- resources -------------------------------------------------------------
def test_resource_visibility_by_area(admin_client, client, partner_a, partner_b, partner_a_client, partner_b_client):
    r = admin_client.post("/admin/resources/new", data={
        "title": "Middlesbrough price list", "description": "Local fee sheet", "kind": "file", "url": "", "body": "",
        "category": "Fees", "visibility": "area", "region": "", "area_id": str(partner_a.area_id), "sort": "10",
    }, files={"file": ("Price List.pdf", PDF_BYTES, "application/pdf")})
    assert r.status_code == 303, r.text
    rid = int(r.headers["location"].split("/")[3].split("?")[0])
    with session() as db:
        res = db.get(Resource, rid)
        assert res.file_path == f"resources/{rid}-Price-List.pdf" and not res.is_placeholder
        assert res.visibility == "area" and res.area_id == partner_a.area_id
    assert (config.UPLOAD_DIR / f"resources/{rid}-Price-List.pdf").read_bytes() == PDF_BYTES

    a_list = partner_a_client.get("/partners/resources")
    b_list = partner_b_client.get("/partners/resources")
    assert a_list.status_code == 200 and "Middlesbrough price list" in a_list.text and f"/partners/files/{rid}" in a_list.text
    assert b_list.status_code == 200 and "Middlesbrough price list" not in b_list.text

    for path in (f"/files/{rid}", f"/partners/files/{rid}"):
        ok = partner_a_client.get(path)
        assert ok.status_code == 200 and ok.content == PDF_BYTES
        assert 'filename="Price-List.pdf"' in ok.headers["content-disposition"]
        assert partner_b_client.get(path).status_code == 404
        assert admin_client.get(path).status_code == 200
        anon = client.get(path)
        assert anon.status_code == 303 and anon.headers["location"].startswith("/partners/login")
    assert partner_a_client.get("/files/999999").status_code == 404

    # a region-restricted item follows the area's region; a placeholder is never downloadable
    with session() as db:
        wales = Resource(title="Rent Smart Wales pack", kind="link", url="https://example.test/rsw", visibility="region", region="wales")
        england = Resource(title="England tenancy pack", kind="link", url="https://example.test/eng", visibility="region", region="england")
        db.add_all([wales, england])
        db.commit()
        placeholder = db.query(Resource).filter(Resource.is_placeholder.is_(True)).first()
    for c in (partner_a_client, partner_b_client):
        text = c.get("/partners/resources").text
        assert "England tenancy pack" in text and "Rent Smart Wales pack" not in text
    if placeholder is not None:
        assert partner_a_client.get(f"/files/{placeholder.id}").status_code == 404


def test_admin_resource_upload_rejects_bad_extension(admin_client, partner_a):
    r = admin_client.post("/admin/resources/new", data={
        "title": "Bad upload", "kind": "file", "category": "Fees", "visibility": "all", "sort": "10",
    }, files={"file": ("virus.exe", b"MZ....", "application/octet-stream")})
    assert r.status_code == 400 and "Files must be one of" in r.text
    with session() as db:
        assert db.query(Resource).filter(Resource.title == "Bad upload").count() == 0


# ---- marketing kit ---------------------------------------------------------
def test_kit_renders_for_partner_area(partner_a, partner_b, partner_a_client):
    r = partner_a_client.get("/partners/kit", params={"area": partner_a.slug})
    assert r.status_code == 200
    assert f"Marketing kit: {partner_a.area_name}" in r.text
    for tab in ("flyers", "fb", "graphics", "gbp", "letters", "scripts"):
        assert f'data-tab="{tab}"' in r.text
    assert f"/partners/kit/flyer/a5-landlord?area={partner_a.slug}" in r.text
    # the default area is the partner's own; another partner's area is invisible
    assert f"Marketing kit: {partner_a.area_name}" in partner_a_client.get("/partners/kit").text
    assert partner_a_client.get("/partners/kit", params={"area": partner_b.slug}).status_code == 404
    assert partner_a_client.get("/partners/kit", params={"area": "swansea"}).status_code == 404
    for tab in ("fb", "gbp", "letters", "scripts", "graphics"):
        page = partner_a_client.get("/partners/kit", params={"area": partner_a.slug, "tab": tab})
        assert page.status_code == 200 and f'data-tab="{tab}" class="is-active"' in page.text

    flyer = partner_a_client.get("/partners/kit/flyer/a5-landlord", params={"area": partner_a.slug})
    assert flyer.status_code == 200 and partner_a.area_name in flyer.text and "{{" not in flyer.text
    letter = partner_a_client.get("/partners/kit/letter/board-canvass-letter", params={"area": partner_a.slug})
    assert letter.status_code == 200 and partner_a.area_name in letter.text
    assert partner_a_client.get("/partners/kit/flyer/no-such-flyer", params={"area": partner_a.slug}).status_code == 404


def test_kit_graphic_svg(partner_a, partner_b, partner_a_client, partner_b_client):
    r = partner_a_client.get("/partners/kit/graphic/sq-capped-fee.svg", params={"area": partner_a.slug})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(r.content)
    assert root.tag.endswith("svg") and root.get("width") == "1080" and root.get("height") == "1080"
    assert "hello" in r.text and "new" in r.text and f"/areas/{partner_a.slug}" in r.text
    assert "{{" not in r.text
    story = partner_a_client.get("/partners/kit/graphic/st-tenant-find.svg", params={"area": partner_a.slug})
    assert story.status_code == 200 and ET.fromstring(story.content).get("height") == "1920"
    page = partner_a_client.get("/partners/kit/graphic/sq-capped-fee", params={"area": partner_a.slug})
    assert page.status_code == 200 and f"/partners/kit/graphic/sq-capped-fee.svg?area={partner_a.slug}" in page.text
    assert partner_a_client.get("/partners/kit/graphic/sq-capped-fee.svg", params={"area": partner_b.slug}).status_code == 404
    assert partner_b_client.get("/partners/kit/graphic/sq-capped-fee.svg", params={"area": partner_a.slug}).status_code == 404
    assert partner_a_client.get("/partners/kit/graphic/no-such.svg", params={"area": partner_a.slug}).status_code == 404


# ---- portal: my area -------------------------------------------------------
def test_partner_area_page_and_change_request(partner_a, partner_b, partner_a_client, partner_b_client):
    page = partner_a_client.get("/partners/area")
    assert page.status_code == 200 and partner_a.area_name in page.text and "TS1" in page.text
    assert partner_b.area_name not in page.text
    r = partner_a_client.post("/partners/area/request", data={"area_id": str(partner_a.area_id), "request": "Add TS4", "reason": "New estate"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        req = db.query(Lead).filter(Lead.kind == "partner_request", Lead.partner_id == partner_a.partner_id).one()
        assert req.area_id == partner_a.area_id and req.payload["request"] == "Add TS4"
    # change requests are head office's business: hidden from the partner lead list but visible on My area
    assert "Add TS4" in partner_a_client.get("/partners/area").text
    assert "Add TS4" not in partner_a_client.get("/partners/leads").text
    assert partner_a_client.get(f"/partners/leads/{req.id}").status_code == 404
    assert partner_b_client.post("/partners/area/request", data={"area_id": str(partner_a.area_id), "request": "Steal"}).status_code == 404


# ---- CSRF ------------------------------------------------------------------
def test_csrf_foreign_origin_rejected(client, partner_a, partner_a_client, admin_client):
    lead_id = post_lead(client, name="CSRF Target", postcode="TS1 4AB")["id"]
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/attack"}, {"Origin": "null"}):
        r = partner_a_client.post(f"/partners/leads/{lead_id}", data={"status": "lost"}, headers=headers)
        assert r.status_code == 403, headers
    r = admin_client.post("/admin/areas/new", data=area_form(name="Forged"), headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = partner_a_client.post("/partners/profile", data={"name": "Pwned"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    with session() as db:
        assert db.get(Lead, lead_id).status == "new"
        assert db.query(Area).filter(Area.name == "Forged").count() == 0
        assert db.get(Partner, partner_a.partner_id).name == "Alice Archer"
    # our own origin is fine, and GETs are never blocked
    r = partner_a_client.post(f"/partners/leads/{lead_id}", data={"status": "contacted"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    assert partner_a_client.get("/partners/leads", headers={"Origin": "https://evil.example"}).status_code == 200


# ---- password change -------------------------------------------------------
def test_first_login_must_change_password(client):
    p = make_partner(name="Cara Cole", first_name="Cara", email="cara@example.test", slug="whitby", area_name="Whitby",
                     postcodes=["YO21", "YO22"], whatsapp="", whatsapp_display="", sort=520,
                     must_change_password=True, password="temporary-pass-1")
    c = TestClient(app, follow_redirects=False)
    r = c.post("/partners/login", data={"email": p.email, "password": p.password, "next": "/partners/leads"})
    assert r.status_code == 303 and r.headers["location"] == "/partners/leads"
    for path in ("/partners", "/partners/leads", "/partners/kit", "/partners/profile"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1", path
    form = c.get("/partners/password", params={"first": "1"})
    assert form.status_code == 200 and "Set your password" in form.text
    assert "Temporary password" in form.text and 'name="current"' in form.text

    # the temporary password is always needed: a cookie alone cannot choose the new password
    r = c.post("/partners/password", data={"new": "brand-new-pass-99", "confirm": "brand-new-pass-99"})
    assert r.status_code == 400 and "temporary password is wrong" in r.text
    r = c.post("/partners/password", data={"current": "temporary-pass-1", "new": "short", "confirm": "short"})
    assert r.status_code == 400 and "at least 10 characters" in r.text
    r = c.post("/partners/password", data={"current": "temporary-pass-1", "new": "long-enough-pass", "confirm": "does-not-match"})
    assert r.status_code == 400
    r = c.post("/partners/password", data={"current": "temporary-pass-1", "new": "brand-new-pass-99", "confirm": "brand-new-pass-99"})
    assert r.status_code == 303 and r.headers["location"] == "/partners"
    assert c.get("/partners").status_code == 200
    assert c.get("/partners/leads").status_code == 200
    with session() as db:
        user = db.get(User, p.user_id)
        assert user.must_change_password is False
        assert db.query(AuditLog).filter(AuditLog.user_id == p.user_id, AuditLog.action == "password_change").count() == 1

    # the old password is dead, the new one works, and a routine change needs the current password
    fresh = TestClient(app, follow_redirects=False)
    assert fresh.post("/partners/login", data={"email": p.email, "password": "temporary-pass-1"}).status_code == 401
    login(fresh, "/partners/login", p.email, "brand-new-pass-99")
    assert fresh.get("/partners").status_code == 200
    page = fresh.get("/partners/password")
    assert page.status_code == 200 and 'name="current"' in page.text
    r = fresh.post("/partners/password", data={"current": "wrong", "new": "another-new-pass", "confirm": "another-new-pass"})
    assert r.status_code == 400 and "Current password is wrong" in r.text
    r = fresh.post("/partners/password", data={"current": "brand-new-pass-99", "new": "another-new-pass", "confirm": "another-new-pass"})
    assert r.status_code == 303


def test_admin_creates_partner_login_with_one_time_password(admin_client):
    r = admin_client.post("/admin/partners/new", data={
        "name": "Dev Dale", "first_name": "Dev", "email": "dev@example.test", "phone": "", "whatsapp": "07700 900555",
        "whatsapp_display": "", "bio": "", "tier": "area", "status": "active", "units_managed": "0", "notes": "",
    })
    assert r.status_code == 303, r.text
    partner_id = int(r.headers["location"].split("/")[3].split("?")[0])
    with session() as db:
        assert db.get(Partner, partner_id).whatsapp == "447700900555"
    r = admin_client.post(f"/admin/partners/{partner_id}/login")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    m = re.search(r'id="cred_pw" type="text" readonly value="([A-Za-z0-9]{12})"', r.text)
    assert m, "the generated password should be shown once on the page"
    password = m.group(1)
    with session() as db:
        user = db.query(User).filter(User.email == "dev@example.test").one()
        assert user.role == "partner" and user.partner_id == partner_id and user.must_change_password
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", "dev@example.test", password)
    r = c.get("/partners")
    assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1"
    r = admin_client.post(f"/admin/partners/{partner_id}/login")
    assert r.status_code == 303 and "already has a login" in flash_of(r.headers["location"])["err"]
