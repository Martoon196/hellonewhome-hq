"""Shared fixtures for the HQ test suite.

The environment is fixed at import time, BEFORE app.main (and therefore app.config, which
reads the variables once) is imported: a throwaway data directory, a throwaway upload
directory and a known admin password. DATABASE_URL is dropped so a developer's shell can never
point the suite at a real database; to run the suite on Postgres, set HNH_TEST_DATABASE_URL to a
throwaway database instead (its app tables are dropped and rebuilt at the start of the run).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

ADMIN_EMAIL = "admin@hellonewhome.co.uk"
ADMIN_PASSWORD = "test-admin-pass"
PARTNER_PASSWORD = "partner-pass-12345"

_DATA_DIR = tempfile.mkdtemp(prefix="hnh-test-data-")
_UPLOAD_DIR = tempfile.mkdtemp(prefix="hnh-test-uploads-")
os.environ["HNH_DATA_DIR"] = _DATA_DIR
os.environ["HNH_UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
os.environ["ADMIN_EMAIL"] = ADMIN_EMAIL
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["SECURE_COOKIES"] = "0"      # TestClient speaks plain http://testserver; a Secure cookie would never be sent back
os.environ["SMTP_HOST"] = ""            # never attempt a lead notification email
# The SQLite file inside the temp data dir, unless the opt-in variable names a throwaway Postgres database.
TEST_DATABASE_URL = os.environ.get("HNH_TEST_DATABASE_URL", "").strip()
os.environ.pop("DATABASE_URL", None)
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth, config  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Area, Partner, User  # noqa: E402

TEMP_DATA_DIR = _DATA_DIR
TEMP_UPLOAD_DIR = _UPLOAD_DIR


@pytest.fixture(scope="session", autouse=True)
def _temp_dirs():
    if TEST_DATABASE_URL:
        # A throwaway database named on purpose: start every run from nothing, so the lifespan builds the
        # schema with Alembic and seeds it exactly as it does for a fresh SQLite file.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.drop_all(engine)
    yield
    engine.dispose()
    shutil.rmtree(_DATA_DIR, ignore_errors=True)
    shutil.rmtree(_UPLOAD_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_throttles():
    """Every test starts with empty login and lead throttles, so one test's failed logins or posted leads
    never push a later test over a limit; a test that exercises a limit builds it up itself."""
    auth._attempts.clear()
    yield
    auth._attempts.clear()


@contextmanager
def session():
    """A short-lived SQLAlchemy session; open one per read so nothing stale is cached between requests."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db():
    with session() as s:
        yield s


@pytest.fixture(scope="session")
def client():
    """Anonymous client. Entering the context runs the lifespan, which creates the tables and seeds the data."""
    with TestClient(app, follow_redirects=False) as c:
        yield c


def login(c: TestClient, path: str, email: str, password: str):
    r = c.post(path, data={"email": email, "password": password})
    assert r.status_code == 303, f"login at {path} failed: {r.status_code} {r.text[:300]}"
    assert config.SESSION_COOKIE in r.headers.get("set-cookie", ""), "no session cookie set on login"
    return r


@pytest.fixture(scope="session")
def admin_client(client):
    c = TestClient(app, follow_redirects=False)
    login(c, "/admin/login", ADMIN_EMAIL, ADMIN_PASSWORD)
    return c


def make_partner(*, name: str, first_name: str, email: str, slug: str, area_name: str, postcodes: list[str],
                 whatsapp: str, whatsapp_display: str, region: str = "england", sort: int = 500,
                 must_change_password: bool = False, password: str = PARTNER_PASSWORD) -> SimpleNamespace:
    """A partner with a login and one active area, created straight through the models."""
    with session() as db:
        partner = Partner(name=name, first_name=first_name, email=email, phone="", whatsapp=whatsapp,
                          whatsapp_display=whatsapp_display, bio=f"{first_name} has lived in {area_name} for years.",
                          tier="area", status="active")
        db.add(partner)
        db.flush()
        user = User(email=email, password_hash=hash_password(password), role="partner", partner_id=partner.id,
                    is_active=True, must_change_password=must_change_password)
        area = Area(slug=slug, name=area_name, region=region, role="partner", status="active", partner_id=partner.id,
                    covers=f"{area_name} and the surrounding villages", postcodes=list(postcodes),
                    places=[f"{area_name} centre", "Riverside", "Old Town"], rent_avg="£850", rent_change="up 4.1%",
                    sort=sort)
        db.add_all([user, area])
        db.commit()
        return SimpleNamespace(
            partner_id=partner.id, user_id=user.id, area_id=area.id, email=email, password=password,
            slug=slug, area_name=area_name, postcodes=list(postcodes), name=name, first_name=first_name,
        )


@pytest.fixture(scope="session")
def partner_a(client):
    return make_partner(name="Alice Archer", first_name="Alice", email="alice@example.test", slug="middlesbrough",
                        area_name="Middlesbrough", postcodes=["TS1", "TS2", "TS3"], whatsapp="447700900001",
                        whatsapp_display="07700 900001", sort=500)


@pytest.fixture(scope="session")
def partner_b(client):
    return make_partner(name="Ben Brook", first_name="Ben", email="ben@example.test", slug="york",
                        area_name="York", postcodes=["YO1", "YO10", "YO24"], whatsapp="447700900002",
                        whatsapp_display="07700 900002", sort=510)


def partner_client(partner) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    login(c, "/partners/login", partner.email, partner.password)
    return c


@pytest.fixture(scope="session")
def partner_a_client(partner_a):
    return partner_client(partner_a)


@pytest.fixture(scope="session")
def partner_b_client(partner_b):
    return partner_client(partner_b)
