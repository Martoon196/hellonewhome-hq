"""Runtime configuration from environment variables (with safe dev defaults)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # hq/
CONTENT_DIR = ROOT / "content"
DATA_DIR = Path(os.environ.get("HNH_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = Path(os.environ.get("HNH_UPLOAD_DIR", ROOT / "uploads"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# A blank or whitespace-only DATABASE_URL means SQLite, the same as an unset one: Render prompts for every
# `sync: false` variable on a Blueprint sync, and an empty submission must not stop the service booting.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{DATA_DIR / 'hq.db'}"
if DATABASE_URL.startswith("postgres://"):            # Render/Heroku style URL
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    # Dev only: a random key per process means sessions reset on restart.
    SECRET_KEY = secrets.token_hex(32)
    DEV_SECRET = True
else:
    DEV_SECRET = False

BASE_URL = os.environ.get("BASE_URL", "https://hellonewhome.co.uk").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@hellonewhome.co.uk")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")      # empty => generated and printed at first seed
# FEES_CONFIRMED=1 marks the fee sheet as signed off on every boot (clears the hold notice on the partners' kit)
# without an admin click; leave it unset to manage the flag from Admin -> Settings alone.
FEES_CONFIRMED = os.environ.get("FEES_CONFIRMED", "").strip().lower() in ("1", "true", "yes")
SESSION_COOKIE = "hnh_session"
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", 60 * 60 * 24 * 14))   # 14 days
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "1" if BASE_URL.startswith("https") else "0") == "1"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 25))

# Optional lead notifications by email (SMTP). Leave blank to disable.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "lettings@hellonewhome.co.uk")
LEAD_NOTIFY_TO = os.environ.get("LEAD_NOTIFY_TO", "lettings@hellonewhome.co.uk")
