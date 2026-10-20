"""Password hashing, signed-cookie sessions and route guards."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import AuditLog, User

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="hnh-session")
_PBKDF2_ITERATIONS = 200_000


# ---- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


def generate_password(length: int = 12) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---- account emails --------------------------------------------------------
def normalise_email(email: str | None) -> str:
    """The one form an account email is stored and looked up in: trimmed and lower-cased.

    Every write to User.email and Partner.email, and every lookup by email (both login forms, the partner
    login creation and the admin user form), goes through here. A login typed with capitals or stray spaces
    therefore finds the same row, and two addresses differing only by case can never both be created. "=" is
    case-sensitive on SQLite and on Postgres alike, so the rule sits at the boundary; the two login routes
    and the two pre-insert checks in the admin router compare on lower(email), the expression behind the
    unique index ux_users_email_lower, so a row saved with capitals before this rule still logs in and a
    second login for the same address is refused politely rather than tripping the index.
    Lead and notification addresses are not accounts and are stored as typed."""
    return (email or "").strip().lower()


# ---- throttles (per process; fine for a single-instance HQ app) -----------
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 8            # failed logins per address + email
_MAX_IP_ATTEMPTS = 40        # failed logins per address, whatever the email
_WINDOW = 15 * 60


def client_ip(request: Request) -> str:
    """The caller's address. Behind Render's proxy uvicorn must run with --proxy-headers so this is the real client."""
    return request.client.host if request.client else "x"


def throttle_check(key: str, limit: int = _MAX_ATTEMPTS, window: int = _WINDOW) -> bool:
    """True if another attempt is allowed for this key within the window."""
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    return len(hits) < limit


def throttle_hit(key: str) -> None:
    _attempts.setdefault(key, []).append(time.time())


def login_keys(request: Request, email: str) -> tuple[str, str, str]:
    """(normalised email, per-address+email key, per-address key). The email is normalised exactly as the
    user lookup normalises it, so padding it with whitespace cannot buy a fresh attempt budget."""
    norm = normalise_email(email)
    ip = client_ip(request)
    return norm, f"{ip}:{norm}", f"ip:{ip}"


def login_allowed(request: Request, email: str) -> tuple[str, list[str], bool]:
    norm, key, ip_key = login_keys(request, email)
    allowed = throttle_check(key) and throttle_check(ip_key, _MAX_IP_ATTEMPTS)
    return norm, [key, ip_key], allowed


# ---- sessions --------------------------------------------------------------
def _credential_version(user: User) -> str:
    """A short fingerprint of the password hash. It changes on every password change or reset, so every
    cookie issued before that stops working at once (a stolen or forgotten session cannot ride out a reset)."""
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def set_session(response: Response, user: User) -> None:
    token = _serializer.dumps({"uid": user.id, "role": user.role, "pv": _credential_version(user)})
    response.set_cookie(
        config.SESSION_COOKIE, token, max_age=config.SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=config.SECURE_COOKIES, path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    user = db.get(User, data.get("uid"))
    if not user or not user.is_active:
        return None
    if not hmac.compare_digest(str(data.get("pv", "")), _credential_version(user)):
        return None   # password changed or reset since this cookie was issued
    return user


def _redirect(location: str) -> HTTPException:
    # FastAPI renders an HTTPException with a Location header as a redirect.
    return HTTPException(status_code=303, headers={"Location": location})


def safe_next(target: str | None, default: str) -> str:
    """A post-login destination must be a path on this site: "//evil.example" and absolute URLs fall back to the default."""
    t = (target or "").strip()
    if t.startswith("/") and not t.startswith(("//", "/\\")):
        return t
    return default


PARTNER_PAUSED_MESSAGE = "Your partner account is paused. Contact head office."
PASSWORD_PAGE = "/partners/password"


def partner_blocked(user: User) -> bool:
    """A paused or departed partner is locked out everywhere, not only at the login form."""
    return user.role == "partner" and user.partner is not None and user.partner.status in ("paused", "left")


def _must_change(request: Request, user: User) -> bool:
    return bool(user.must_change_password) and not request.url.path.startswith(PASSWORD_PAGE)


def require_partner(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise _redirect(f"/partners/login?next={request.url.path}")
    if user.role not in ("partner", "admin"):
        raise HTTPException(status_code=403, detail="Partner access only")
    if partner_blocked(user):
        raise HTTPException(status_code=403, detail=PARTNER_PAUSED_MESSAGE)
    if _must_change(request, user):
        raise _redirect(f"{PASSWORD_PAGE}?first=1")
    return user


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise _redirect(f"/admin/login?next={request.url.path}")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")
    if _must_change(request, user):
        # A generated admin password (first boot, or a login made from Admin users) must be replaced before use.
        raise _redirect(f"{PASSWORD_PAGE}?first=1")
    return user


def check_origin(request: Request) -> None:
    """CSRF guard for state-changing requests: the Origin/Referer host must match ours."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return  # non-browser client (tests, curl); cookies are SameSite=Lax anyway
    host = request.headers.get("host", "")
    from urllib.parse import urlparse
    if urlparse(origin).netloc.split(":")[0] != host.split(":")[0]:
        raise HTTPException(status_code=403, detail="Cross-site request blocked")


def audit(db: Session, user: User | None, action: str, target: str = "", detail: str = "") -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, target=target, detail=detail[:2000]))


def touch_login(db: Session, user: User) -> None:
    user.last_login = datetime.utcnow()
    db.add(user)
