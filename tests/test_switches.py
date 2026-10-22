"""The switches head office sets in the admin rather than in the environment: the approved commercial fee sheet (a
one-time seed revision) quoted plus VAT, the "VAT registered" switch for the residential wording, the per-partner
service switches (migration 0005) and what they do to routing, the area pages and the portal, and the switch that
holds instructed properties back from Ordris.

Sam Switch (Leeds, LS1, LS2, LS6) is this module's own partner, so the seeded areas are left exactly as they were.
Ordris is never called for real: `ordris_sync.http_client` is replaced with a client on an httpx.MockTransport."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app import auth, config, migrations, seed as seed_module
from app.db import make_engine
from app.main import PAGES, app
from app.models import ApiClient, Area, AuditLog, Instruction, Lead, Outbox, Partner, Setting, User
from app.routers.admin import PARTNER_SERVICE_KEYS, SETTINGS_FORM_STALE, SETTINGS_FORM_VERSION
from app.services import content, mail, ordris_listings, ordris_sync, routing, settings as site_settings, vat
from scripts import outbox_retry
from tests.conftest import make_partner, partner_client, session

ROOT = Path(__file__).resolve().parent.parent
SAM_EMAIL = "sam.switch@example.test"
SENDER = "switches-sender"
SEATER = "switches-grouphq"
API_EMAILS = ("nina.newpartner@example.test", "omar.otherpartner@example.test")
ORDRIS_URL = "https://ordris.test"
ORDRIS_TOKEN = "ordris-secret-for-hnh-switches-0123456789abcdef"
NOINDEX = '<meta name="robots" content="noindex">'
# (name, kind, payload) of leads stored before revision 0005, for the migration test.
OLD_LEADS = (
    ("shortlet", "sa_appraisal", {}),
    ("commercial vacant", "commercial_appraisal", {"lease_status": "vacant"}),
    ("commercial let", "commercial_appraisal", {"lease_status": "let"}),
    ("review empty", "rent_review", {}),
    ("review managed", "rent_review", {"interest": "Full management", "type": "House"}),
    ("review hmo house", "rent_review", {"interest": "Rent guarantee", "type": "HMO / shared house"}),
    ("review hmo", "rent_review", {"interest": "HMO"}),
    ("review short let", "rent_review", {"interest": "Short let"}),
    ("review tenant find", "rent_review", {"interest": "Tenant find"}),
    ("contact", "contact", {"interest": "Short let"}),
)


# ---- fixtures and helpers ----------------------------------------------------------------
@pytest.fixture(scope="module")
def sam(client):
    return make_partner(name="Sam Switch", first_name="Sam", email=SAM_EMAIL, slug="leeds", area_name="Leeds",
                        postcodes=["LS1", "LS2", "LS6"], whatsapp="447700900077", whatsapp_display="07700 900077", sort=520)


@pytest.fixture(scope="module")
def sam_client(sam):
    return partner_client(sam)


def _token(name: str, scopes: str) -> str:
    with session() as db:
        existing = db.query(ApiClient).filter(ApiClient.name == name).first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        _, token = auth.create_api_client(db, name, scopes)
        db.commit()
    return token


@pytest.fixture(scope="module")
def sender(client):
    c = TestClient(app, follow_redirects=False)
    c.headers.update({"X-Partner-Client": SENDER, "X-Service-Token": _token(SENDER, "properties:write")})
    return c


@pytest.fixture(scope="module")
def seater(client):
    c = TestClient(app, follow_redirects=False)
    c.headers.update({"X-Partner-Client": SEATER, "X-Service-Token": _token(SEATER, "partners:write")})
    return c


@pytest.fixture(scope="module", autouse=True)
def _leave_things_as_found(client):
    """The switches go back to their defaults and everything this module made is removed, so later modules (and a
    re-run) see the seeded site."""
    yield
    with session() as db:
        site_settings.put(db, "vat_registered", False)
        site_settings.put(db, "ordris_sync_enabled", True)
        for inst in db.query(Instruction).filter(Instruction.external_ref.like("switches:%")).all():
            db.query(Outbox).filter(Outbox.target == f"instruction:{inst.id}").delete()
            db.query(AuditLog).filter(AuditLog.target == f"instruction:{inst.id}").delete()
            db.delete(inst)
        area = db.query(Area).filter(Area.slug == "leeds").first()
        if area is not None:
            db.query(Lead).filter(Lead.area_id == area.id).delete()
        db.query(Area).filter(Area.slug == "switch-empty").delete()
        for email in (SAM_EMAIL, *API_EMAILS):
            partner = db.query(Partner).filter(Partner.email == email).first()
            if partner is None:
                continue
            for u in db.query(User).filter(User.partner_id == partner.id).all():
                db.query(AuditLog).filter(AuditLog.user_id == u.id).delete()
                db.delete(u)
            for a in db.query(Area).filter(Area.partner_id == partner.id).all():
                db.delete(a)
            db.flush()
            db.delete(partner)
        db.query(ApiClient).filter(ApiClient.name.in_((SENDER, SEATER))).delete()
        db.commit()


def _settings_form(**changes) -> dict:
    """The admin settings form as the page would post it, with `changes` applied (a bool False is an unticked box)."""
    with session() as db:
        current = site_settings.get_all(db)
    current.update(changes)
    form = {}
    for key, value in current.items():
        if key not in site_settings.DEFAULTS:
            continue
        if isinstance(value, dict):
            form.update({f"{key}__{sub}": str(v) for sub, v in value.items()})
        elif isinstance(value, bool):
            if value:
                form[key] = "1"
        else:
            form[key] = str(value)
    form["settings_form"] = SETTINGS_FORM_VERSION
    return form


def _save_settings(admin_client: TestClient, **changes) -> None:
    r = admin_client.post("/admin/settings", data=_settings_form(**changes))
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.text


def _set_services(admin_client: TestClient, partner_id: int, **services) -> None:
    """Save the partner through the admin form, exactly as head office would, with `services` changed."""
    with session() as db:
        p = db.get(Partner, partner_id)
        form = {"name": p.name, "first_name": p.first_name, "email": p.email, "phone": p.phone, "whatsapp": p.whatsapp,
                "whatsapp_display": p.whatsapp_display, "bio": p.bio, "tier": p.tier, "status": p.status,
                "units_managed": str(p.units_managed), "notes": p.notes, "areas": [str(a.id) for a in p.areas],
                "services_form": "1"}
        if p.commercial_enabled:
            form["commercial_enabled"] = "1"
        wanted = {key: bool(getattr(p, key)) for key in PARTNER_SERVICE_KEYS}
    wanted.update(services)
    form.update({key: "1" for key, on in wanted.items() if on})
    r = admin_client.post(f"/admin/partners/{partner_id}", data=form)
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.text
    with session() as db:
        p = db.get(Partner, partner_id)
        assert {key: bool(getattr(p, key)) for key in PARTNER_SERVICE_KEYS} == wanted


def _all_on(admin_client: TestClient, partner_id: int) -> None:
    _set_services(admin_client, partner_id, residential_management_enabled=True, hmo_management_enabled=True,
                  shortlet_management_enabled=True)


def _lead(client: TestClient, **fields) -> dict:
    body = {"name": "Lenny Landlord", "phone": "07700 900123", "email": "lenny@example.test", "postcode": "LS1 4AP",
            "address": "4 Park Row, Leeds", "_page": "/valuation"}
    body.update(fields)
    r = client.post("/api/lead", json=body)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()


def _instruction(external_ref: str, service_line: str) -> dict:
    return {"external_ref": external_ref, "origin_system": "pp",
            "landlord": {"name": "Ivy Investor", "phone": "07700 900321"},
            "consent": {"given": True, "text_version": "hnh-landlord-2026-10"},
            "property": {"address": "9 Hyde Park Road, Leeds", "postcode": "LS6 1AA", "service_line": service_line,
                         "property_type": "Terraced house"}}


def _mock_ordris(monkeypatch, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"ok": True, "landlord_client_id": "lc_9", "property_id": "prop_9", "tenancy_id": None})
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", ORDRIS_TOKEN)
    monkeypatch.setattr(ordris_sync, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


# ---- 1. the approved commercial fee sheet: a one-time seed revision ------------------------
def test_seed_revision_approves_the_commercial_sheet_once_on_a_database_that_predates_it(tmp_path, monkeypatch):
    """The live database already holds the sheet unconfirmed with the old VAT note. The first boot of this release
    confirms it and quotes it plus VAT; after that head office's untick, and its own wording, survive every boot."""
    eng = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    migrations.run_migrations(eng, quiet=True)
    Session = sessionmaker(bind=eng)
    with Session() as db:
        old_sheet = dict(site_settings.DEFAULTS["fees_commercial"], vat_note=site_settings.OLD_COMMERCIAL_VAT_NOTE)
        db.add_all([Setting(key="fees_commercial_confirmed", value=False), Setting(key="fees_commercial", value=old_sheet),
                    Setting(key="seed_revisions", value={"swansea": 1})])
        db.commit()
    tag = "settings:commercial-fees-approved"
    with Session() as db:
        seed_module.seed(db, quiet=True)
    with Session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is True
        assert db.get(Setting, "fees_commercial").value["vat_note"] == "plus VAT"
        assert db.get(Setting, "fees_commercial").value["letting_minimum"] == "£1,500"
        revisions = db.get(Setting, "seed_revisions").value
        assert revisions[tag] == 1 and revisions["swansea"] == 1
        # head office unticks the box and words the note its own way: the next boots leave both alone
        site_settings.put(db, "fees_commercial_confirmed", False)
        site_settings.put(db, "fees_commercial", dict(old_sheet, vat_note="plus VAT at the standard rate"))
        db.commit()
    for _ in range(2):
        with Session() as db:
            seed_module.seed(db, quiet=True)
    with Session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is False
        assert db.get(Setting, "fees_commercial").value["vat_note"] == "plus VAT at the standard rate"
        # were the revision ever run again, an edited note would still never be overwritten
        site_settings.put(db, "seed_revisions", dict(db.get(Setting, "seed_revisions").value, **{tag: 0}))
        db.commit()
    with Session() as db:
        seed_module.seed(db, quiet=True)
    with Session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is True
        assert db.get(Setting, "fees_commercial").value["vat_note"] == "plus VAT at the standard rate"
        assert db.get(Setting, "seed_revisions").value[tag] == 1
        site_settings.put(db, "fees_commercial_confirmed", False)
        db.commit()
    # FEES_COMMERCIAL_CONFIRMED=1 still confirms the sheet on every boot, as before
    monkeypatch.setattr(config, "FEES_COMMERCIAL_CONFIRMED", True)
    with Session() as db:
        seed_module.seed(db, quiet=True)
    with Session() as db:
        assert db.get(Setting, "fees_commercial_confirmed").value is True
    eng.dispose()


def test_the_app_database_carries_the_revision_and_the_new_switch_defaults(client):
    with session() as db:
        assert db.get(Setting, "seed_revisions").value["settings:commercial-fees-approved"] == 1
        assert db.get(Setting, "fees_commercial_confirmed").value is True
        assert db.get(Setting, "vat_registered").value is False
        assert db.get(Setting, "ordris_sync_enabled").value is True
    assert site_settings.DEFAULTS["fees_commercial"]["vat_note"] == "plus VAT"


# ---- 3. the VAT wording --------------------------------------------------------------------
def test_vat_wording_is_untouched_while_not_registered_and_rewritten_once_registered():
    claims = ["No VAT on any fee", "12% of rent, capped at £150 a month, no VAT, no lock-in.",
              "Nothing is added for VAT, because we are not VAT-registered.", "with no VAT added",
              "Hello New Home 2 Limited is not VAT-registered: no VAT is added to any fee.",
              "(we are not VAT-registered)", "We charge one capped fee, add no VAT, and let you leave",
              "and no VAT on top", "Fee capped. No lock-in. No VAT."]
    claims += ["No VAT is added to any fee.", "There is no VAT added to any Hello New Home fee.",
               "No VAT is added to any Hello New Home fee, and there are no renewal charges."]
    for text_ in claims:
        assert vat.wording(text_, False) is text_
        assert vat.claims(text_), text_
        assert vat.claims(vat.wording(text_, True)) == [], text_
        # commercial fees are quoted plus VAT, so a rewrite never says VAT is included in every fee
        assert vat.unscoped(vat.wording(text_, True)) == [], text_
    assert vat.wording("No VAT on any fee", True) == "VAT included in every residential fee"
    assert vat.wording("No VAT is added to any fee.", True) == "Every residential fee includes VAT."
    assert vat.wording("There is no VAT added to any Hello New Home fee.", True) == "Every Hello New Home residential fee includes VAT."
    for unscoped in ("VAT included in every fee", "Every fee includes VAT.", "Every Hello New Home fee includes VAT",
                     "all fees include VAT"):
        assert vat.unscoped(unscoped), unscoped
    assert vat.wording("Nothing is added for VAT, because we are not VAT-registered.", True) == "The fee includes VAT."
    assert vat.wording("is not VAT-registered: no VAT is added to any fee", True) == (
        "is VAT-registered: residential fees include VAT and commercial fees are quoted plus VAT")
    # what other agents charge, and the commercial "plus VAT", are statements of fact either way
    for fact in ("12 to 18% of rent, uncapped, often plus VAT", "against an agent adding VAT", "All fees plus VAT.",
                 "VAT included where the agent charges it", "the VAT option to tax"):
        assert vat.wording(fact, True) == fact and not vat.claims(fact)
    assert vat.wording_deep({"a": ["no VAT", {"b": "No VAT."}], "n": 3}, True) == {"a": ["VAT included", {"b": "VAT included."}], "n": 3}


def test_every_no_vat_claim_in_the_sources_is_covered_by_the_rewrites():
    """Templates, page fragments, the area content, the seeded area facts, the marketing kit and the routers'
    fallback copy: with the switch on, no claim may survive. A new phrase the rewrites do not cover fails here."""
    sources = (list((ROOT / "app/templates/public").rglob("*.html")) + list((ROOT / "content").glob("*.json"))
               + list((ROOT / "content/areas").glob("*.json")) + [ROOT / "app/routers/public.py", ROOT / "app/services/kit.py"])
    found = 0
    for path in sources:
        source = path.read_text(encoding="utf-8")
        found += len(vat.claims(source))
        assert vat.claims(vat.wording(source, True)) == [], path.name
        assert vat.unscoped(vat.wording(source, True)) == [], (path.name, vat.unscoped(vat.wording(source, True))[:3])
    assert found > 100        # the sweep really did read the claims


def test_the_public_site_reads_as_today_while_not_registered(client):
    home = client.get("/").text
    assert "Hello New Home 2 Limited is not VAT-registered: no VAT is added to any fee." in home
    assert "We charge one capped fee, add no VAT, and let you leave whenever you like." in home
    fees = client.get("/fees").text
    assert "and no VAT on top</h1>" in fees and "No hidden extras and no VAT: we are not VAT-registered" in fees
    assert ("If Hello New Home 2 Ltd registers for VAT, we will tell you at least 30 days before any fee changes, and you can "
            "leave without an exit fee.") in fees
    assert ("Commercial property (shops, offices, workshops and units on a business lease) is a separate service with its "
            'own fee sheet, on the <a href="/commercial">commercial lettings</a> page.') in fees
    hub = client.get("/areas/somerset").text
    assert "<li>No VAT on any fee</li>" in hub and "no VAT, no extras, no lock-in" in hub
    assert "(we are not VAT-registered)" in client.get("/partners-programme").text
    # beside fees quoted plus VAT, the footer says which fees have no VAT added
    for path in ("/commercial", "/areas/london/commercial"):
        page = client.get(path).text
        assert "£750 plus VAT" in page, path
        assert ("Hello New Home 2 Limited is not VAT-registered: no VAT is added to any residential fee, and commercial fees "
                "are quoted plus VAT.") in page, path
        assert "no VAT is added to any fee" not in page, path


def _public_urls(db) -> list[str]:
    urls = list(PAGES) + ["/commercial", "/no-such-page"]
    for a in db.query(Area).filter(Area.status.in_(("active", "coming", "paused"))).all():
        urls += [f"/areas/{a.slug}", f"/areas/{a.slug}/lettings", f"/areas/{a.slug}/short-lets", f"/areas/{a.slug}/commercial"]
    return urls


def test_vat_registered_switch_rewrites_every_public_page_and_the_kit(client, admin_client, sam, sam_client):
    admin_page = admin_client.get("/admin/settings").text
    assert 'name="vat_registered"' in admin_page and "Switching this on changes the public wording at once" in admin_page
    assert "review the residential figures" in admin_page
    _save_settings(admin_client, vat_registered=True)
    try:
        with session() as db:
            assert db.get(Setting, "vat_registered").value is True
            entry = db.query(AuditLog).filter(AuditLog.action == "settings.update").order_by(AuditLog.id.desc()).first()
            assert entry.detail == "vat_registered on"
            urls = _public_urls(db)
        for url in urls:
            r = client.get(url)
            assert r.status_code in (200, 404), url
            assert vat.claims(r.text) == [], (url, vat.claims(r.text)[:3])
            assert vat.unscoped(r.text) == [], (url, vat.unscoped(r.text)[:3])
        home = client.get("/").text
        assert ("Hello New Home 2 Limited is VAT-registered: residential fees include VAT and commercial fees are quoted "
                "plus VAT.") in home
        assert "12 to 18% of rent, uncapped, often plus VAT" in home          # what other agents charge is untouched
        fees = client.get("/fees").text
        assert "Every fee, in one place, the same in every area, with VAT included</h1>" in fees
        assert "Hello New Home 2 Ltd is registered for VAT and every fee on this page includes it. We will tell you" in fees
        hub = client.get("/areas/somerset").text
        assert "<li>VAT included in every residential fee</li>" in hub and "VAT included, no extras, no lock-in" in hub
        assert "Every residential fee includes VAT" in client.get("/fees").text            # the meta description
        assert "(net of VAT)" in client.get("/partners-programme").text
        commercial = client.get("/commercial").text
        assert "£750 plus VAT" in commercial                                    # commercial fees are already plus VAT
        assert "is VAT-registered: residential fees include VAT and commercial fees are quoted plus VAT." in commercial
        # the marketing kit: every tab, every printable flyer and letter, every graphic
        tpl = content.marketing_templates()
        kit_urls = [f"/partners/kit?area=leeds&tab={tab}&region=all" for tab in ("flyers", "fb", "graphics", "gbp", "letters", "scripts")]
        kit_urls += [f"/partners/kit/flyer/{f['id']}?area=leeds" for f in tpl["flyers"]]
        kit_urls += [f"/partners/kit/letter/{x['id']}?area=leeds" for x in tpl["letters"]]
        kit_urls += [f"/partners/kit/graphic/{g['id']}.svg?area=leeds" for g in tpl["graphics"]]
        for url in kit_urls:
            r = sam_client.get(url)
            assert r.status_code == 200, url
            assert vat.claims(r.text) == [], (url, vat.claims(r.text)[:3])
            assert vat.unscoped(r.text) == [], (url, vat.unscoped(r.text)[:3])
        assert "VAT included" in sam_client.get("/partners/kit?area=leeds&tab=flyers").text
        # the admin always shows what is stored
        with session() as db:
            somerset_id = db.query(Area).filter(Area.slug == "somerset").one().id
        assert "no VAT" in admin_client.get(f"/admin/areas/{somerset_id}").text
    finally:
        _save_settings(admin_client, vat_registered=False)
    assert "is not VAT-registered: no VAT is added to any fee." in client.get("/").text
    assert "No VAT" in sam_client.get("/partners/kit?area=leeds&tab=flyers").text


# ---- 4. services area by area: migration 0005 ------------------------------------------------
def test_migration_0005_keeps_todays_behaviour_and_comes_back_out(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'services.db'}")
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), "0004_commercial")
    columns = {c["name"] for c in inspect(eng).get_columns("partners")}
    assert "commercial_enabled" in columns and "hmo_management_enabled" not in columns
    with eng.begin() as conn:
        for name, email, commercial in (("Cleared Partner", "cleared@example.test", 1), ("Plain Partner", "plain@example.test", 0)):
            conn.execute(text("INSERT INTO partners (name, first_name, email, phone, whatsapp, whatsapp_display, bio, photo_path, "
                              "tier, status, units_managed, notes, created_at, commercial_enabled) VALUES (:n, '', :e, '', '', "
                              "'', '', '', 'area', 'active', 0, '', '2026-01-01 00:00:00', :c)"), {"n": name, "e": email, "c": commercial})
        # leads stored before the revision: the service is read from each one's kind and payload, as at intake
        for name, kind, payload in OLD_LEADS:
            conn.execute(text("INSERT INTO leads (created_at, kind, name, phone, email, postcode, address, region, status, payload, "
                              "notes, source_page, updated_at) VALUES ('2026-10-01 09:00:00', :k, :n, '', '', 'TA1', '', "
                              "'england', 'new', :p, '', '', '2026-10-01 09:00:00')"), {"k": kind, "n": name, "p": json.dumps(payload)})
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == "0005_partner_services" == migrations.head_revision()
    for column in inspect(eng).get_columns("partners"):
        if column["name"] in PARTNER_SERVICE_KEYS:
            assert not column["nullable"], column["name"]
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT email, residential_management_enabled, hmo_management_enabled, shortlet_management_enabled, "
                                 "commercial_management_enabled FROM partners ORDER BY email")).all()
        assert [tuple(bool(v) if i else v for i, v in enumerate(r)) for r in rows] == [
            ("cleared@example.test", True, True, True, True), ("plain@example.test", True, True, True, False)]
        services = dict(conn.execute(text("SELECT name, service FROM leads")).all())
    assert services == {name: routing.lead_service(kind, payload) for name, kind, payload in OLD_LEADS}
    assert services == {"shortlet": "shortlet_management", "commercial vacant": "commercial_letting",
                        "commercial let": "commercial_management", "review empty": "", "review managed": "residential_management",
                        "review hmo house": "hmo_management", "review hmo": "hmo_management", "review short let": "shortlet_management",
                        "review tenant find": "", "contact": ""}
    # a row written by something that does not know the columns still gets today's behaviour
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO partners (name, first_name, email, phone, whatsapp, whatsapp_display, bio, photo_path, tier, "
                          "status, units_managed, notes, created_at) VALUES ('Raw Row', '', 'raw@example.test', '', '', '', '', '', "
                          "'area', 'active', 0, '', '2026-01-01 00:00:00')"))
        raw = conn.execute(text("SELECT residential_management_enabled, hmo_management_enabled, shortlet_management_enabled, "
                                "commercial_management_enabled, commercial_enabled FROM partners WHERE email = 'raw@example.test'")).one()
    assert [bool(v) for v in raw] == [True, True, True, False, False]
    with eng.begin() as conn:
        command.downgrade(migrations.alembic_config(conn), "0004_commercial")
    columns = {c["name"] for c in inspect(eng).get_columns("partners")}
    assert not set(PARTNER_SERVICE_KEYS) & columns and "commercial_enabled" in columns
    assert "service" not in {c["name"] for c in inspect(eng).get_columns("leads")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM partners")).scalar() == 3
        assert conn.execute(text("SELECT count(*) FROM leads")).scalar() == len(OLD_LEADS)
    eng.dispose()


def test_partners_default_to_todays_services(client, admin_client, sam):
    with session() as db:
        for p in db.query(Partner).all():
            assert (p.residential_management_enabled, p.hmo_management_enabled, p.shortlet_management_enabled) == (True, True, True)
            assert p.commercial_management_enabled is bool(p.commercial_enabled)
    page = admin_client.get("/admin/partners/new").text
    for name in ("residential_management_enabled", "hmo_management_enabled", "shortlet_management_enabled"):
        assert re.search(rf'name="{name}" value="1" checked', page), name
    assert 'name="commercial_management_enabled" value="1">' in page and 'name="services_form" value="1"' in page


def test_lead_service_follows_the_form():
    assert routing.lead_service("sa_appraisal") == routing.SHORTLET_MANAGEMENT
    assert routing.lead_service("commercial_appraisal", {"lease_status": "vacant"}) == routing.COMMERCIAL_LETTING
    assert routing.lead_service("commercial_appraisal", {"lease_status": "let"}) == routing.COMMERCIAL_MANAGEMENT
    assert routing.lead_service("rent_review", {"interest": "Full management"}) == routing.RESIDENTIAL_MANAGEMENT
    assert routing.lead_service("rent_review", {"interest": "Rent guarantee"}) == routing.RESIDENTIAL_MANAGEMENT
    assert routing.lead_service("rent_review", {"interest": "Full management", "type": "HMO / shared house"}) == routing.HMO_MANAGEMENT
    assert routing.lead_service("rent_review", {"interest": "HMO"}) == routing.HMO_MANAGEMENT
    assert routing.lead_service("rent_review", {"interest": "Short let"}) == routing.SHORTLET_MANAGEMENT
    for interest in ("Tenant find", "Not sure", ""):
        assert routing.lead_service("rent_review", {"interest": interest}) == ""
    for kind in ("contact", "alert", "enquiry", "partner_application"):
        assert routing.lead_service(kind, {"interest": "Short let"}) == ""
    assert [routing.instruction_service(x) for x in ("residential", "hmo", "sa", "commercial")] == [
        "", routing.HMO_MANAGEMENT, routing.SHORTLET_MANAGEMENT, routing.COMMERCIAL_LETTING]


# ---- 4. routing ------------------------------------------------------------------------------
def test_leads_for_a_service_the_partner_does_not_offer_go_to_head_office(client, admin_client, sam, sam_client):
    _set_services(admin_client, sam.partner_id, shortlet_management_enabled=False, hmo_management_enabled=False)
    try:
        with session() as db:
            entry = (db.query(AuditLog).filter(AuditLog.target == f"partner:{sam.partner_id}", AuditLog.action == "partner.update")
                     .order_by(AuditLog.id.desc()).first())
            assert "short-let management off (was on)" in entry.detail and "HMO management off (was on)" in entry.detail
            assert "residential full management on;" in entry.detail and "commercial management off" in entry.detail
        sa = _lead(client, kind="sa_appraisal", name="Sasha Shortlet", _page="/serviced-accommodation")
        hmo = _lead(client, kind="rent_review", name="Harry Hmo", interest="Full management", type="HMO / shared house")
        shortlet_review = _lead(client, kind="rent_review", name="Shona Short", interest="Short let")
        managed = _lead(client, kind="rent_review", name="Manny Managed", interest="Full management", type="House")
        find = _lead(client, kind="rent_review", name="Tina Tenantfind", interest="Tenant find")
        hq_whatsapp = site_settings.DEFAULTS["hq_whatsapp"]
        for reply in (sa, hmo, shortlet_review):
            assert reply["area"] == {"slug": "leeds", "name": "Leeds", "partner": "The head office team", "whatsapp": hq_whatsapp}
        for reply in (managed, find):
            assert reply["area"]["partner"] == "Sam Switch" and reply["area"]["whatsapp"] == "447700900077"
        with session() as db:
            leeds = db.query(Area).filter(Area.slug == "leeds").one()
            got = {lead.id: lead for lead in db.query(Lead).filter(Lead.area_id == leeds.id).all()}
            assert got[sa["id"]].partner_id is None and got[sa["id"]].service == "shortlet_management"
            assert got[sa["id"]].notes == "Routed to head office: Sam Switch does not offer short-let management in Leeds."
            assert got[hmo["id"]].partner_id is None and got[hmo["id"]].notes.endswith("does not offer HMO management in Leeds.")
            assert got[shortlet_review["id"]].partner_id is None
            assert got[managed["id"]].partner_id == sam.partner_id and got[managed["id"]].notes == ""
            assert got[find["id"]].partner_id == sam.partner_id and got[find["id"]].service == ""
            # only head office is emailed about the ones Sam does not take
            assert mail.notify_recipients(leeds, service="shortlet_management") == [config.LEAD_NOTIFY_TO]
            assert mail.notify_recipients(leeds, service="residential_management") == [config.LEAD_NOTIFY_TO, SAM_EMAIL]
        # Sam's portal leaves them out, list and detail alike; head office sees every one
        listing = sam_client.get("/partners/leads").text
        assert "Manny Managed" in listing and "Tina Tenantfind" in listing
        for name in ("Sasha Shortlet", "Harry Hmo", "Shona Short"):
            assert name not in listing, name
        assert sam_client.get(f"/partners/leads/{sa['id']}").status_code == 404
        assert sam_client.get(f"/partners/leads/{managed['id']}").status_code == 200
        assert "Routed to head office" in admin_client.get(f"/admin/leads/{sa['id']}").text
        # once Sam offers short lets, head office can hand the lead over by saving it with its area
        _set_services(admin_client, sam.partner_id, shortlet_management_enabled=True)
        with session() as db:
            leeds_id = db.query(Area).filter(Area.slug == "leeds").one().id
        r = admin_client.post(f"/admin/leads/{sa['id']}", data={"status": "", "notes": "", "area_id": str(leeds_id)})
        assert r.status_code == 303 and "ok=" in r.headers["location"]
        with session() as db:
            assert db.get(Lead, sa["id"]).partner_id == sam.partner_id
        assert sam_client.get(f"/partners/leads/{sa['id']}").status_code == 200
    finally:
        _all_on(admin_client, sam.partner_id)


def test_instructions_for_a_service_the_partner_does_not_offer_go_to_head_office(client, admin_client, sam, sam_client, sender):
    _set_services(admin_client, sam.partner_id, hmo_management_enabled=False, shortlet_management_enabled=False)
    try:
        refs = {}
        for line in ("hmo", "sa", "residential"):
            r = sender.post("/api/partner/properties", json=_instruction(f"switches:{line}-1", line))
            assert r.status_code == 201, r.text
            refs[line] = r.json()
        assert refs["hmo"]["instruction"]["partner_first"] == "the head office team"
        assert refs["residential"]["instruction"]["partner_first"] == "Sam"
        assert "Routed" not in str(refs["hmo"])                    # the sender is not told why, only who
        with session() as db:
            rows = {i.service_line: i for i in db.query(Instruction).filter(Instruction.external_ref.like("switches:%-1")).all()}
            assert rows["hmo"].partner_id is None and rows["sa"].partner_id is None and rows["residential"].partner_id == sam.partner_id
            assert rows["hmo"].area.slug == "leeds"
            assert rows["hmo"].notes == "Routed to head office: Sam Switch does not offer HMO management in Leeds."
            entry = db.query(AuditLog).filter(AuditLog.target == f"instruction:{rows['sa'].id}").one()
            assert "partner head office" in entry.detail and "does not offer short-let management" in entry.detail
            ids = {line: inst.id for line, inst in rows.items()}
        listing = sam_client.get("/partners/instructions").text
        assert refs["residential"]["ref"] in listing
        assert refs["hmo"]["ref"] not in listing and refs["sa"]["ref"] not in listing
        assert sam_client.get(f"/partners/instructions/{ids['hmo']}").status_code == 404
        assert admin_client.get(f"/admin/instructions/{ids['hmo']}").status_code == 200
    finally:
        _all_on(admin_client, sam.partner_id)
    # switched back on, the same kinds of property reach Sam again
    r = sender.post("/api/partner/properties", json=_instruction("switches:hmo-2", "hmo"))
    assert r.status_code == 201 and r.json()["instruction"]["partner_first"] == "Sam"


# ---- 4. the public area pages -------------------------------------------------------------
def test_area_pages_show_only_the_services_the_partner_offers(client, admin_client, sam):
    with session() as db:
        empty = Area(slug="switch-empty", name="Empty Town", region="england", role="partner", status="active", partner_id=None,
                     covers="Empty Town", postcodes=["LS27"], places=[], sort=530)
        db.add(empty)
        db.commit()
    _set_services(admin_client, sam.partner_id, residential_management_enabled=False, shortlet_management_enabled=False)
    try:
        for path, title, form in (("/areas/leeds/short-lets", "Short-let management", "/serviced-accommodation?area=leeds#appraisal"),
                                  ("/areas/leeds/lettings", "Property management", "/valuation?area=leeds")):
            r = client.get(path)
            assert r.status_code == 200, path
            page = r.text
            assert f"{title} is not offered locally in Leeds yet" in page and NOINDEX in page
            assert f'href="{form}"' in page and "WhatsApp head office" in page
            assert f"https://wa.me/{site_settings.DEFAULTS['hq_whatsapp']}?text=" in page
            assert 'rel="canonical"' not in page
        hub = client.get("/areas/leeds").text
        assert NOINDEX not in hub and 'href="/areas/leeds/short-lets"' not in hub and 'href="/areas/leeds/lettings"' not in hub
        assert 'href="/areas/leeds/commercial"' in hub and "Full management 12%, capped at" not in hub
        commercial = client.get("/areas/leeds/commercial").text
        assert 'href="/areas/leeds/short-lets"' not in commercial and 'href="/areas/leeds/lettings"' not in commercial
        sitemap = client.get("/sitemap.xml").text
        assert f"{config.BASE_URL}/areas/leeds</loc>" in sitemap and f"{config.BASE_URL}/areas/leeds/commercial</loc>" in sitemap
        assert "/areas/leeds/short-lets" not in sitemap and "/areas/leeds/lettings" not in sitemap
        leeds = {a["slug"]: a for a in client.get("/api/areas").json()}["leeds"]
        assert set(leeds["pages"]) == {"hub", "commercial"}
        assert leeds["services"]["shortlet_management"] is False and leeds["services"]["hmo_management"] is True
        assert '"slug": "leeds"' in client.get("/areas.js").text
        # head office's own area, and an area with no partner, keep every page (head office covers them)
        for slug in ("swansea", "switch-empty"):
            for suffix in ("/lettings", "/short-lets"):
                page = client.get(f"/areas/{slug}{suffix}").text
                assert "not offered locally" not in page and NOINDEX not in page, slug + suffix
                assert f"{config.BASE_URL}/areas/{slug}{suffix}</loc>" in sitemap
            assert f'href="/areas/{slug}/short-lets"' in client.get(f"/areas/{slug}").text
    finally:
        _all_on(admin_client, sam.partner_id)
        with session() as db:
            db.query(Area).filter(Area.slug == "switch-empty").delete()
            db.commit()
    page = client.get("/areas/leeds/short-lets").text
    assert "not offered locally" not in page and NOINDEX not in page and "Full management 12%, capped at" in client.get("/areas/leeds").text
    assert "/areas/leeds/short-lets</loc>" in client.get("/sitemap.xml").text


def test_a_lead_stored_before_the_services_follows_the_routing_once_head_office_saves_it(admin_client, sam, sam_client):
    """A rent review stored before revision 0005 that the migration could not read (service ""): head office saving
    it with its area routes it by its payload, and the stored service then agrees, so the partner's portal hides it."""
    with session() as db:
        leeds = db.query(Area).filter(Area.slug == "leeds").one()
        lead = Lead(kind="rent_review", name="Olive Oldlead", phone="07700 900555", postcode="LS1", region="england",
                    area_id=leeds.id, partner_id=sam.partner_id, service="", payload={"interest": "Short let"})
        db.add(lead)
        db.commit()
        lead_id, leeds_id = lead.id, leeds.id
    _set_services(admin_client, sam.partner_id, shortlet_management_enabled=False)
    try:
        r = admin_client.post(f"/admin/leads/{lead_id}", data={"status": "contacted", "notes": "", "area_id": str(leeds_id)})
        assert r.status_code == 303 and "ok=" in r.headers["location"]
        with session() as db:
            lead = db.get(Lead, lead_id)
            assert lead.partner_id is None and lead.service == routing.SHORTLET_MANAGEMENT
            entry = db.query(AuditLog).filter(AuditLog.target == f"lead:{lead_id}").order_by(AuditLog.id.desc()).first()
            assert "partner to head office (Routed to head office: Sam Switch does not offer short-let management" in entry.detail
        assert "Olive Oldlead" not in sam_client.get("/partners/leads").text
        assert sam_client.get(f"/partners/leads/{lead_id}").status_code == 404
    finally:
        _all_on(admin_client, sam.partner_id)
    r = admin_client.post(f"/admin/leads/{lead_id}", data={"status": "", "notes": "", "area_id": str(leeds_id)})
    assert r.status_code == 303
    assert sam_client.get(f"/partners/leads/{lead_id}").status_code == 200


def test_homes_to_rent_stay_on_the_lettings_page_when_management_is_not_offered(client, admin_client, sam, monkeypatch):
    """The homes to rent are for tenants, and a tenant find is any partner's: switching residential management off
    replaces the lettings page with the "not offered locally yet" page, and the homes stay on it."""
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", ORDRIS_TOKEN)
    calls: list = []

    def fake(outwards):
        calls.append(tuple(outwards))
        return {"ok": True, "listings": [{"id": 31, "outward": "LS6", "bedrooms": 2, "property_type": "flat", "rent_pcm": 950,
                                          "available_from": "now", "photos": []}]}

    monkeypatch.setattr(ordris_listings, "request_listings", fake)
    ordris_listings.reset()
    _set_services(admin_client, sam.partner_id, residential_management_enabled=False)
    try:
        page = client.get("/areas/leeds/lettings").text
        assert "Property management is not offered locally in Leeds yet" in page and NOINDEX in page
        assert '<div class="homes" id="homes-to-rent">' in page and "<h2>Homes to rent in Leeds</h2>" in page
        assert "£950 <small>pcm</small>" in page and "LS6 · Available now" in page
        assert calls == [("LS1", "LS2", "LS6")]
        assert "homes-to-rent" not in client.get("/areas/leeds/short-lets").text      # the lettings page only, as before
    finally:
        _all_on(admin_client, sam.partner_id)
        ordris_listings.reset()


def test_partner_portal_shows_the_services_read_only(admin_client, sam, sam_client):
    page = sam_client.get("/partners/area").text
    assert "Services you offer" in page and "Head office sets these" in page and "ask head office" in page
    assert "<span class=\"status status-active\">On</span> Short-let management" in page
    _set_services(admin_client, sam.partner_id, shortlet_management_enabled=False)
    try:
        page = sam_client.get("/partners/area").text
        assert "<span class=\"status status-paused\">Off</span> Short-let management" in page
        assert "<span class=\"status status-paused\">Off</span> Commercial letting" in page
        assert 'name="shortlet_management_enabled"' not in page and "Short-lets page" not in page
    finally:
        _all_on(admin_client, sam.partner_id)


def test_admin_partner_form_without_the_service_boxes_leaves_them_alone(admin_client, sam):
    """A post from a partner page rendered before the boxes existed (no services_form marker) changes nothing."""
    _set_services(admin_client, sam.partner_id, hmo_management_enabled=False)
    try:
        with session() as db:
            p = db.get(Partner, sam.partner_id)
            form = {"name": p.name, "first_name": p.first_name, "email": p.email, "phone": p.phone, "whatsapp": p.whatsapp,
                    "whatsapp_display": p.whatsapp_display, "bio": p.bio, "tier": p.tier, "status": p.status,
                    "units_managed": "3", "notes": p.notes, "areas": [str(a.id) for a in p.areas]}
        r = admin_client.post(f"/admin/partners/{sam.partner_id}", data=form)
        assert r.status_code == 303
        with session() as db:
            p = db.get(Partner, sam.partner_id)
            assert p.units_managed == 3 and p.hmo_management_enabled is False and p.residential_management_enabled is True
            p.units_managed = 0
            db.commit()
        page = admin_client.get(f"/admin/partners/{sam.partner_id}").text
        assert 'name="hmo_management_enabled" value="1">' in page and "Offers HMO management" in page
        assert "not offered: HMO</span>" in admin_client.get("/admin/partners").text
    finally:
        _all_on(admin_client, sam.partner_id)


# ---- 4. the Partner API ------------------------------------------------------------------
def test_partner_api_returns_the_services_and_takes_them_on_create(seater):
    body = {"email": API_EMAILS[0], "name": "Nina Newpartner", "tier": "area", "status": "pending",
            "services": {"hmo_management": False, "commercial_management": True}}
    r = seater.post("/api/partner/partners", json=body)
    assert r.status_code == 201, r.text
    services = r.json()["partner"]["services"]
    assert services == {"residential_management": True, "hmo_management": False, "shortlet_management": True,
                        "commercial_letting": False, "commercial_management": True}
    assert r.json()["partner"]["commercial_enabled"] is False
    with session() as db:
        p = db.query(Partner).filter(Partner.email == API_EMAILS[0]).one()
        assert (p.hmo_management_enabled, p.commercial_management_enabled, p.residential_management_enabled) == (False, True, True)
        entry = db.query(AuditLog).filter(AuditLog.action == "partner_api.create", AuditLog.target == f"partner:{p.id}").one()
        assert "services residential full management on, HMO management off" in entry.detail
    # an update is head office's territory: the services sent are left as they are, and the answer says what is set
    r = seater.post("/api/partner/partners", json=dict(body, services={"hmo_management": True}))
    assert r.status_code == 200 and r.json()["partner"]["services"]["hmo_management"] is False
    with session() as db:
        p = db.query(Partner).filter(Partner.email == API_EMAILS[0]).one()
        assert p.hmo_management_enabled is False
        entry = (db.query(AuditLog).filter(AuditLog.action == "partner_api.update", AuditLog.target == f"partner:{p.id}")
                 .order_by(AuditLog.id.desc()).first())
        assert "services sent on an update were left as head office set them" in entry.detail
    assert seater.get("/api/partner/partners", params={"email": API_EMAILS[0]}).json()["partner"]["services"]["commercial_management"] is True
    # without services the body is exactly what it was, and the partner starts with today's services
    r = seater.post("/api/partner/partners", json={"email": API_EMAILS[1], "name": "Omar Otherpartner", "tier": "introducer",
                                                   "status": "pending"})
    assert r.status_code == 201 and r.json()["partner"]["services"] == {
        "residential_management": True, "hmo_management": True, "shortlet_management": True, "commercial_letting": False,
        "commercial_management": False}
    # refused in the API's own shape, and nothing written
    for services, field in (("yes", "services"), ({"commercial_letting": True}, "services.commercial_letting"),
                            ({"hmo_management": "no"}, "services.hmo_management")):
        r = seater.post("/api/partner/partners", json={"email": "nobody.new@example.test", "name": "No Body", "tier": "area",
                                                       "status": "pending", "services": services})
        assert r.status_code == 422 and r.json()["reason"] == "invalid_request" and r.json()["field"] == field, r.text
    with session() as db:
        assert db.query(Partner).filter(Partner.email == "nobody.new@example.test").count() == 0


# ---- a settings page rendered before the switches existed ------------------------------------
def test_a_settings_page_from_an_earlier_release_saves_nothing(admin_client):
    """A page rendered before an update added settings shows no box for them and the values of the time: posted
    after the deploy, it would switch Ordris sending off and untick the approved commercial sheet. It is refused."""
    page = admin_client.get("/admin/settings").text
    assert f'name="settings_form" value="{SETTINGS_FORM_VERSION}"' in page
    with session() as db:
        before = site_settings.get_all(db)
    # the form as the release before this one rendered it: no marker, no VAT or Ordris boxes, the old commercial values
    old_form = _settings_form(hq_phone="01792 000 000", fees_commercial_confirmed=False,
                              fees_commercial=dict(before["fees_commercial"], vat_note=site_settings.OLD_COMMERCIAL_VAT_NOTE))
    for key in ("settings_form", "vat_registered", "ordris_sync_enabled"):
        old_form.pop(key, None)
    for marker in (None, "0123456789abcdef"):
        form = dict(old_form) if marker is None else dict(old_form, settings_form=marker)
        r = admin_client.post("/admin/settings", data=form)
        assert r.status_code == 409 and SETTINGS_FORM_STALE in r.text
        assert 'name="ordris_sync_enabled" value="1" checked' in r.text        # the page now shows the current values
        with session() as db:
            assert site_settings.get_all(db) == before
    # the current page saves as before, and an unticked box it shows is a real untick
    _save_settings(admin_client, ordris_sync_enabled=False)
    try:
        with session() as db:
            assert db.get(Setting, "ordris_sync_enabled").value is False
    finally:
        _save_settings(admin_client, ordris_sync_enabled=True)


# ---- 5. the Ordris switch ------------------------------------------------------------------
def test_ordris_switch_holds_instructed_properties_and_resend_releases_them(admin_client, sam, sender, monkeypatch):
    calls: list = []
    _mock_ordris(monkeypatch, calls)
    r = sender.post("/api/partner/properties", json=_instruction("switches:ordris-1", "residential"))
    assert r.status_code == 201, r.text
    with session() as db:
        inst = db.query(Instruction).filter(Instruction.external_ref == "switches:ordris-1").one()
        inst_id, area_id = inst.id, inst.area_id
    page = admin_client.get("/admin/settings").text
    assert re.search(r'name="ordris_sync_enabled" value="1" checked', page) and "Send instructed properties to Ordris" in page
    _save_settings(admin_client, ordris_sync_enabled=False)
    try:
        with session() as db:
            assert db.query(AuditLog).filter(AuditLog.action == "settings.update").order_by(AuditLog.id.desc()).first().detail == \
                "ordris_sync_enabled off"
        r = admin_client.post(f"/admin/instructions/{inst_id}", data={"status": "instructed", "notes": "", "area_id": str(area_id)})
        assert r.status_code == 303 and "switched+off" in r.headers["location"]
        assert calls == []
        with session() as db:
            row = db.query(Outbox).filter(Outbox.target == f"instruction:{inst_id}").one()
            assert (row.state, row.attempts, row.last_error) == ("pending", 0, ordris_sync.SWITCHED_OFF)
            row_id = row.id
            assert not db.get(Instruction, inst_id).sent_to_ordris
        detail = admin_client.get(f"/admin/instructions/{inst_id}").text
        assert "Sending to Ordris is switched off" in detail and "Resend" in detail
        assert "Sending to Ordris is switched off" in admin_client.get("/admin/instructions").text
        # Resend and the retry command both leave it waiting while the switch is off
        r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
        assert r.status_code == 303 and "err=" in r.headers["location"]
        with session() as db:                   # (other modules may have left rows waiting too: none of them goes)
            row = db.get(Outbox, row_id)
            row.state, row.last_error = "failed", "Ordris answered 500"      # as an earlier failed attempt would leave it
            db.commit()
            results = ordris_sync.retry_pending(db)
            assert row_id in [row.id for row, _, _ in results] and not any(sent for _, sent, _ in results)
        with session() as db:                   # the retry did not even touch the row: its last error is still there
            row = db.get(Outbox, row_id)
            assert (row.state, row.last_error, row.attempts) == ("failed", "Ordris answered 500", 0)
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            assert outbox_retry.main([]) == 1
        assert ordris_sync.SWITCHED_OFF in err.getvalue() and "left waiting." in out.getvalue()
        assert calls == []
        with session() as db:
            assert db.get(Outbox, row_id).attempts == 0
    finally:
        _save_settings(admin_client, ordris_sync_enabled=True)
    # switched back on, Resend sends what waited
    r = admin_client.post(f"/admin/instructions/{inst_id}/outbox/{row_id}/resend")
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.headers["location"]
    assert len(calls) == 1 and calls[0].url == httpx.URL(ORDRIS_URL + ordris_sync.PATH)
    with session() as db:
        row = db.get(Outbox, row_id)
        assert (row.state, row.attempts) == ("sent", 1)
        assert db.get(Instruction, inst_id).ordris_property_id == "prop_9"
