"""SQLAlchemy engine and session helpers.

One app, two engines. `DATABASE_URL` decides which:

* `sqlite:///...` (the default, local development and the test suite) gets the SQLite pragmas
  (foreign keys on, WAL journal) and the thread check relaxed for the ASGI worker;
* a Postgres URL (Neon in production) is normalised to the psycopg 3 driver and given a small,
  pre-pinged connection pool. `postgres://`, `postgresql://` and `postgresql+psycopg://` are all
  accepted; the first two are rewritten so SQLAlchemy never falls back to psycopg2.

The schema itself is managed by Alembic (see `app.migrations`), not by this module.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


PG_DRIVER_PREFIX = "postgresql+psycopg://"


def normalise_url(url: str) -> str:
    """Rewrite a Postgres URL to the psycopg (v3) driver; any other URL is returned unchanged."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return PG_DRIVER_PREFIX + url[len(prefix):]
    return url


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_postgres(url: str) -> bool:
    return url.startswith(("postgresql", "postgres:"))


def make_engine(url: str, **overrides) -> Engine:
    """An engine for `url` with the per-backend settings the app relies on.

    SQLite: `check_same_thread` off (FastAPI hands a connection between threads) and the pragmas
    applied on every new connection. Postgres: `pool_pre_ping` so a connection Neon has closed while
    the service was idle is replaced rather than handed to a request, a pool sized for a single
    small web instance, and a recycle time well inside Neon's idle timeout.
    """
    url = normalise_url(url)
    options: dict = {"pool_pre_ping": True}
    if is_sqlite(url):
        options["connect_args"] = {"check_same_thread": False}
    elif is_postgres(url):
        options.update({
            "pool_size": 5,
            "max_overflow": 5,
            "pool_timeout": 30,
            "pool_recycle": 1800,
            "connect_args": {"application_name": "hellonewhome-hq"},
        })
    options.update(overrides)
    eng = create_engine(url, **options)
    if is_sqlite(url):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return eng


DATABASE_URL = normalise_url(config.DATABASE_URL)
engine = make_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
