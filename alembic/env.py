"""Alembic environment.

The target is `app.db.Base.metadata` and the database is whatever `app.config.DATABASE_URL`
names (SQLite by default, Postgres when the variable is set), so the command-line tool and the
app's own startup migration always look at the same thing.

Two ways in:

* from the command line (`alembic upgrade head`, `alembic revision --autogenerate`): the app's
  engine is used, which carries the SQLite pragmas or the Postgres pool settings;
* from `app.migrations.run_migrations`, which passes an open connection through
  `config.attributes["connection"]` and asks for the logging configuration to be left alone.
"""
from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.engine import Connection

from app import db as app_db
from app import models  # noqa: F401  (importing the module registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = app_db.Base.metadata


def compare_type(ctx, inspected_column, metadata_column, inspected_type, metadata_type):
    """Column-type comparison for autogenerate. SQLite ignores the length of a VARCHAR, so a String
    whose only difference is its length is the same column there: the live file and any developer's
    data/hq.db were built before the baseline widened several lengths and are adopted by stamping, and
    without this rule the first autogenerate against such a file would propose fourteen table rebuilds
    that change nothing. Postgres enforces the length, so there the default comparison applies.
    Returning None asks Alembic for its own comparison; False means "no change"."""
    if (ctx.dialect.name == "sqlite"
            and isinstance(inspected_type, sa.String) and isinstance(metadata_type, sa.String)
            and inspected_type._type_affinity is metadata_type._type_affinity):
        return False
    return None


def _options(dialect_name: str) -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": compare_type,
        "compare_server_default": True,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the table instead.
        "render_as_batch": dialect_name == "sqlite",
    }


def run_migrations_offline() -> None:
    """Emit the SQL for a migration without connecting (alembic upgrade head --sql)."""
    url = app_db.DATABASE_URL
    dialect_name = "sqlite" if app_db.is_sqlite(url) else "postgresql"
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_options(dialect_name))
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, **_options(connection.dialect.name))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    with app_db.engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
