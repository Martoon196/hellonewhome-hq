"""Startup schema management (app.migrations) on throwaway SQLite files, independent of the app's engine."""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import migrations
from app.db import Base, make_engine
from app.models import User

HEAD = migrations.head_revision()


def _engine(tmp_path: Path, name: str):
    return make_engine(f"sqlite:///{tmp_path / name}")


def _at_revision(eng, revision: str) -> None:
    with eng.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), revision)


def _legacy_engine(tmp_path: Path, name: str):
    """A file exactly as the pre-Alembic release left it: the baseline's tables (built here by the baseline
    script itself), no version table and no case-insensitive email index."""
    eng = _engine(tmp_path, name)
    _at_revision(eng, migrations.BASELINE_REVISION)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("DROP INDEX ux_users_email_lower"))
    assert migrations.current_revision(eng) is None
    return eng


def _columns(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_head_is_beyond_the_baseline():
    assert HEAD != migrations.BASELINE_REVISION
    assert migrations.BASELINE_TABLES < migrations.app_table_names()


def test_empty_database_is_upgraded_to_head_and_a_second_run_is_a_no_op(tmp_path):
    eng = _engine(tmp_path, "fresh.db")
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == HEAD
    assert migrations.app_table_names() <= set(inspect(eng).get_table_names())
    assert {"franchise_fee_waived", "franchise_note"} <= _columns(eng, "partners")
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == HEAD
    eng.dispose()


def test_database_at_the_baseline_is_upgraded_to_head_and_keeps_its_rows(tmp_path):
    """The live database after the Postgres move sits at 0001_baseline; the next deploy must add the Partner
    API's table and columns rather than mistake the schema for a partial one."""
    eng = _engine(tmp_path, "baseline.db")
    _at_revision(eng, migrations.BASELINE_REVISION)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO partners (name, first_name, email, phone, whatsapp, whatsapp_display, bio, photo_path, "
                          "tier, status, units_managed, notes, created_at) VALUES ('Pat Partner', '', 'pat@example.test', '', "
                          "'', '', '', '', 'area', 'active', 0, '', '2026-01-01 00:00:00')"))
    assert "api_clients" not in inspect(eng).get_table_names()
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    assert migrations.current_revision(eng) == HEAD
    assert "api_clients" in inspect(eng).get_table_names()
    with eng.connect() as conn:
        row = conn.execute(text("SELECT name, franchise_fee_waived, franchise_note FROM partners")).one()
    assert tuple(row) == ("Pat Partner", None, None)
    # and the way back exists (batch mode on SQLite, plain ALTER on Postgres)
    with eng.begin() as conn:
        command.downgrade(migrations.alembic_config(conn), migrations.BASELINE_REVISION)
    assert migrations.current_revision(eng) == migrations.BASELINE_REVISION
    assert "api_clients" not in inspect(eng).get_table_names()
    assert not {"franchise_fee_waived", "franchise_note"} & _columns(eng, "partners")
    eng.dispose()


def test_sqlite_file_built_by_create_all_is_stamped_and_gains_the_email_index(tmp_path):
    """The live file (built by the old create_all) is adopted: stamped at the baseline, the case-insensitive
    index added, the later revisions applied, and from then on every boot is an ordinary no-op upgrade."""
    eng = _legacy_engine(tmp_path, "legacy.db")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO users (email, password_hash, role, is_active, must_change_password, created_at) "
                          "VALUES ('Old.Admin@Example.test', 'x', 'admin', 1, 0, '2026-01-01 00:00:00')"))
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    assert migrations.current_revision(eng) == HEAD
    assert "api_clients" in inspect(eng).get_table_names()
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO users (email, password_hash, role, is_active, must_change_password, created_at) "
                              "VALUES ('old.admin@example.test', 'x', 'admin', 1, 0, '2026-01-01 00:00:00')"))
    assert migrations.run_migrations(eng, quiet=True) == "upgraded"
    eng.dispose()


def test_version_table_present_but_empty_still_counts_as_pre_alembic(tmp_path):
    """A hand-made or interrupted alembic_version with no row must not send the file down `upgrade head`,
    whose first create_table would fail on a table that already exists."""
    eng = _legacy_engine(tmp_path, "empty-version.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    assert migrations.current_revision(eng) == HEAD
    eng.dispose()


def test_partial_schema_is_refused_with_the_missing_tables_named(tmp_path):
    eng = _engine(tmp_path, "partial.db")
    Base.metadata.create_all(eng, tables=[User.__table__])
    with pytest.raises(migrations.SchemaStateError) as excinfo:
        migrations.run_migrations(eng, quiet=True)
    message = str(excinfo.value)
    assert "but not" in message and "leads" in message and "partners" in message
    assert "users" not in message.split("but not", 1)[1].split(", and")[0]
    assert "api_clients" not in message          # a baseline rule: later tables are added by upgrade, never demanded
    assert migrations.current_revision(eng) is None
    eng.dispose()


def test_only_sqlite_is_adopted_by_stamping(tmp_path, monkeypatch):
    """A Postgres database with the app's tables but no revision would keep the narrower pre-baseline columns if it
    were stamped, so it is refused with directions rather than adopted. Simulated by reporting a different dialect
    name for an otherwise adoptable SQLite file."""
    eng = _legacy_engine(tmp_path, "not-sqlite.db")
    monkeypatch.setattr(eng.dialect, "name", "postgresql")
    with pytest.raises(migrations.SchemaStateError, match="postgresql database has the app's tables but no Alembic"):
        migrations.run_migrations(eng, quiet=True)
    monkeypatch.undo()
    assert migrations.current_revision(eng) is None
    assert migrations.run_migrations(eng, quiet=True) == "stamped"
    assert migrations.current_revision(eng) == HEAD
    eng.dispose()
