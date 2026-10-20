"""Hello New Home HQ: public site + partner portal + admin panel in one FastAPI app."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .seed import seed
from .services import kit
from .services.times import fmt_uk

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals.update({"base_url": config.BASE_URL, "Markup": Markup})
templates.env.filters["uk"] = fmt_uk          # naive UTC -> Europe/London for display
templates.env.globals["partner_name"] = kit.partner_name   # public wording, with the head-office fallback

NAV = [
    ("/to-let", "to-let", "To Let"),
    ("/landlords", "landlords", "Landlords"),
    ("/serviced-accommodation", "sa", "Short Lets"),
    ("/tenants", "tenants", "Tenants"),
    ("/areas", "areas", "Areas"),
    ("/partners-programme", "partners", "Partners"),
    ("/contact", "contact", "Contact"),
]
templates.env.globals["NAV"] = NAV

# ---- page fragments (the static site's pages, reused as-is) ----------------
_META_RE = re.compile(r"^\s*<!--(.*?)-->", re.S)
_PAGE_ROUTE = {
    # file stem -> public path. The partners *programme* page moves so that
    # /partners can be the portal.
    "index": "/",
    "partners": "/partners-programme",
}


def _rewrite_links(html: str) -> str:
    html = re.sub(r'(href|src)="assets/', r'\1="/assets/', html)
    html = html.replace('href="index.html"', 'href="/"')
    html = html.replace('href="partners.html"', 'href="/partners-programme"')
    html = re.sub(r'href="area-([a-z0-9-]+)\.html"', r'href="/areas/\1"', html)
    html = re.sub(r'href="([a-z0-9-]+)\.html(\?[^"]*)?"', lambda m: f'href="/{m.group(1)}{m.group(2) or ""}"', html)
    return html


def load_pages() -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for path in sorted((APP_DIR / "templates" / "public" / "pages").glob("*.html")):
        raw = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        m = _META_RE.match(raw)
        body = raw
        if m:
            for line in m.group(1).strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = raw[m.end():]
        stem = path.stem
        route = _PAGE_ROUTE.get(stem, f"/{stem}")
        pages[route] = {"stem": stem, "meta": meta, "body": _rewrite_links(body.strip())}
    return pages


PAGES = load_pages()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed(quiet=False)
    yield


app = FastAPI(title="Hello New Home HQ", lifespan=lifespan, docs_url=None, redoc_url=None)

# Largest request body any route accepts: the biggest upload plus a margin for the rest of the form.
# Starlette spools a whole multipart body before a handler can check its size, so the cap sits in front.
MAX_BODY_BYTES = (config.MAX_UPLOAD_MB + 1) * 1024 * 1024


class BodySizeLimit:
    """Pure ASGI middleware: refuse a request whose declared Content-Length is over MAX_BODY_BYTES."""

    def __init__(self, inner, limit: int = MAX_BODY_BYTES):
        self.inner, self.limit = inner, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            declared = next((v for k, v in scope.get("headers", []) if k == b"content-length"), b"")
            if declared.isdigit() and int(declared) > self.limit:
                body = b'{"ok": false, "error": "Request too large"}'
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.inner(scope, receive, send)


app.add_middleware(BodySizeLimit)
app.mount("/assets", StaticFiles(directory=str(APP_DIR / "static")), name="assets")

from .routers import public, portal, admin, partner_api  # noqa: E402  (import after templates/PAGES exist)

app.include_router(public.router)
app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(partner_api.router)
# Catch-all for brand pages must be registered after every other route.
app.add_api_route("/{page}", public.fragment, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)

PARTNER_API_PREFIX = "/api/partner/"


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        # A dict detail (auth.ApiError) is the body itself: {"ok": false, "reason": ..., "message": ...}; a plain
        # detail keeps the older {"ok": false, "error": ...} shape the site's own forms read.
        body = {"ok": False, **exc.detail} if isinstance(exc.detail, dict) else {"ok": False, "error": exc.detail}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 404:
        from .routers.public import render_public
        return render_public(request, "public/404.html", {"title": "Page not found"}, status_code=404)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """The Partner API answers in its own JSON shape even when FastAPI refuses a path or query value (a
    partner id that is not a number); every other route keeps FastAPI's default 422 body."""
    if request.url.path.startswith(PARTNER_API_PREFIX):
        first = next(iter(exc.errors()), {}) or {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("path", "query", "body"))
        message = f"{where}: {first.get('msg', 'invalid value')}." if where else "The request is not valid."
        return JSONResponse({"ok": False, "reason": "invalid_request", "message": message}, status_code=422,
                            headers={"Cache-Control": "no-store"})
    from fastapi.exception_handlers import request_validation_exception_handler
    return await request_validation_exception_handler(request, exc)
