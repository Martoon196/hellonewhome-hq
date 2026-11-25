"""The app must let Neon scale to zero and survive the wake-up.

* A page that renders without being handed a session (the 404 page above all) returns its connection to the pool
  at once, so a scanner's burst of 404s can no longer empty the pool and stall the event loop.
* /sitemap.xml, polled every five minutes by an outside monitor, is answered from memory, not from Postgres, until
  something is committed.
* A Postgres engine retries its first connection while a scaled-to-zero compute starts, and switches psycopg's
  prepared statements off on Neon's PgBouncer endpoint.
"""
from __future__ import annotations

import gc

import pytest
from sqlalchemy import event

from app import db as dbmod
from app.db import SessionLocal, engine, make_engine
from app.models import Area
from app.routers import public


def test_a_burst_of_404s_leaves_no_connection_checked_out(client):
    gc.disable()                      # the old leak was only ever undone by the garbage collector
    try:
        for i in range(25):
            assert client.get(f"/.env-probe-{i}").status_code == 404
        assert engine.pool.checkedout() == 0
    finally:
        gc.enable()


def test_public_pages_return_their_connection(client):
    gc.disable()
    try:
        for path in ("/", "/contact", "/to-let"):
            assert client.get(path).status_code == 200
        assert engine.pool.checkedout() == 0
    finally:
        gc.enable()


@pytest.fixture()
def queries():
    seen: list[str] = []

    def count(conn, cursor, statement, *_):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    public.clear_sitemap_cache()
    yield seen
    event.remove(engine, "before_cursor_execute", count)


def test_sitemap_is_served_from_memory_after_the_first_fetch(client, queries):
    first = client.get("/sitemap.xml")
    assert first.status_code == 200 and "<urlset" in first.text
    built_with = len(queries)
    assert built_with > 0
    for _ in range(5):
        again = client.get("/sitemap.xml")
        assert again.text == first.text
    assert len(queries) == built_with, "a cached sitemap must not touch the database"


def test_a_commit_refreshes_the_sitemap(client, queries):
    client.get("/sitemap.xml")
    with SessionLocal() as s:
        area = s.query(Area).filter(Area.status == "active").first()
        assert area is not None
        old_slug = area.slug
        area.status = "hidden"
        s.commit()
    try:
        assert f"/areas/{old_slug}<" not in client.get("/sitemap.xml").text
    finally:
        with SessionLocal() as s:
            s.query(Area).filter(Area.slug == old_slug).one().status = "active"
            s.commit()
    assert f"/areas/{old_slug}<" in client.get("/sitemap.xml").text


def test_postgres_connect_is_retried_while_the_compute_wakes(monkeypatch):
    monkeypatch.setattr(dbmod, "CONNECT_RETRY_DELAYS", (0, 0))
    eng = make_engine("postgresql://u:p@127.0.0.1:9/nodb")
    attempts = []
    real_connect = eng.dialect.connect

    def flaky(*cargs, **cparams):
        attempts.append(cparams)
        if len(attempts) < 3:
            raise eng.dialect.loaded_dbapi.OperationalError("the endpoint is starting")
        return real_connect(*cargs, **cparams)

    monkeypatch.setattr(eng.dialect, "connect", flaky)
    with pytest.raises(Exception):            # the third attempt reaches the (closed) port and fails for real
        eng.connect()
    assert len(attempts) == 3
    assert attempts[0]["connect_timeout"] == dbmod.CONNECT_TIMEOUT_SECONDS
    eng.dispose()


def test_retry_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setattr(dbmod, "CONNECT_RETRY_DELAYS", (0, 0))
    eng = make_engine("postgresql://u:p@127.0.0.1:9/nodb")
    calls = []

    def down(*_a, **_k):
        calls.append(1)
        raise eng.dialect.loaded_dbapi.OperationalError("down")

    monkeypatch.setattr(eng.dialect, "connect", down)
    with pytest.raises(Exception):
        eng.connect()
    assert len(calls) == 3
    eng.dispose()


def test_pooler_endpoint_disables_prepared_statements():
    pooled = make_engine("postgresql://u:p@ep-x-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require")
    direct = make_engine("postgresql://u:p@ep-x.eu-west-2.aws.neon.tech/neondb?sslmode=require")
    assert dbmod.is_neon_pooler(str(pooled.url)) and not dbmod.is_neon_pooler(str(direct.url))
    pooled.dispose()
    direct.dispose()
