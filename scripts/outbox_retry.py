#!/usr/bin/env python
"""Resend what the outbox still owes Ordris (the instructions marked instructed whose send failed or waited).

    .venv/bin/python scripts/outbox_retry.py            # send every pending or failed row with attempts left
    .venv/bin/python scripts/outbox_retry.py --list     # show the queue without sending anything
    .venv/bin/python scripts/outbox_retry.py --all      # include rows that have used up their attempts

The database is whatever DATABASE_URL names (the SQLite file under HNH_DATA_DIR when it is unset), so on
Render run it from the service shell. ORDRIS_BASE_URL and ORDRIS_PARTNER_TOKEN_HNH must be set in the same
shell; with either missing nothing is attempted and every row keeps its place. Only outcomes are printed:
never a token, never a request header. Exit status 0 when nothing remains unsent, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.services import ordris_sync  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resend outbox rows to Ordris.")
    parser.add_argument("--list", action="store_true", help="show the queue without sending")
    parser.add_argument("--all", action="store_true", help=f"also resend rows past {ordris_sync.MAX_ATTEMPTS} attempts")
    parser.add_argument("--limit", type=int, default=50, help="rows to handle in one run (default 50)")
    args = parser.parse_args(argv)

    run_migrations(quiet=True)
    db = SessionLocal()
    try:
        rows = ordris_sync.retryable(db, limit=args.limit, include_exhausted=args.all)
        if not rows:
            print("Outbox: nothing waiting for Ordris.")
            return 0
        if args.list:
            for row in rows:
                print(f"#{row.id:<5} {row.target:<20} {row.state:<8} attempts {row.attempts:<2} created {ordris_sync.fmt_when(row.created_at)}"
                      + (f"  last error: {row.last_error}" if row.last_error else ""))
            return 1
        if not ordris_sync.configured():
            print(ordris_sync.NOT_CONFIGURED, file=sys.stderr)
            print(f"{len(rows)} row{'s' if len(rows) != 1 else ''} left waiting.")
            return 1
        remaining = 0
        for row in rows:
            sent, message = ordris_sync.deliver(db, row)
            print(f"#{row.id} {row.target}: {'sent' if sent else 'not sent'} ({message})")
            remaining += 0 if sent else 1
        print(f"Done: {len(rows) - remaining} sent, {remaining} still waiting.")
        return 0 if remaining == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
