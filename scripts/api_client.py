#!/usr/bin/env python
"""Manage the Partner API's clients (the server-to-server callers of /api/partner).

    .venv/bin/python scripts/api_client.py create grouphq partners:write     # prints the secret once
    .venv/bin/python scripts/api_client.py rotate grouphq                    # new secret, printed once
    .venv/bin/python scripts/api_client.py disable grouphq                   # refused at the next request
    .venv/bin/python scripts/api_client.py enable grouphq
    .venv/bin/python scripts/api_client.py list

The database is whatever DATABASE_URL names (the SQLite file under HNH_DATA_DIR when it is unset), so
on Render run it from the service shell. Only the SHA-256 of a secret is stored: the secret is printed
to standard output exactly once, by create and rotate, and cannot be shown again. Pending migrations
are applied first, as the app does when it starts.

The alternative to this script for Group HQ is the environment variable HNH_PARTNER_TOKEN_GROUPHQ: when
it is set, every boot makes sure a client named grouphq with the partners:write scope carries its hash.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth import (API_SCOPES, create_api_client, rotate_api_client_token, validate_api_client_name)  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.models import ApiClient  # noqa: E402


def _client(db, name: str) -> ApiClient:
    client = db.query(ApiClient).filter(ApiClient.name == validate_api_client_name(name)).first()
    if client is None:
        raise ValueError(f"no API client is named {name}")
    return client


def _when(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "never"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, rotate, enable, disable and list Partner API clients.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a client and print its secret once")
    create.add_argument("name", help="the client name, sent as X-Partner-Client (for example grouphq)")
    create.add_argument("scopes", help=f"comma-separated scopes from: {', '.join(API_SCOPES)}")
    for verb, help_text in (("rotate", "replace the client's secret and print the new one once"),
                            ("disable", "refuse the client from the next request"),
                            ("enable", "accept the client again")):
        sub.add_parser(verb, help=help_text).add_argument("name")
    sub.add_parser("list", help="show every client, without secrets")
    args = parser.parse_args(argv)

    run_migrations(quiet=True)
    db = SessionLocal()
    try:
        if args.command == "create":
            client, token = create_api_client(db, args.name, args.scopes)
            db.commit()
            print(f"API client {client.name} created with scopes {', '.join(client.scopes)}.")
            print("Its secret, shown once and never stored:")
            print(token)
            return 0
        if args.command == "list":
            rows = db.query(ApiClient).order_by(ApiClient.name).all()
            if not rows:
                print("No API clients.")
                return 0
            for c in rows:
                print(f"{c.name:<24} {'active' if c.active else 'disabled':<9} scopes {', '.join(c.scopes or []) or '-':<28} "
                      f"created {_when(c.created_at)}  last used {_when(c.last_used_at)}")
            return 0
        client = _client(db, args.name)
        if args.command == "rotate":
            token = rotate_api_client_token(db, client)
            db.commit()
            print(f"API client {client.name} has a new secret; the old one no longer works. Shown once and never stored:")
            print(token)
            return 0
        client.active = args.command == "enable"
        db.add(client)
        db.commit()
        print(f"API client {client.name} is now {'enabled' if client.active else 'disabled'}.")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
