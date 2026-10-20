"""Startup schema management (app.migrations) on throwaway SQLite files, independent of the app's engine."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import migrations
from app.db import Base, make_engine
from app.models import User


def _engine(tmp_path: Path, name: str):
    return make_engine(f"sqlite:///{tmp_path / name}")


def test_empty_database_is_upgraded_to_head_and_a_second_run_is_a_no_op(tmp_path):
    eng = _engine(tmp_path, "fresh.db")
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == migrations.BASELINE_REVISION
    assert migrations.app_table_names() <= set(inspect(eng).get_table_names())
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == migrations.BASELINE_REVISION
    eng.dispose()


def test_sqlite_file_built_by_create_all_is_stamped_and_gains_the_email_index(tmp_path):
    """The live file (built by the old create_all) is adopted: stamped at the baseline, the case-insensitive
    index added, and from then on every boot is an ordinary no-op upgrade."""
    eng = _engine(tmp_path, "legacy.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO users (email, password_hash, role, is_active, must_change_password, created_at) "
                          "VALUES ('Old.Admin@Example.test', 'x', 'admin', 1, 0, '2026-01-01 00:00:00')"))
    assert migrations.current_revision(eng) is None
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    assert migrations.current_revision(eng) == migrations.BASELINE_REVISION
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO users (email, password_hash, role, is_active, must_change_password, created_at) "
                              "VALUES ('old.admin@example.test', 'x', 'admin', 1, 0, '2026-01-01 00:00:00')"))
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    eng.dispose()


def test_version_table_present_but_empty_still_counts_as_pre_alembic(tmp_path):
    """A hand-made or interrupted alembic_version with no row must not send the file down `upgrade head`,
    whose first create_table would fail on a table that already exists."""
    eng = _engine(tmp_path, "empty-version.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    assert migrations.current_revision(eng) == migrations.BASELINE_REVISION
    eng.dispose()


def test_partial_schema_is_refused_with_the_missing_tables_named(tmp_path):
    eng = _engine(tmp_path, "partial.db")
    Base.metadata.create_all(eng, tables=[User.__table__])
    with pytest.raises(migrations.SchemaStateError) as excinfo:
        migrations.run_migrations(eng, quiet=True)
    message = str(excinfo.value)
    assert "but not" in message and "leads" in message and "partners" in message
    assert "users" not in message.split("but not", 1)[1].split(", and")[0]
    assert migrations.current_revision(eng) is None
    eng.dispose()


def test_only_sqlite_is_adopted_by_stamping(tmp_path, monkeypatch):
    """A Postgres database with the app's tables but no revision would keep the narrower pre-baseline columns if it
    were stamped, so it is refused with directions rather than adopted. Simulated by reporting a different dialect
    name for an otherwise adoptable SQLite file."""
    eng = _engine(tmp_path, "not-sqlite.db")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(eng.dialect, "name", "postgresql")
    with pytest.raises(migrations.SchemaStateError, match="postgresql database has the app's tables but no Alembic"):
        migrations.run_migrations(eng, quiet=True)
    monkeypatch.undo()
    assert migrations.current_revision(eng) is None
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    eng.dispose()
