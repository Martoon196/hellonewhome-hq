#!/usr/bin/env python
"""Copy the live SQLite database into Postgres, once.

    export NEON_DIRECT_URL='postgresql://...'        # for this shell only; it is read from the environment
    .venv/bin/python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL --dry-run
    .venv/bin/python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL

The connection string is taken from the environment variable named by --postgres-env (default
DATABASE_URL), so it never appears in the process list or the shell history; --postgres URL is
accepted as well for a throwaway local database.

Run it against a copy of the live file (or the live file while the service is stopped), before the
service is pointed at Postgres: the app seeds an empty database on its first boot, and a seeded
target is refused. What happens, in order:

1. the SQLite file is opened read-only and the Postgres database through the app's own engine
   settings (app.db.make_engine), so the same driver and URL handling apply as in production;
2. if Postgres has none of the app's tables, the schema is created with Alembic (upgrade head),
   so the target is exactly what the app would build for itself;
3. the copy stops if the target already holds rows, unless --append is given;
4. every row is checked against the model before anything is written: a string longer than its
   column, a NULL in a column that forbids it, or two logins whose emails differ only in case
   (the case-insensitive unique index would reject them) is reported with its table and primary
   key, and nothing is written;
5. every table is copied in foreign-key order, in batches, inside one transaction, so a failure
   part-way leaves the target as it was;
6. the Postgres sequences are reset so the next insert follows the highest copied id;
7. a per-table count comparison is printed, and the exit code is 0 only when every table matches.

--dry-run performs steps 1, 3 and 4, prints the counts, and neither creates the schema nor writes.
The connection string is never printed; errors are reported with it masked.

Rows that arrived in SQLite after the main copy (the service was still on the file until
DATABASE_URL was set) are brought across with a second, narrower run:

    .venv/bin/python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL \
        --only leads --after-id 123 --append

--only names the tables to copy (repeatable); --after-id N takes only rows whose integer primary key
is above N, so the ids already in Postgres are skipped and the sequences are reset again; --append is
still required because the target holds rows. Changes to rows that were already copied (a lead's
status, a password, a partner's profile) are not detected by this script and are lost at the switch
unless they are made again on Postgres, which is why DEPLOY-NEON.md keeps that window short and quiet.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import JSON, String, create_engine, func, insert, inspect, select, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app import models  # noqa: E402,F401  (registers every table on Base.metadata)
from app.db import Base, is_postgres, make_engine, normalise_url  # noqa: E402
from app.migrations import run_migrations  # noqa: E402

DEFAULT_BATCH = 500
IGNORED_SOURCE_TABLES = {"alembic_version", "sqlite_sequence", "sqlite_stat1"}


# ---- reporting ---------------------------------------------------------------
class Masker:
    """Rewrites any text so the connection string (in either spelling), its password and its host never
    appear, however a driver phrases an error."""

    def __init__(self, raw_url: str):
        self.replacements: dict[str, str] = {raw_url: "<postgres url>", normalise_url(raw_url): "<postgres url>"}
        try:
            url = make_url(normalise_url(raw_url))
        except Exception:                      # an unparsable URL is still a secret, and is masked as a whole
            url = None
        if url is not None:
            if url.password:
                self.replacements[url.password] = "***"
            if url.host and len(url.host) >= 4:
                self.replacements[url.host] = "<host>"

    def __call__(self, message: object) -> str:
        out = str(message)
        for secret in sorted((s for s in self.replacements if s), key=len, reverse=True):
            out = out.replace(secret, self.replacements[secret])
        return out


def say(message: str = "") -> None:
    print(message, flush=True)


# ---- engines ---------------------------------------------------------------
def open_sqlite(path: Path) -> Engine:
    """The source file, read-only. A WAL-mode file needs its shared-memory file; when the directory
    does not allow that in read-only mode the file is opened normally, and still nothing is written."""
    uri = path.resolve().as_uri()

    def connect_ro():
        return sqlite3.connect(f"{uri}?mode=ro", uri=True)

    try:
        probe = connect_ro()
        probe.execute("select count(*) from sqlite_master").fetchone()
        probe.close()
        return create_engine("sqlite://", creator=connect_ro)
    except sqlite3.OperationalError as exc:
        say(f"note: read-only open failed ({exc}); opening the file normally, nothing will be written to it")
        return create_engine("sqlite://", creator=lambda: sqlite3.connect(str(path.resolve())))


def model_tables() -> list:
    """Every table in Base.metadata, parents before children (so foreign keys always resolve)."""
    return list(Base.metadata.sorted_tables)


# ---- checks ----------------------------------------------------------------
def integer_key(table):
    """The table's single integer primary-key column, or None (settings is keyed by a string)."""
    pk = list(table.primary_key.columns)
    if len(pk) == 1 and pk[0].type.python_type is int:
        return pk[0]
    return None


def read_rows(source: Engine, table, columns, after_id: int | None = None) -> list[dict]:
    with source.connect() as conn:
        stmt = select(*columns).order_by(*table.primary_key.columns)
        key = integer_key(table)
        if after_id is not None and key is not None:
            stmt = stmt.where(key > after_id)
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def validate(table, rows: list[dict], columns) -> list[str]:
    """Problems Postgres would reject that SQLite let through, one line each."""
    problems: list[str] = []
    pk_names = [c.name for c in table.primary_key.columns]
    for row in rows:
        pk = ", ".join(f"{k}={row.get(k)!r}" for k in pk_names)
        for col in columns:
            value = row.get(col.name)
            if value is None:
                if not col.nullable and not isinstance(col.type, JSON):
                    problems.append(f"{table.name} ({pk}): {col.name} is NULL but the column forbids it")
                continue
            if isinstance(col.type, String) and col.type.length and isinstance(value, str) and len(value) > col.type.length:
                problems.append(f"{table.name} ({pk}): {col.name} is {len(value)} characters, "
                                f"the column allows {col.type.length}, so it would be truncated")
    if table.name == "users":
        seen: dict[str, object] = {}
        for row in rows:
            key = str(row.get("email") or "").lower()
            if key in seen:
                problems.append(f"users (id={row.get('id')!r}): email {row.get('email')!r} differs only in case "
                                f"from the login with id={seen[key]!r}; the case-insensitive unique index would reject it")
            else:
                seen[key] = row.get("id")
    return problems


def count_rows(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)


# ---- the copy ----------------------------------------------------------------
def copy_tables(target: Engine, plan: list[tuple], batch_size: int) -> None:
    """Insert every table's rows in batches, then reset the sequences, all in one transaction."""
    with target.begin() as conn:
        preparer = conn.dialect.identifier_preparer
        for table, rows, columns in plan:
            if not rows:
                continue
            keys = [c.name for c in columns]
            for start in range(0, len(rows), batch_size):
                batch = [{k: r.get(k) for k in keys} for r in rows[start:start + batch_size]]
                conn.execute(insert(table), batch)
            say(f"  {table.name:<14} {len(rows):>6} rows inserted")
        for table, rows, _ in plan:
            column = integer_key(table)
            if column is None or not column.autoincrement:
                continue
            quoted = preparer.quote(table.name)
            stmt = text(
                f"SELECT setval(pg_get_serial_sequence(:table_name, :column_name), "
                f"COALESCE(MAX({preparer.quote(column.name)}), 1), COUNT(*) > 0) FROM {quoted}"
            )
            conn.execute(stmt, {"table_name": table.name, "column_name": column.name})
        say("  sequences reset to follow the highest copied id")


# ---- main ------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the HQ SQLite database into Postgres, once.")
    parser.add_argument("--sqlite", required=True, type=Path, help="path to the SQLite file (opened read-only)")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--postgres-env", metavar="NAME", default=None,
                              help="name of the environment variable holding the Postgres URL (default DATABASE_URL); "
                                   "the string then never appears in the process list or the shell history")
    target_group.add_argument("--postgres", metavar="URL", default=None,
                              help="the Postgres URL itself, for a throwaway local database")
    parser.add_argument("--dry-run", action="store_true", help="check and count only; create nothing and write nothing")
    parser.add_argument("--append", action="store_true", help="copy even if the target already holds rows")
    parser.add_argument("--only", metavar="TABLE", action="append", default=None,
                        help="copy only this table (repeatable); the others are left as they are")
    parser.add_argument("--after-id", metavar="N", type=int, default=None,
                        help="copy only rows whose integer primary key is above N (for the rows that arrived after the main copy)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help=f"rows per insert statement (default {DEFAULT_BATCH})")
    args = parser.parse_args(argv)
    if args.postgres is None:
        name = args.postgres_env or "DATABASE_URL"
        args.postgres = os.environ.get(name, "").strip()
        if not args.postgres:
            say(f"error: the environment variable {name} is not set; export it in this shell or pass --postgres-env NAME")
            return 2
    mask = Masker(args.postgres)

    try:
        return run(args, mask)
    except SQLAlchemyError as exc:
        say(f"database error: {exc.__class__.__name__}: {mask(getattr(exc, 'orig', None) or exc)}")
        return 1
    except Exception as exc:  # noqa: BLE001  (anything else is reported masked rather than as a traceback)
        say(f"error: {exc.__class__.__name__}: {mask(exc)}")
        return 1


def run(args: argparse.Namespace, mask: Masker) -> int:
    if not args.sqlite.is_file():
        say(f"error: {args.sqlite} is not a file")
        return 2
    if not is_postgres(normalise_url(args.postgres)):
        say("error: --postgres must be a Postgres URL (postgres://, postgresql:// or postgresql+psycopg://)")
        return 2
    if args.batch < 1:
        say("error: --batch must be at least 1")
        return 2
    if args.after_id is not None and args.after_id < 0:
        say("error: --after-id must be 0 or more")
        return 2

    source = open_sqlite(args.sqlite)
    target = make_engine(args.postgres)
    tables = model_tables()
    if args.only:
        unknown = sorted(set(args.only) - {t.name for t in tables})
        if unknown:
            say(f"error: --only names tables the app does not have: {', '.join(unknown)}")
            return 2
    say(f"source: {args.sqlite}")
    say(f"target: Postgres database {make_url(target.url).database!r}" + (" (dry run)" if args.dry_run else ""))
    say(f"tables in foreign-key order: {', '.join(t.name for t in tables)}")
    if args.only:
        say(f"copying only: {', '.join(t.name for t in tables if t.name in args.only)}")
    if args.after_id is not None:
        say(f"copying only rows with an id above {args.after_id} (tables without an integer id are copied whole)")

    # Target schema -----------------------------------------------------------
    present = set(inspect(target).get_table_names())
    wanted = {t.name for t in tables}
    if not present & wanted:
        if args.dry_run:
            say("target schema: absent; the copy would create it with Alembic (upgrade head)")
        else:
            run_migrations(target, quiet=True)
            say("target schema: created with Alembic (upgrade head)")
    elif wanted - present:
        say(f"error: the target has some of the app's tables but not {', '.join(sorted(wanted - present))}; "
            "finish or drop that schema first")
        return 2
    else:
        say("target schema: present")
    schema_ready = not (wanted - set(inspect(target).get_table_names()))

    # Refuse a populated target ------------------------------------------------
    before = {t.name: (count_rows(target, t) if schema_ready else 0) for t in tables}
    populated = {name: n for name, n in before.items() if n and (not args.only or name in args.only)}
    if populated and not args.append:
        say("error: the target already holds rows: " + ", ".join(f"{k}={v}" for k, v in populated.items()))
        say("       copy into an empty database (the app seeds a database on its first boot, so copy before pointing "
            "the service at it), or pass --append to add to what is there (clashing ids will fail the copy)")
        return 3

    # Read and check the source ---------------------------------------------
    src_inspector = inspect(source)
    src_tables = set(src_inspector.get_table_names())
    for extra in sorted(src_tables - wanted - IGNORED_SOURCE_TABLES):
        say(f"note: source table {extra!r} is not part of the app and will not be copied")
    plan: list[tuple] = []
    problems: list[str] = []
    for table in tables:
        if args.only and table.name not in args.only:
            continue
        if table.name not in src_tables:
            say(f"note: source has no {table.name!r} table; it will be empty in the target")
            plan.append((table, [], list(table.columns)))
            continue
        src_cols = {c["name"] for c in src_inspector.get_columns(table.name)}
        columns = [c for c in table.columns if c.name in src_cols]
        missing = [c.name for c in table.columns if c.name not in src_cols]
        if missing:
            say(f"note: {table.name}: source lacks {', '.join(missing)}; the model defaults will be used")
        ignored = sorted(src_cols - {c.name for c in table.columns})
        if ignored:
            say(f"note: {table.name}: source columns {', '.join(ignored)} are not in the model and will not be copied")
        rows = read_rows(source, table, columns, args.after_id)
        problems.extend(validate(table, rows, columns))
        plan.append((table, rows, columns))

    say()
    say(f"{'table':<14} {'source':>8} {'target':>8} {'highest id':>11}")
    for table, rows, _ in plan:
        key = integer_key(table)
        top = max((r[key.name] for r in rows), default=None) if key is not None else None
        say(f"{table.name:<14} {len(rows):>8} {before[table.name]:>8} {'-' if top is None else top:>11}")
    say()
    if problems:
        say(f"{len(problems)} row(s) would be rejected by Postgres; nothing has been written:")
        for line in problems:
            say(f"  - {line}")
        return 4
    say("validation: every row fits its column and every login email is unique regardless of case")
    if args.dry_run:
        say("dry run: nothing written")
        return 0

    # Copy ------------------------------------------------------------------
    say()
    say("copying inside one transaction:")
    copy_tables(target, plan, args.batch)

    # Compare ---------------------------------------------------------------
    say()
    say(f"{'table':<14} {'source':>8} {'before':>8} {'after':>8}  result")
    ok = True
    for table, rows, _ in plan:
        after = count_rows(target, table)
        expected = before[table.name] + len(rows)
        match = after == expected
        ok = ok and match
        say(f"{table.name:<14} {len(rows):>8} {before[table.name]:>8} {after:>8}  {'ok' if match else 'MISMATCH'}")
    say()
    say("copy complete: every table matches" if ok else "copy finished with a count mismatch; inspect the target before use")
    source.dispose()
    target.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
