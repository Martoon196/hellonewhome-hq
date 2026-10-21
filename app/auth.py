"""Password hashing, signed-cookie sessions, route guards and the Partner API's client secrets."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import ApiClient, AuditLog, User, now

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


# ---- Partner API clients ---------------------------------------------------
# A server-to-server caller (Group HQ first) holds a secret; the database holds only its SHA-256. The
# caller names itself in X-Partner-Client and sends the secret in X-Service-Token (or as a bearer token),
# and require_api_client compares the hash of what was sent with the stored one in constant time.
API_SCOPES = ("partners:read", "partners:write", "properties:read", "properties:write")
API_CLIENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,59}$")
API_TOKEN_BYTES = 36                   # secrets.token_urlsafe(36) is 48 characters
API_TOKEN_MIN_LENGTH = 32              # the shortest secret the bootstrap variable is allowed to carry
API_TOKEN_MAX_LENGTH = 200             # anything longer is refused before it is hashed
API_FAIL_LIMIT = 30                    # refused credentials per address before the address is answered 429
API_FAIL_WINDOW = 60 * 60              # for this long (seconds); the same per-process throttle the login forms use
TOKEN_HEADER = "x-service-token"
CLIENT_HEADER = "x-partner-client"


def generate_api_token() -> str:
    """A fresh 48-character URL-safe secret. Shown once by the CLI, never stored."""
    return secrets.token_urlsafe(API_TOKEN_BYTES)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_NO_CLIENT_HASH = hash_api_token("")   # compared against when no row matches, so a miss costs the same time as a hit


def normalise_scopes(scopes) -> list[str]:
    """A clean, ordered scope list; raises ValueError naming any scope the API does not know."""
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    out: list[str] = []
    unknown: list[str] = []
    for raw in scopes or []:
        scope = str(raw).strip().lower()
        if not scope:
            continue
        if scope not in API_SCOPES:
            unknown.append(scope)
        elif scope not in out:
            out.append(scope)
    if unknown:
        raise ValueError(f"unknown scope{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
                         f"Known scopes: {', '.join(API_SCOPES)}.")
    return out


def scope_allowed(granted, scope: str) -> bool:
    """True when `scope` is in the client's list; a write scope also covers the read scope of the same name."""
    held = {str(s).strip().lower() for s in (granted or []) if isinstance(s, str)}
    if scope in held:
        return True
    name, _, verb = scope.partition(":")
    return verb == "read" and f"{name}:write" in held


def validate_api_client_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if not API_CLIENT_NAME_RE.match(cleaned):
        raise ValueError("a client name is 2 to 60 characters: lower-case letters, digits, hyphens and underscores, "
                         "starting with a letter or digit")
    return cleaned


def create_api_client(db: Session, name: str, scopes, *, token: str | None = None) -> tuple[ApiClient, str]:
    """Create a client and return it with its secret, the only time the secret exists in clear. The caller
    (the CLI, or the seed's bootstrap) decides what to do with the string; nothing here logs it."""
    name = validate_api_client_name(name)
    if db.query(ApiClient).filter(ApiClient.name == name).first() is not None:
        raise ValueError(f"an API client named {name} already exists")
    token = token or generate_api_token()
    client = ApiClient(name=name, token_hash=hash_api_token(token), scopes=normalise_scopes(scopes), active=True)
    db.add(client)
    db.flush()
    return client, token


def rotate_api_client_token(db: Session, client: ApiClient, *, token: str | None = None) -> str:
    """Replace the client's secret; the old one stops working at the next request."""
    token = token or generate_api_token()
    client.token_hash = hash_api_token(token)
    db.add(client)
    return token


def ensure_api_client(db: Session, name: str, scopes, token: str) -> tuple[ApiClient, str]:
    """The bootstrap behind HNH_PARTNER_TOKEN_<NAME>: make sure a client with this name carries the hash of
    `token` and at least these scopes. Returns the client and what happened: "created", "rotated",
    "scoped" or "unchanged". The active flag is left alone on an existing row, so a client disabled with
    scripts/api_client.py stays disabled across boots until it is enabled again."""
    name = validate_api_client_name(name)
    wanted = normalise_scopes(scopes)
    client = db.query(ApiClient).filter(ApiClient.name == name).first()
    if client is None:
        client, _ = create_api_client(db, name, wanted, token=token)
        return client, "created"
    outcome = "unchanged"
    if not hmac.compare_digest(client.token_hash or "", hash_api_token(token)):
        rotate_api_client_token(db, client, token=token)
        outcome = "rotated"
    merged = normalise_scopes(list(client.scopes or []) + wanted)
    if merged != list(client.scopes or []):
        client.scopes = merged
        db.add(client)
        outcome = outcome if outcome != "unchanged" else "scoped"
    return client, outcome


class ApiError(HTTPException):
    """A JSON error for the Partner API: {"ok": false, "reason": <short code>, "message": <sentence>}.
    The reason is what a connector keys its handling on; the message is for the person reading its log."""

    def __init__(self, status_code: int, reason: str, message: str, *, headers: dict | None = None, **extra):
        super().__init__(status_code=status_code, detail={"reason": reason, "message": message, **extra}, headers=headers)


def api_throttle_key(request: Request) -> str:
    """The per-address key refused Partner API credentials are counted under."""
    return f"api:{client_ip(request)}"


def presented_api_token(request: Request) -> str:
    """The secret the caller sent: X-Service-Token, or the bearer token of an Authorization header."""
    token = (request.headers.get(TOKEN_HEADER) or "").strip()
    if token:
        return token
    scheme, _, value = (request.headers.get("authorization") or "").strip().partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def require_api_client(scope: str):
    """Dependency factory: the request must come from an active API client that holds `scope`.

    In order: any Origin header is refused (a browser is never a client of this API, whatever it carries);
    an address that has had API_FAIL_LIMIT credentials refused within API_FAIL_WINDOW is answered 429
    whatever it sends; the secret must be present; X-Partner-Client must name a client; the hash of the
    presented secret must equal that client's stored hash (hmac.compare_digest, against a fixed dummy hash
    when the name is unknown so the two misses take the same time); the client must be active and hold the
    scope. Every 401 counts against the address. The secret is never logged, stored or echoed, and no
    message says which of the name and secret was wrong. On success last_used_at is written and the
    ApiClient row is returned."""
    if scope not in API_SCOPES:
        raise ValueError(f"unknown API scope {scope!r}")
    unauthorised = {"WWW-Authenticate": 'Bearer realm="partner-api"'}

    def refused(request: Request, reason: str, message: str) -> ApiError:
        throttle_hit(api_throttle_key(request))
        return ApiError(401, reason, message, headers=unauthorised)

    def dependency(request: Request, db: Session = Depends(get_db)) -> ApiClient:
        if request.headers.get("origin") is not None:
            raise ApiError(403, "origin_not_allowed",
                           "This endpoint is for server-to-server calls only; requests carrying an Origin header are refused.")
        if not throttle_check(api_throttle_key(request), API_FAIL_LIMIT, API_FAIL_WINDOW):
            raise ApiError(429, "too_many_attempts", "Too many refused credentials from this address; try again in an hour.",
                           headers={"Retry-After": str(API_FAIL_WINDOW)})
        token = presented_api_token(request)
        if not token:
            raise refused(request, "missing_token", "Send the client secret in the X-Service-Token header or as a bearer token.")
        name = (request.headers.get(CLIENT_HEADER) or "").strip().lower()
        if not name:
            raise refused(request, "missing_client", "Send the client name in the X-Partner-Client header.")
        if len(token) > API_TOKEN_MAX_LENGTH:
            raise refused(request, "invalid_client", "The client name and secret were not recognised.")
        client = db.query(ApiClient).filter(ApiClient.name == name).first() if API_CLIENT_NAME_RE.match(name) else None
        stored = client.token_hash if client is not None else _NO_CLIENT_HASH
        if not hmac.compare_digest(stored or "", hash_api_token(token)) or client is None:
            raise refused(request, "invalid_client", "The client name and secret were not recognised.")
        if not client.active:
            raise refused(request, "client_inactive", "This API client has been disabled.")
        if not scope_allowed(client.scopes, scope):
            raise ApiError(403, "scope_missing", f"This client does not have the {scope} scope.")
        client.last_used_at = now()
        db.add(client)
        db.commit()
        request.state.api_client = client
        return client

    return dependency
