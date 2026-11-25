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

import logging
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


PG_DRIVER_PREFIX = "postgresql+psycopg://"

log = logging.getLogger("hnh.db")

# Neon scales the compute to zero when the database is idle, and the first connection after that waits while the
# compute starts (usually well under a second, a few seconds at worst). CONNECT_TIMEOUT bounds one attempt;
# CONNECT_RETRY_DELAYS are the pauses before the second and third attempts, so a request survives a cold start of
# several seconds and still fails within about half a minute if Neon is genuinely unreachable.
CONNECT_TIMEOUT_SECONDS = 10
CONNECT_RETRY_DELAYS = (0.5, 2.0)


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


def is_neon_pooler(url: str) -> bool:
    """True for Neon's PgBouncer endpoint (the host carries "-pooler"), which runs in transaction mode."""
    return is_postgres(url) and "-pooler." in url


def make_engine(url: str, **overrides) -> Engine:
    """An engine for `url` with the per-backend settings the app relies on.

    SQLite: `check_same_thread` off (FastAPI hands a connection between threads) and the pragmas
    applied on every new connection. Postgres: `pool_pre_ping` so a connection Neon has closed while
    the service was idle is replaced rather than handed to a request, a pool sized for a single
    small web instance, and a recycle time well inside Neon's idle timeout.

    Postgres also gets a bounded connect timeout and up to three connection attempts (see CONNECT_RETRY_DELAYS), so
    the first request after Neon has scaled the compute to zero waits for it to wake instead of failing; and a
    pool_timeout of 10 seconds, so a request that cannot get a connection fails quickly rather than holding a worker
    thread for half a minute. On Neon's pooled endpoint (PgBouncer, transaction mode) psycopg's automatic server-side
    prepared statements are switched off, because consecutive transactions may land on different server connections.
    """
    url = normalise_url(url)
    options: dict = {"pool_pre_ping": True}
    if is_sqlite(url):
        options["connect_args"] = {"check_same_thread": False}
    elif is_postgres(url):
        connect_args: dict = {"application_name": "hellonewhome-hq", "connect_timeout": CONNECT_TIMEOUT_SECONDS}
        if is_neon_pooler(url):
            connect_args["prepare_threshold"] = None
        options.update({
            "pool_size": 5,
            "max_overflow": 5,
            "pool_timeout": 10,
            "pool_recycle": 1800,
            "connect_args": connect_args,
        })
    options.update(overrides)
    eng = create_engine(url, **options)
    if is_postgres(url):
        @event.listens_for(eng, "do_connect")
        def _connect_with_retry(dialect, conn_rec, cargs, cparams):
            """Open a DBAPI connection, retrying while a scaled-to-zero Neon compute starts up."""
            for attempt, delay in enumerate((*CONNECT_RETRY_DELAYS, None), start=1):
                try:
                    return dialect.connect(*cargs, **cparams)
                except dialect.loaded_dbapi.OperationalError as exc:
                    if delay is None:
                        raise
                    log.warning("database connect attempt %d failed (%s); retrying in %.1fs",
                                attempt, type(exc).__name__, delay)
                    time.sleep(delay)
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


@contextmanager
def session_or_new(db: Session | None) -> Iterator[Session]:
    """`db` itself when the caller already has a session; otherwise a new one that is closed on the way out.

    The render helpers used to write `next(get_db())`, which never runs get_db's `finally`: the session kept its
    connection checked out until the garbage collector happened to free it (on whichever thread was running), so
    a burst of 404s from a scanner emptied the pool and every request then waited pool_timeout for a connection.
    """
    if db is not None:
        yield db
        return
    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()
