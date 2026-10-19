#!/usr/bin/env sh
# Local dev: creates the SQLite DB in hq/data, prints the admin password on first run.
cd "$(dirname "$0")"
exec uvicorn app.main:app --reload --port 8010
