"""Schema management at startup: Alembic, driven from inside the app.

`run_migrations()` is called by `seed()` before any row is read or written. It looks at the
database and does one of two things:

* a SQLite file that carries the app's tables but no Alembic revision was built by the old
  `Base.metadata.create_all` (the live file on the Render disk, or a developer's `data/hq.db`).
  Its tables are the baseline's tables, so the baseline revision is stamped rather than re-run,
  and the one object the baseline adds on top of that schema (the case-insensitive unique index
  on users.email) is created if it is missing. Only SQLite is adopted this way: it ignores the
  String lengths the baseline widened, so the adopted file and a file the baseline built behave
  identically. Postgres would keep the narrower columns, so a Postgres database that has the
  app's tables but no revision is refused with an explanation (drop the schema and copy again
  with scripts/copy_sqlite_to_pg.py, or widen the columns by hand and stamp it);
* anything else (an empty database, or one already under Alembic): `upgrade head`.

A database with only some of the app's tables is refused as well, since `upgrade head` would
stop at the first table that already exists and the boot would end in a traceback rather than
a reason. Both paths are idempotent, so every boot and every call to `seed()` may run them. The
same scripts are what `alembic upgrade head` applies from the command line (see alembic.ini).
"""
from __future__ import annotations

import sys

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from . import config
from . import models  # noqa: F401  (registers every table on Base.metadata)
from .db import Base
from .db import engine as default_engine

ALEMBIC_INI = config.ROOT / "alembic.ini"
SCRIPT_LOCATION = config.ROOT / "alembic"
VERSION_TABLE = "alembic_version"
BASELINE_REVISION = "0001_baseline"
# Created by the baseline migration; the stamp path adds it to a schema that create_all built.
EMAIL_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower ON users (lower(email))"


class SchemaStateError(RuntimeError):
    """The database is in a state the app will not guess about; the message says what to do."""


def alembic_config(connection: Connection | None = None) -> Config:
    """The Alembic configuration the app uses, optionally bound to an already open connection."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.attributes["configure_logger"] = False      # env.py must not reconfigure the app's logging
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def app_table_names() -> set[str]:
    return {t.name for t in Base.metadata.sorted_tables}


def current_revision(bind: Engine) -> str | None:
    """The revision recorded in alembic_version, or None when the table is absent or empty."""
    with bind.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_pre_alembic(connection: Connection) -> bool:
    """True for a database that create_all built and Alembic has never seen: every one of the app's
    tables is there and no revision is recorded (the version table is absent or empty)."""
    names = set(inspect(connection).get_table_names())
    if not app_table_names() <= names:
        return False
    return MigrationContext.configure(connection).get_current_revision() is None


def missing_tables(connection: Connection) -> set[str]:
    """The app tables that are absent when some, but not all, of them exist; empty otherwise."""
    present = set(inspect(connection).get_table_names()) & app_table_names()
    return (app_table_names() - present) if present and present != app_table_names() else set()


def _ensure_email_index(bind: Engine) -> None:
    """Add the baseline's functional unique index to an adopted schema. Two existing logins whose
    addresses differ only in case would make it impossible; that is reported, not fatal."""
    try:
        with bind.begin() as connection:
            connection.execute(text(EMAIL_INDEX_SQL))
    except SQLAlchemyError as exc:
        print(f"[migrations] could not create ux_users_email_lower on the adopted database: {exc.__class__.__name__}: "
              f"{str(exc.orig) if getattr(exc, 'orig', None) else exc}", file=sys.stderr)


def run_migrations(bind: Engine | None = None, *, quiet: bool = False) -> str:
    """Bring `bind` (the app's engine by default) to the current head. Returns "stamped" when an
    existing pre-Alembic SQLite database was adopted, "upgraded" when scripts ran (or nothing was
    needed). Raises SchemaStateError for a database the app must not guess about."""
    bind = bind or default_engine
    dialect = bind.dialect.name
    before = current_revision(bind)
    with bind.connect() as connection:
        missing = missing_tables(connection)
        adopt = not missing and is_pre_alembic(connection)
    if missing:
        raise SchemaStateError(
            f"the database has some of the app's tables but not {', '.join(sorted(missing))}, and no Alembic "
            "revision; finish or drop that schema before starting the app")
    if adopt and dialect != "sqlite":
        raise SchemaStateError(
            f"the {dialect} database has the app's tables but no Alembic revision; an older release built it with "
            "create_all and it would keep its narrower columns if adopted. Drop the schema and copy the data again "
            "with scripts/copy_sqlite_to_pg.py, or widen the columns to match alembic/versions/20261020_0001_baseline.py "
            "and run `alembic stamp 0001_baseline`")
    if adopt:
        with bind.begin() as connection:
            command.stamp(alembic_config(connection), BASELINE_REVISION)
        _ensure_email_index(bind)
        outcome = "stamped"
    else:
        with bind.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
        outcome = "upgraded"
    after = current_revision(bind)
    if not quiet and after != before:
        print(f"[migrations] schema {outcome}: {before or 'no revision'} -> {after}", file=sys.stderr)
    return outcome
