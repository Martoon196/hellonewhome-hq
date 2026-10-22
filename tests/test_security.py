"""Regression tests for the reviewer findings: stored XSS, session revocation, login throttling,
paused-partner lock-out, lead intake limits, admin password rotation, finder routing parity and the
smaller behaviour fixes that came with them."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.main import app
from app.models import Area, Lead, Partner, Setting, User
from app.services import kit
from app.services.postcodes import BORDER_ENGLAND, NI, SCOTLAND, SCOTLAND_BORDER, WALES, locate
from app.services.times import fmt_uk
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, login, make_partner, session

XSS = '<img src=x onerror=alert(1)>'


def _set_partner(partner_id: int, **fields) -> None:
    with session() as db:
        p = db.get(Partner, partner_id)
        for k, v in fields.items():
            setattr(p, k, v)
        db.add(p)
        db.commit()


def _profile_form(**overrides) -> dict:
    base = {"first_name": "", "name": "", "phone": "", "whatsapp": "", "whatsapp_display": "", "bio": ""}
    base.update(overrides)
    return base


# ---- high: stored XSS through the partner's first name --------------------
def test_partner_name_is_escaped_on_public_area_pages(client):
    p = make_partner(name="Eve Evil", first_name="Eve", email="eve@example.test", slug="xss-town", area_name="XSS Town",
                     postcodes=["XX1"], whatsapp="447700900777", whatsapp_display="07700 900777", sort=530)
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", p.email, p.password)
    r = c.post("/partners/profile", data=_profile_form(first_name=XSS, name=f"{XSS} Evil", bio="Hi"))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with session() as db:
        assert db.get(Partner, p.partner_id).first_name == XSS       # stored as typed, escaped on output
    for path in (f"/areas/{p.slug}", f"/areas/{p.slug}/lettings", f"/areas/{p.slug}/short-lets", "/areas", "/"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert XSS not in page.text, path
        assert "<img" not in page.text.replace('<img src="/uploads/', ""), path
    hub = client.get(f"/areas/{p.slug}").text
    assert "&lt;img src=x onerror=alert(1)&gt;" in hub            # the name is still shown, harmlessly
    # the JSON surfaces carry the raw string (JSON, not HTML) with no way to close a script tag
    assert "</" not in client.get("/areas.js").text.split("window.HNH_AREAS = ", 1)[1].replace("<\\/", "")
    # the marketing kit print pages are built from the same escaped context
    flyer = c.get("/partners/kit/flyer/a5-landlord", params={"area": p.slug})
    assert flyer.status_code == 200 and XSS not in flyer.text


# ---- high: a password reset ends every other session ----------------------
def test_admin_reset_revokes_sessions_and_needs_the_temporary_password(admin_client):
    p = make_partner(name="Fay Field", first_name="Fay", email="fay@example.test", slug="fayton", area_name="Fayton",
                     postcodes=["FY1"], whatsapp="", whatsapp_display="", sort=540)
    old = TestClient(app, follow_redirects=False)
    login(old, "/partners/login", p.email, p.password)
    assert old.get("/partners/leads").status_code == 200

    r = admin_client.post(f"/admin/partners/{p.partner_id}/reset")
    assert r.status_code == 200
    temp = re.search(r'id="cred_pw" type="text" readonly value="([A-Za-z0-9]{12})"', r.text).group(1)

    # the stale cookie is dead: no portal page, and no chance to set the new password
    r = old.get("/partners/leads")
    assert r.status_code == 303 and r.headers["location"].startswith("/partners/login")
    r = old.post("/partners/password", data={"current": "", "new": "attacker-chosen-1", "confirm": "attacker-chosen-1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/partners/login")
    with session() as db:
        assert auth.verify_password(temp, db.get(User, p.user_id).password_hash)

    # the real partner logs in with the temporary password and must present it again to replace it
    fresh = TestClient(app, follow_redirects=False)
    login(fresh, "/partners/login", p.email, temp)
    r = fresh.get("/partners/leads")
    assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1"
    r = fresh.post("/partners/password", data={"current": "", "new": "attacker-chosen-1", "confirm": "attacker-chosen-1"})
    assert r.status_code == 400
    r = fresh.post("/partners/password", data={"current": "not-the-temp", "new": "attacker-chosen-1", "confirm": "attacker-chosen-1"})
    assert r.status_code == 400
    r = fresh.post("/partners/password", data={"current": temp, "new": "fay-new-password-1", "confirm": "fay-new-password-1"})
    assert r.status_code == 303 and r.headers["location"] == "/partners"
    assert fresh.get("/partners/leads").status_code == 200          # the changing browser keeps a (new) session
    assert TestClient(app).post("/partners/login", data={"email": p.email, "password": "attacker-chosen-1"}).status_code == 401


def test_self_service_password_change_ends_other_sessions(client):
    p = make_partner(name="Gus Gale", first_name="Gus", email="gus@example.test", slug="gusby", area_name="Gusby",
                     postcodes=["GU1"], whatsapp="", whatsapp_display="", sort=550)
    phone = TestClient(app, follow_redirects=False)
    laptop = TestClient(app, follow_redirects=False)
    login(phone, "/partners/login", p.email, p.password)
    login(laptop, "/partners/login", p.email, p.password)
    assert phone.get("/partners").status_code == 200 and laptop.get("/partners").status_code == 200
    r = laptop.post("/partners/password", data={"current": p.password, "new": "gus-new-password-1", "confirm": "gus-new-password-1"})
    assert r.status_code == 303
    assert laptop.get("/partners").status_code == 200
    r = phone.get("/partners")
    assert r.status_code == 303 and r.headers["location"].startswith("/partners/login")


# ---- high: login throttle cannot be dodged with whitespace ----------------
def test_padded_email_shares_the_login_budget():
    c = TestClient(app, follow_redirects=False)
    for i in range(auth._MAX_ATTEMPTS):
        r = c.post("/admin/login", data={"email": ADMIN_EMAIL + " " * i, "password": "wrong"})
        assert r.status_code == 401, i
    for padded in (ADMIN_EMAIL, " " + ADMIN_EMAIL, ADMIN_EMAIL + "   ", ADMIN_EMAIL.upper()):
        assert c.post("/admin/login", data={"email": padded, "password": "wrong"}).status_code == 429, padded
    # even the right password waits out the window
    assert c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 429
    # the partner login shares the same rule
    auth._attempts.clear()
    for i in range(auth._MAX_ATTEMPTS):
        assert c.post("/partners/login", data={"email": f" nobody@example.test{' ' * i}", "password": "wrong"}).status_code == 401
    assert c.post("/partners/login", data={"email": "nobody@example.test", "password": "wrong"}).status_code == 429


def test_login_throttle_also_caps_the_address_across_emails():
    c = TestClient(app, follow_redirects=False)
    for i in range(auth._MAX_IP_ATTEMPTS):
        assert c.post("/admin/login", data={"email": f"guess{i}@example.test", "password": "wrong"}).status_code == 401
    assert c.post("/admin/login", data={"email": "another@example.test", "password": "wrong"}).status_code == 429


# ---- medium: pausing a partner ends their access at once --------------------
def test_paused_partner_session_stops_working(client):
    p = make_partner(name="Hal Hill", first_name="Hal", email="hal@example.test", slug="halford", area_name="Halford",
                     postcodes=["HA1"], whatsapp="", whatsapp_display="", sort=560)
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", p.email, p.password)
    assert c.get("/partners/leads").status_code == 200
    for status in ("paused", "left"):
        _set_partner(p.partner_id, status=status)
        for path in ("/partners", "/partners/leads", "/partners/profile", "/partners/kit", "/partners/resources"):
            r = c.get(path)
            assert r.status_code == 403 and "paused" in r.text, (status, path)
        assert c.post("/partners/profile", data=_profile_form(name="Hal Hill")).status_code == 403
        assert TestClient(app).post("/partners/login", data={"email": p.email, "password": p.password}).status_code == 403
    _set_partner(p.partner_id, status="active")
    assert c.get("/partners/leads").status_code == 200


# ---- medium: /api/lead intake limits -----------------------------------------
def test_api_lead_rejects_oversized_bodies_and_trims_payload(client, partner_a):
    big = {"name": "Big Lead", "postcode": "TS1 1AA", "blob": "x" * (3 * 1024 * 1024)}
    r = client.post("/api/lead", json=big)
    assert r.status_code == 413
    r = client.post("/api/lead", content=b'{"name": "Chunked", "pad": "' + b"y" * 40_000 + b'"}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    payload = {"name": "Trim Lead", "postcode": "TS1 1AA", "kind": "partner_request" + "x" * 500,
               "note": "n" * 2000, "items": list(range(500)), "nested": {"a": {"b": "c"}}}
    payload.update({f"extra_{i}": "v" for i in range(60)})
    r = client.post("/api/lead", json=payload)
    assert r.status_code == 200, r.text
    with session() as db:
        lead = db.get(Lead, r.json()["id"])
        assert lead.kind == "enquiry"                                   # unknown kinds are never stored
        assert len(lead.payload["note"]) == 500
        assert isinstance(lead.payload["items"], str) and len(lead.payload["items"]) <= 500
        assert len(lead.payload) <= 40
        assert lead.area_id == partner_a.area_id
    # the portal's own kind cannot be forged from the public form
    r = client.post("/api/lead", json={"name": "Forged", "postcode": "TS1 1AA", "kind": "partner_request"})
    with session() as db:
        assert db.get(Lead, r.json()["id"]).kind == "enquiry"
    assert client.post("/api/lead", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400


def test_api_lead_is_rate_limited_per_address(client):
    from app.routers.public import LEAD_RATE_LIMIT
    for i in range(LEAD_RATE_LIMIT):
        assert client.post("/api/lead", json={"name": f"Flood {i}", "postcode": "TS1 1AA"}).status_code == 200
    r = client.post("/api/lead", json={"name": "Flood over", "postcode": "TS1 1AA"})
    assert r.status_code == 429 and r.json()["ok"] is False
    with session() as db:
        assert db.query(Lead).filter(Lead.name == "Flood over").count() == 0


def test_api_lead_refuses_cross_site_posts(client):
    with session() as db:
        before = db.query(Lead).count()
    r = client.post("/api/lead", json={"name": "Forged", "postcode": "TS1 1AA"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/lead", data={"name": "Forged", "postcode": "TS1 1AA"}, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403
    with session() as db:
        assert db.query(Lead).count() == before
    assert client.post("/api/lead", json={"name": "Own site", "postcode": "TS1 1AA"}, headers={"Origin": "http://testserver"}).status_code == 200


def test_api_lead_ignores_paused_or_hidden_area_slug(client, admin_client):
    with session() as db:
        partner = Partner(name="Paula Pause", first_name="Paula", email="paula@example.test", status="active", tier="area")
        db.add(partner)
        db.flush()
        db.add(Area(slug="paused-town", name="Paused Town", region="england", role="partner", status="paused",
                    partner_id=partner.id, postcodes=["PZ1"], sort=570))
        db.commit()
        pid = partner.id
    r = client.post("/api/lead", json={"name": "Hidden Field Lead", "area": "paused-town"})
    assert r.status_code == 200 and r.json()["area"] is None
    with session() as db:
        lead = db.get(Lead, r.json()["id"])
        assert lead.area_id is None and lead.partner_id is None
    # the same postcode does not route there either
    assert client.get("/api/locate", params={"postcode": "PZ1 1AA"}).json()["covered"] is False
    # once active again the slug fallback works
    with session() as db:
        a = db.query(Area).filter(Area.slug == "paused-town").one()
        a.status = "active"
        db.commit()
    r = client.post("/api/lead", json={"name": "Visible Field Lead", "area": "paused-town"})
    assert r.json()["area"]["slug"] == "paused-town"
    with session() as db:
        assert db.get(Lead, r.json()["id"]).partner_id == pid


def test_request_bodies_over_the_upload_cap_are_refused_early(admin_client):
    from app.main import MAX_BODY_BYTES
    r = admin_client.post("/admin/resources/new", content=b"x", headers={
        "Content-Type": "multipart/form-data; boundary=abc", "Content-Length": str(MAX_BODY_BYTES + 1)})
    assert r.status_code == 413


# ---- low: admins must rotate a generated password --------------------------
def test_new_admin_must_change_generated_password(admin_client):
    r = admin_client.post("/admin/users", data={"email": "second-admin@example.test"})
    assert r.status_code == 200
    temp = re.search(r'id="cred_pw" type="text" readonly value="([A-Za-z0-9]{14})"', r.text).group(1)
    c = TestClient(app, follow_redirects=False)
    login(c, "/admin/login", "second-admin@example.test", temp)
    for path in ("/admin", "/admin/settings", "/admin/leads", "/partners", "/partners/kit"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1", path
    assert c.get("/partners/password", params={"first": "1"}).status_code == 200
    r = c.post("/partners/password", data={"current": "", "new": "second-admin-pw-1", "confirm": "second-admin-pw-1"})
    assert r.status_code == 400
    r = c.post("/partners/password", data={"current": temp, "new": "second-admin-pw-1", "confirm": "second-admin-pw-1"})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert c.get("/admin").status_code == 200 and c.get("/admin/settings").status_code == 200
    with session() as db:
        assert db.query(User).filter(User.email == "second-admin@example.test").one().must_change_password is False
    assert 'href="/partners/password"' in c.get("/admin").text and 'href="/admin/audit"' in c.get("/admin").text


# ---- high: browser finder agrees with the server -----------------------------
def _js_locate(areas: list[dict], postcodes: list[str], pick: str = "r && r.area ? r.area.slug : null") -> list:
    """Run site.js's locate() under node with the given areas; returns `pick` (a JS expression over the result r,
    by default the matched slug) per postcode."""
    src = (config.ROOT / "app" / "static" / "site.js").read_text(encoding="utf-8")
    start = src.index("function locate(pc) {")
    depth, i = 0, start
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    fn = src[start:i + 1]
    consts = "\n".join(line for line in src.splitlines() if re.match(r"\s*var [A-Z_]+ = \[", line))
    script = (f"var AREAS = {json.dumps(areas)};\n{consts}\n{fn}\n"
              f"console.log(JSON.stringify({json.dumps(postcodes)}.map(function (pc) {{ var r = locate(pc); return {pick}; }})));")
    out = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run site.js")
def test_browser_and_server_route_nested_districts_the_same_way(client, db):
    """London lists the whole area PL (sorted first); Plymouth lists PL1 (sorted later). Both rules must send
    PL1 to Plymouth and PL2 to London, whatever the list order."""
    with session() as s:
        london = s.query(Area).filter(Area.slug == "london").one()
        london.postcodes = list(london.postcodes) + ["PL"]      # a whole-area code, alongside the seeded London codes
        s.add(london)
        s.add(Area(slug="plymouth-test", name="Plymouth test", region="england", role="partner", status="active",
                   postcodes=["PL1"], sort=900))
        s.commit()
    db.expire_all()
    try:
        assert "PL" in db.query(Area).filter(Area.slug == "london").one().postcodes
        cases = ["PL1 1AA", "PL2 5BB", "SE1 7PB", "TS1 2AB", "EH1 1YZ", "nonsense"]
        server = [loc.area.slug if loc and loc.area else None for loc in (locate(db, pc) for pc in cases)]
        assert server[0] == "plymouth-test" and server[1] == "london"
        areas = client.get("/api/areas").json()
        assert [a["slug"] for a in areas].index("london") < [a["slug"] for a in areas].index("plymouth-test")
        assert _js_locate(areas, cases) == server
        # the lead API and the browser copy name the same partner for the same postcode
        assert client.post("/api/lead", json={"name": "PL1 lead", "postcode": "PL1 1AA"}).json()["area"]["slug"] == "plymouth-test"
    finally:
        with session() as s:
            kt = s.query(Area).filter(Area.slug == "plymouth-test").one()
            s.query(Lead).filter(Lead.area_id == kt.id).delete()
            s.delete(kt)
            london = s.query(Area).filter(Area.slug == "london").one()
            london.postcodes = [c for c in london.postcodes if c != "PL"]
            s.add(london)
            s.commit()


def _js_lists() -> dict[str, set[str]]:
    """The postcode-area constants site.js declares (var NAME = ["..", ..];), by name."""
    src = (config.ROOT / "app" / "static" / "site.js").read_text(encoding="utf-8")
    return {m.group(1): set(json.loads(m.group(2)))
            for m in re.finditer(r"^\s*var ([A-Z_]+) = (\[[^\]]*\]);", src, re.M)}


def test_browser_and_server_carry_the_same_nation_lists():
    """site.js and services/postcodes.py hold the same lists. The fifteen wholly Scottish areas, the TD border area
    and BT are Property Predator's (backend/services/jurisdiction.py, frontend/pp-jurisdiction.js); TD15 as England
    is Hello New Home's own split of PP's "scotland-likely" TD, since HNH has no "likely" region."""
    js = _js_lists()
    server = {"WALES": WALES, "SCOTLAND": SCOTLAND, "SCOTLAND_BORDER": SCOTLAND_BORDER, "BORDER_ENGLAND": BORDER_ENGLAND,
              "NI": NI}
    assert js == server
    assert server["SCOTLAND"] == {"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "ZE"}
    assert (server["SCOTLAND_BORDER"], server["BORDER_ENGLAND"], server["NI"]) == ({"TD"}, {"TD15"}, {"BT"})


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run site.js")
def test_browser_and_server_put_berwick_in_england(db):
    """The finder must not tell a Berwick-upon-Tweed (TD15) landlord "Not yet in Scotland"; the rest of TD is
    Scotland, as on the server."""
    cases = ["TD15 1AA", "td151aa", "TD1 1AA", "TD6 9AA", "EH1 1YZ", "NE1 4ST", "SA1 1AA", "BT1 5GS"]
    server = [locate(db, pc).region for pc in cases]
    assert server == ["england", "england", "scotland", "scotland", "scotland", "england", "wales", "northern-ireland"]
    assert _js_locate([], cases, pick="r && r.region") == server


def test_admin_is_told_when_a_district_nests_inside_another_areas_whole_area(admin_client):
    from tests.test_app import area_form, flash_of
    with session() as s:
        london = s.query(Area).filter(Area.slug == "london").one()
        london.postcodes = list(london.postcodes) + ["PL"]
        s.add(london)
        s.commit()
    r = admin_client.post("/admin/areas/new", data=area_form(name="Plymouth nest", postcodes="PL1, PL3", status="coming", sort="910"))
    assert r.status_code == 303, r.text
    flash = flash_of(r.headers["location"])["ok"]
    assert flash.startswith("Area saved.") and "PL1 sits inside the whole area PL listed for London" in flash
    area_id = int(r.headers["location"].split("/")[3].split("?")[0])
    r = admin_client.post(f"/admin/areas/{area_id}", data=area_form(name="Plymouth nest", postcodes="PL1", status="coming", sort="910", action="view"))
    assert r.status_code == 303 and "PL1 sits inside" in flash_of(r.headers["location"])["ok"]
    with session() as s:
        london = s.query(Area).filter(Area.slug == "london").one()
        london.postcodes = [c for c in london.postcodes if c != "PL"]
        s.add(london)
        nest = s.query(Area).filter(Area.slug.like("plymouth-nest%")).first()
        if nest:
            s.delete(nest)
        s.commit()


# ---- medium: bespoke content cannot take the public pages down ---------------
def test_bad_content_json_is_rejected_and_legacy_bad_content_renders(admin_client, client):
    from tests.test_app import area_form
    bad = [
        json.dumps({"hub": {"sections": ["str"]}}),
        json.dumps({"hub": {"sections": [{"h2": "x", "html": 3}]}}),
        json.dumps({"lettings": {"faqs": [{"q": "x"}]}}),
        json.dumps({"neighbourhoods": ["Town"]}),
    ]
    for content in bad:
        r = admin_client.post("/admin/areas/new", data=area_form(name="Broken content", postcodes="BC1", content=content))
        assert r.status_code == 400, content
        assert "must be an object" in r.text, content
    with session() as db:
        assert db.query(Area).filter(Area.slug == "broken-content").count() == 0
        db.add(Area(slug="legacy-content", name="Legacy content", region="england", role="partner", status="active",
                    postcodes=["LC1"], sort=920,
                    content={"hub": {"sections": ["str", {"h2": "Real", "html": "<p>Real section</p>"}, {"h2": "No html"}],
                                     "faqs": [{"q": "Only q"}, "str", {"q": "Full", "a": "Answer"}]}}))
        db.commit()
    for path in ("/areas/legacy-content", "/areas/legacy-content/lettings", "/areas/legacy-content/short-lets"):
        r = client.get(path)
        assert r.status_code == 200, path
    hub = client.get("/areas/legacy-content").text
    assert "Real section" in hub and "Only q" in hub and "Answer" in hub


# ---- medium: fees_confirmed can be set, which clears the kit hold notice -----
def test_fees_confirmed_setting_clears_kit_notice(admin_client, partner_a, partner_a_client):
    from app.services import settings as site_settings
    with session() as db:                      # the default is now confirmed; start this test from the hold state
        site_settings.put(db, "fees_confirmed", False)
        db.commit()
    page = admin_client.get("/admin/settings")
    assert page.status_code == 200 and 'name="fees_confirmed"' in page.text and "Fee sheet confirmed" in page.text
    assert "hold the fee-bearing items" in partner_a_client.get("/partners/kit", params={"area": partner_a.slug}).text
    with session() as db:
        current = site_settings.get_all(db)
    form = {}
    for key, value in current.items():
        if isinstance(value, dict):
            form.update({f"{key}__{sub}": str(v) for sub, v in value.items()})
        elif isinstance(value, bool):
            if value:
                form[key] = "1"
        else:
            form[key] = str(value)
    form.pop("fees_confirmed", None)
    form["settings_form"] = re.search(r'name="settings_form" value="([0-9a-f]+)"', page.text).group(1)
    r = admin_client.post("/admin/settings", data=dict(form, fees_confirmed="1"))
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.text
    with session() as db:
        assert db.get(Setting, "fees_confirmed").value is True
    assert "hold the fee-bearing items" not in partner_a_client.get("/partners/kit", params={"area": partner_a.slug}).text
    r = admin_client.post("/admin/settings", data=form)
    assert r.status_code == 303
    with session() as db:
        assert db.get(Setting, "fees_confirmed").value is False


# ---- low: coming-soon areas do not name a placeholder person -----------------
def test_coming_area_falls_back_to_head_office_wording(client, db):
    durham = db.query(Area).filter(Area.slug == "durham").one()
    assert durham.partner is not None and durham.partner.status == "pending"
    assert kit.partner_first(durham) == "the head office team" and kit.partner_name(durham) == "The head office team"
    page = client.get("/areas/durham").text
    assert "WhatsApp the head office team" in page and "WhatsApp Durham" not in page and "to be appointed" not in page
    assert client.get("/api/locate", params={"postcode": "DH1 3LE"}).json()["area"]["partner"] == "The head office team"
    by_slug = {a["slug"]: a for a in client.get("/api/areas").json()}
    assert by_slug["durham"]["partner"] == "The head office team" and by_slug["durham"]["partner_first"] == "the head office team"
    assert "to be appointed" not in client.get("/areas").text
    assert client.post("/api/lead", json={"name": "Durham lead", "postcode": "DH1 3LE"}).json()["area"]["partner"] == "The head office team"
    # an appointed partner is still named
    assert "Alice Archer" in client.get("/api/areas").text


# ---- low: partner_request leads filter in admin ---------------------------------
def test_admin_can_filter_partner_requests(admin_client, partner_a, partner_a_client):
    r = partner_a_client.post("/partners/area/request", data={"area_id": str(partner_a.area_id), "request": "Filter me please"})
    assert r.status_code == 303
    page = admin_client.get("/admin/leads", params={"kind": "partner_request"})
    assert page.status_code == 200 and 'value="partner_request" selected' in page.text
    with session() as db:
        total = db.query(Lead).count()
        requests = db.query(Lead).filter(Lead.kind == "partner_request").count()
        req = db.query(Lead).filter(Lead.kind == "partner_request").order_by(Lead.id.desc()).first()
    assert requests >= 1 and total != requests
    assert f"{requests} lead" in page.text and f"/admin/leads/{req.id}" in page.text
    assert "Partner change request" in page.text
    assert f"/admin/leads/{req.id}" not in admin_client.get("/admin/leads", params={"kind": "rent_review"}).text
    assert "Filter me please" in admin_client.get(f"/admin/leads/{req.id}").text


# ---- low: times are shown in UK time -----------------------------------------
def test_timestamps_render_in_uk_time(client, partner_a, partner_a_client, admin_client):
    assert fmt_uk(datetime(2026, 7, 1, 9, 15), "%H:%M") == "10:15"      # BST
    assert fmt_uk(datetime(2026, 1, 1, 9, 15), "%H:%M") == "09:15"      # GMT
    assert fmt_uk(None) == ""
    r = client.post("/api/lead", json={"name": "Summer Lead", "postcode": "TS1 1AA"})
    lead_id = r.json()["id"]
    with session() as db:
        lead = db.get(Lead, lead_id)
        lead.created_at = datetime(2026, 7, 1, 9, 15)
        db.commit()
    assert "01 Jul 2026 at 10:15" in partner_a_client.get(f"/partners/leads/{lead_id}").text
    assert "01 Jul 2026 10:15" in partner_a_client.get("/partners/leads").text
    admin_page = admin_client.get(f"/admin/leads/{lead_id}").text
    assert "01 Jul 2026 10:15" in admin_page and " UTC" not in admin_page
    assert "2026-07-01 10:15" in admin_client.get("/admin/leads/export.csv", params={"q": "Summer Lead"}).text


# ---- low: head office's own response target is used --------------------------
def test_admin_dashboard_flags_late_head_office_leads(admin_client, client):
    r = client.post("/api/lead", json={"name": "Late HQ Lead", "postcode": "SA1 1AA"})
    lead_id = r.json()["id"]
    with session() as db:
        lead = db.get(Lead, lead_id)
        assert lead.partner_id is None
        lead.created_at = datetime.utcnow() - timedelta(hours=30)
        db.commit()
    dash = admin_client.get("/admin").text
    assert "head-office lead past the 24-hour response target" in dash
    leads = admin_client.get("/admin/leads", params={"status": "new", "partner": "none"}).text
    assert "Late HQ Lead" in leads and "Past target" in leads and 'value="new" selected' in leads
    assert "Past target" not in admin_client.get("/admin/leads", params={"status": "contacted"}).text
    with session() as db:
        db.get(Lead, lead_id).status = "contacted"
        db.commit()
    assert "Late HQ Lead" not in admin_client.get("/admin/leads", params={"status": "new", "partner": "none"}).text


# ---- low: alert town list follows the database ------------------------------
def test_alert_town_options_come_from_public_areas(client, partner_a, admin_client):
    page = client.get("/to-let").text
    assert f'<option value="{partner_a.slug}">{partner_a.area_name}</option>' in page
    assert '<option value="">Somewhere else</option>' in page and "{{AREA_OPTIONS}}" not in page
    with session() as db:
        db.add(Area(slug="hidden-alert-town", name="Hidden Alert Town", region="england", role="partner", status="hidden", sort=930))
        db.commit()
    assert "hidden-alert-town" not in client.get("/to-let").text


# ---- low: account emails are one case, whatever was typed --------------------
def test_email_typed_with_capitals_and_spaces_logs_in_and_is_stored_lower_cased(admin_client):
    from tests.test_app import flash_of
    # an admin user created with capitals and padding is stored trimmed and lower-cased
    r = admin_client.post("/admin/users", data={"email": "  Mixed.Case@Example.TEST  "})
    assert r.status_code == 200, r.text
    temp = re.search(r'id="cred_pw" type="text" readonly value="([A-Za-z0-9]{14})"', r.text).group(1)
    with session() as db:
        assert db.query(User).filter(User.email == "mixed.case@example.test").count() == 1
        assert db.query(User).filter(User.email == "Mixed.Case@Example.TEST").count() == 0
    # and logs in however the address is typed; the credentials page showed the stored form
    assert 'mixed.case@example.test' in r.text
    c = TestClient(app, follow_redirects=False)
    login(c, "/admin/login", " MIXED.case@Example.test ", temp)
    assert c.get("/partners/password", params={"first": "1"}).status_code == 200
    # the same address in another case is the same login, so it cannot be created twice
    r = admin_client.post("/admin/users", data={"email": "MIXED.CASE@EXAMPLE.TEST"})
    assert r.status_code == 303 and "already exists" in flash_of(r.headers["location"])["err"]

    # a partner saved from the admin form with capitals and padding is stored lower-cased, and so is the
    # login created from the record; the partner logs in with whatever case they type
    r = admin_client.post("/admin/partners/new", data={
        "name": "Kay Kent", "first_name": "Kay", "email": "  Kay.Kent@Example.TEST ", "phone": "", "whatsapp": "",
        "whatsapp_display": "", "bio": "", "tier": "area", "status": "active", "units_managed": "0", "notes": "",
    })
    assert r.status_code == 303, r.text
    partner_id = int(r.headers["location"].split("/")[3].split("?")[0])
    with session() as db:
        assert db.get(Partner, partner_id).email == "kay.kent@example.test"
    r = admin_client.post(f"/admin/partners/{partner_id}/login")
    assert r.status_code == 200, r.text
    temp = re.search(r'id="cred_pw" type="text" readonly value="([A-Za-z0-9]{12})"', r.text).group(1)
    with session() as db:
        user = db.query(User).filter(User.email == "kay.kent@example.test").one()
        assert user.partner_id == partner_id and user.role == "partner"
    p = TestClient(app, follow_redirects=False)
    login(p, "/partners/login", "KAY.KENT@example.test   ", temp)
    r = p.get("/partners")
    assert r.status_code == 303 and r.headers["location"] == "/partners/password?first=1"


def test_partner_logins_differing_only_by_case_are_refused(admin_client):
    from tests.test_app import flash_of
    # two partner records whose addresses differ only by case and padding: the second was saved before
    # addresses were normalised, straight into the table, as a record from the SQLite days might have been
    with session() as db:
        first = Partner(name="Lou Lower", first_name="Lou", email="lou.lower@example.test", status="active", tier="area")
        second = Partner(name="Lou Upper", first_name="Lou", email="  LOU.Lower@Example.TEST ", status="active", tier="area")
        db.add_all([first, second])
        db.commit()
        first_id, second_id = first.id, second.id
    r = admin_client.post(f"/admin/partners/{first_id}/login")
    assert r.status_code == 200, r.text
    # the second create is refused with a flash message, not a 500 from the unique index
    r = admin_client.post(f"/admin/partners/{second_id}/login")
    assert r.status_code == 303, r.text
    assert "already belongs to another login" in flash_of(r.headers["location"])["err"]
    with session() as db:
        logins = db.query(User).filter(User.email.ilike("lou.lower@example.test")).all()
        assert len(logins) == 1 and logins[0].partner_id == first_id and logins[0].email == "lou.lower@example.test"
    # and the other way round: the mixed-case record creates first, is stored lower-cased, and then blocks the other
    with session() as db:
        third = Partner(name="Max Mixed", first_name="Max", email="MAX.Mixed@Example.TEST", status="active", tier="area")
        fourth = Partner(name="Max Lower", first_name="Max", email="max.mixed@example.test", status="active", tier="area")
        db.add_all([third, fourth])
        db.commit()
        third_id, fourth_id = third.id, fourth.id
    r = admin_client.post(f"/admin/partners/{third_id}/login")
    assert r.status_code == 200, r.text
    assert "max.mixed@example.test" in r.text and "MAX.Mixed" not in r.text
    r = admin_client.post(f"/admin/partners/{fourth_id}/login")
    assert r.status_code == 303 and "already belongs to another login" in flash_of(r.headers["location"])["err"]
    with session() as db:
        assert db.query(User).filter(User.email.ilike("max.mixed@example.test")).count() == 1
        assert db.query(User).filter(User.email == "max.mixed@example.test").one().partner_id == third_id


def test_login_row_from_before_normalisation_is_refused_not_a_500(admin_client):
    """A users row saved with capitals before addresses were normalised (as a row copied from the SQLite days
    could be) still logs in, however the address is typed, and blocks a new login for the same address with a
    flash message. Both the login routes and the pre-insert checks compare lower(email), the same expression
    as the unique index, so the request never reaches the index and the old row is never locked out."""
    from tests.test_app import flash_of
    with session() as db:
        db.add(User(email="Legacy.Mixed@Example.test", password_hash=auth.hash_password("legacy-pass-12345"),
                    role="partner", partner_id=None, is_active=True))
        partner = Partner(name="Lena Legacy", first_name="Lena", email="legacy.mixed@example.test", status="active", tier="area")
        db.add(partner)
        db.commit()
        partner_id = partner.id
    # the legacy row logs in with the address as stored, lower-cased, or in any other case
    for typed in ("Legacy.Mixed@Example.test", "legacy.mixed@example.test", " LEGACY.MIXED@EXAMPLE.TEST "):
        c = TestClient(app, follow_redirects=False)
        login(c, "/partners/login", typed, "legacy-pass-12345")
        assert c.get("/partners").status_code == 200
    # and the same for an admin row from before the rule
    with session() as db:
        db.add(User(email="Legacy.Admin@Example.test", password_hash=auth.hash_password("legacy-admin-pass-1"),
                    role="admin", is_active=True))
        db.commit()
    c = TestClient(app, follow_redirects=False)
    login(c, "/admin/login", "legacy.admin@example.test", "legacy-admin-pass-1")
    assert c.get("/admin").status_code == 200
    r = admin_client.post(f"/admin/partners/{partner_id}/login")
    assert r.status_code == 303, r.text
    assert "already belongs to another login" in flash_of(r.headers["location"])["err"]
    r = admin_client.post("/admin/users", data={"email": " LEGACY.mixed@example.test "})
    assert r.status_code == 303, r.text
    assert "already exists" in flash_of(r.headers["location"])["err"]
    with session() as db:
        assert db.query(User).filter(User.email.ilike("legacy.mixed@example.test")).count() == 1
