"""Homes to rent on the area pages, read from Ordris (plan, Phase 6 C5).

Ordris publishes the marketed properties of Hello New Home's own organisation, by postcode outward code,
with nothing that identifies a person or a door:

    GET {ORDRIS_BASE_URL}/api/partner/hnh/listings?outward=SA1,SA2
    Authorization: Bearer <ORDRIS_PARTNER_TOKEN_HNH>      (the only header Ordris reads; sent once)
    X-Partner-Client: hnh
    -> {"ok": true, "listings": [{"id", "outward", "bedrooms", "property_type", "rent_pcm", "available_from",
                                  "photos": ["https://..."]}]}

The same base URL, secret and client name as the sender in ordris_sync.py, and the same rules: the secret is
read from config at call time and never stored, logged or put in a message; the page never sees an exception
from here. /areas/{slug}/lettings asks for the area's outward codes and shows a "Homes to rent" block when the
answer has something in it; with Ordris unconfigured, or an empty or malformed answer, the page is exactly as
it was without this module.

The cache is a small in-process dictionary keyed by the sorted outward set, held by the one uvicorn process
the service runs as (one worker on Render; a second worker or instance would keep its own copy and refresh on
its own clock, which is harmless, so nothing is shared through the database). An answer is served for an hour
(FRESH_SECONDS), then refreshed on the next page view. When a refresh fails, the last good answer is kept and
served for up to a day from the time it was fetched (STALE_SECONDS) and a warning is logged; the next attempt
waits RETRY_SECONDS so a page view never pays the five-second timeout more than once every few minutes while
Ordris is down. A well-formed HTTP 200 whose body is not the shape above counts as an empty answer, not a
failure, and is cached like any other: a feed that has changed shape cannot slow the public site down.

Whitelisting: fetch_listings() returns dictionaries with exactly FIELDS, each value coerced to the type the
feed promises, and anything else the feed sends is dropped before it is cached. for_template() then builds the
view the page renders field by field from those, so no key the feed might add can ever reach a template.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from .. import config
from ..models import now
from .postcodes import _OUTWARD

log = logging.getLogger("hnh.ordris.listings")

CLIENT = "hnh"
PATH = "/api/partner/hnh/listings"
TIMEOUT = 5.0
USER_AGENT = "HelloNewHomeHQ/1.0 (+https://hellonewhome.co.uk)"
FRESH_SECONDS = 60 * 60              # how long an answer is served before it is refreshed
STALE_SECONDS = 24 * 60 * 60         # how long a last good answer is served while refreshes fail
RETRY_SECONDS = 5 * 60               # how long after a failed refresh the next one is attempted
MAX_OUTWARDS_PER_REQUEST = 60        # Ordris's limit per call; a larger area is asked in batches
MAX_LISTINGS = 60                    # listings kept per outward set
MAX_PHOTOS = 12                      # photo URLs kept per listing
MAX_URL = 2000
FIELDS = ("id", "outward", "bedrooms", "property_type", "rent_pcm", "available_from", "photos")
VIEW_FIELDS = ("outward", "bedrooms", "property_type", "rent", "available", "photo", "alt")
ERROR_LIMIT = 300                    # characters of an error kept for the admin status line
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# Ordris sends its portal property type when the agent set one, else its own subtype or type; the feed never
# invents a value, so a token it has not named is shown with its underscores replaced and its first letter raised.
PROPERTY_TYPE_LABELS = {
    "single_let": "House", "hmo": "House share", "residential": "Home", "flat": "Flat", "apartment": "Apartment",
    "studio": "Studio", "maisonette": "Maisonette", "bungalow": "Bungalow", "detached": "Detached house",
    "semi_detached": "Semi-detached house", "terraced": "Terraced house", "end_terrace": "End-terrace house",
    "town_house": "Town house", "cottage": "Cottage", "room": "Room", "office": "Office", "retail": "Retail unit",
    "industrial": "Industrial unit", "commercial": "Commercial unit", "block": "Block",
}
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


class ListingsError(Exception):
    """Ordris could not be read: a network error, a timeout or a non-200 answer. The message carries the status
    and Ordris's short reason, or the error's class name and text, never a header."""


@dataclass
class _Entry:
    listings: list[dict]
    fetched_at: float                 # clock() when the listings were read
    fetched_wall: datetime            # the same moment as naive UTC, for the admin status line
    next_attempt: float               # clock() before which no refresh is tried
    stale: bool = False               # True once a refresh has failed and the listings are the last good answer


@dataclass
class _Status:
    last_refreshed: datetime | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    entries: dict[tuple[str, ...], _Entry] = field(default_factory=dict)


_lock = threading.Lock()
_state = _Status()
clock = time.monotonic                # tests replace this to move the cache through its windows


def configured() -> bool:
    return bool(config.ORDRIS_BASE_URL and config.ORDRIS_PARTNER_TOKEN_HNH)


def reset() -> None:
    """Forget every cached answer and the status (tests, and a deliberate restart of the cache)."""
    global _state
    with _lock:
        _state = _Status()


def status() -> dict:
    """What the admin status line shows: when listings were last refreshed on this instance, how many outward
    sets are cached (and how many of those are stale), and the last problem with its time."""
    with _lock:
        entries = list(_state.entries.values())
        return {
            "configured": configured(),
            "last_refreshed": _state.last_refreshed,
            "cached": len(entries),
            "stale": sum(1 for e in entries if e.stale),
            "last_error": _state.last_error,
            "last_error_at": _state.last_error_at,
        }


def outward_key(outwards) -> tuple[str, ...]:
    """The cache key: the real outward codes among `outwards` (an area may also list whole postcode areas such as
    "SE" or "TA", which Ordris cannot look up and which are left out), upper-cased, de-duplicated and sorted."""
    codes = set()
    for raw in outwards if isinstance(outwards, (list, tuple, set)) else []:
        code = str(raw or "").strip().upper().replace(" ", "")
        if code and _OUTWARD.match(code):
            codes.add(code)
    return tuple(sorted(codes))


def http_client() -> httpx.Client:
    """The HTTP client the reader uses. Tests replace this with one built on httpx.MockTransport."""
    return httpx.Client(timeout=TIMEOUT)


def _headers() -> dict:
    token = config.ORDRIS_PARTNER_TOKEN_HNH
    return {"Authorization": f"Bearer {token}", "X-Partner-Client": CLIENT,
            "User-Agent": USER_AGENT, "Accept": "application/json"}


def _clip(text: str, limit: int = ERROR_LIMIT) -> str:
    return " ".join(str(text or "").split())[:limit]


def _failure_message(response: httpx.Response) -> str:
    reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            reason = " ".join(str(body.get(k)) for k in ("reason", "message") if body.get(k))
    except ValueError:
        reason = response.text[:120]
    return _clip(f"Ordris answered {response.status_code}" + (f": {reason}" if reason else ""))


def request_listings(outwards: tuple[str, ...]):
    """One call to Ordris for these outward codes (at most MAX_OUTWARDS_PER_REQUEST): the decoded JSON body, or
    ListingsError. Tests replace this function, or http_client(), to answer without a network."""
    url = config.ORDRIS_BASE_URL + PATH
    try:
        with http_client() as client:
            response = client.get(url, params={"outward": ",".join(outwards)}, headers=_headers())
    except httpx.HTTPError as exc:
        # str(exc) names the URL at most; the headers, and so the secret, are never part of it.
        raise ListingsError(_clip(f"Could not reach Ordris: {exc.__class__.__name__}: {exc}")) from None
    if response.status_code != 200:
        raise ListingsError(_failure_message(response))
    try:
        return response.json()
    except ValueError:
        return None                   # a 200 that is not JSON: an empty answer, as any other malformed body


def _int_or_none(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 100 else None
    if isinstance(value, float) and value.is_integer():
        return _int_or_none(int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return _int_or_none(int(value.strip()))
    return None


def _rent_or_none(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return number if 0 < number < 10_000_000 and number == number else None


def _text_or_none(value, limit: int) -> str | None:
    if isinstance(value, str):
        text = _clip(value, limit)
        return text or None
    return None


def clean_listing(item) -> dict | None:
    """One listing with exactly FIELDS, each coerced to the type the feed promises, or None when the item is not
    a listing at all (not an object, or no usable outward code). Every other key the feed sends is dropped here."""
    if not isinstance(item, dict):
        return None
    outward = str(item.get("outward") or "").strip().upper().replace(" ", "")
    if not _OUTWARD.match(outward):
        return None
    raw_id = item.get("id")
    listing_id = str(raw_id) if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
    photos = []
    for url in item.get("photos") if isinstance(item.get("photos"), list) else []:
        if isinstance(url, str) and url.startswith("https://") and len(url) <= MAX_URL and not any(c.isspace() for c in url):
            photos.append(url)
        if len(photos) >= MAX_PHOTOS:
            break
    return {
        "id": listing_id[:40],
        "outward": outward,
        "bedrooms": _int_or_none(item.get("bedrooms")),
        "property_type": _text_or_none(item.get("property_type"), 40) or "",
        "rent_pcm": _rent_or_none(item.get("rent_pcm")),
        "available_from": _text_or_none(item.get("available_from"), 60),
        "photos": photos,
    }


def parse_answer(body, outwards: tuple[str, ...] = ()) -> list[dict]:
    """The listings in Ordris's answer, cleaned; anything but {"ok": true, "listings": [...]} is an empty list.
    A listing whose outward code was not asked for is left out, so a cached set never shows another area's homes."""
    if not isinstance(body, dict) or body.get("ok") is not True or not isinstance(body.get("listings"), list):
        return []
    wanted = set(outwards)
    listings = []
    for item in body["listings"]:
        cleaned = clean_listing(item)
        if cleaned is None or (wanted and cleaned["outward"] not in wanted):
            continue
        listings.append(cleaned)
        if len(listings) >= MAX_LISTINGS:
            break
    return listings


def _refresh(key: tuple[str, ...]) -> list[dict]:
    listings: list[dict] = []
    for start in range(0, len(key), MAX_OUTWARDS_PER_REQUEST):
        batch = key[start:start + MAX_OUTWARDS_PER_REQUEST]
        listings.extend(parse_answer(request_listings(batch), batch))
    return listings[:MAX_LISTINGS]


def _copy(listings: list[dict]) -> list[dict]:
    return [dict(item, photos=list(item["photos"])) for item in listings]


def fetch_listings(outwards: list[str]) -> list[dict]:
    """The homes to rent in these outward codes: the cached answer while it is fresh, else a refresh from Ordris,
    else the last good answer while it is within a day of being fetched, else nothing. Never raises; with Ordris
    unconfigured nothing is attempted or cached."""
    if not configured():
        return []
    key = outward_key(outwards)
    if not key:
        return []
    moment = clock()
    with _lock:
        entry = _state.entries.get(key)
        if entry is not None and (moment - entry.fetched_at < FRESH_SECONDS or moment < entry.next_attempt):
            return _copy(entry.listings)
    try:
        listings = _refresh(key)
    except Exception as exc:                      # noqa: BLE001  (every failure is one warning, never a 500)
        message = str(exc) if isinstance(exc, ListingsError) else _clip(f"{exc.__class__.__name__}: {exc}")
        with _lock:
            _state.last_error, _state.last_error_at = message, now()
            entry = _state.entries.get(key)
            if entry is not None and moment - entry.fetched_at < STALE_SECONDS:
                entry.stale = True
                entry.next_attempt = moment + RETRY_SECONDS
                kept = _copy(entry.listings)
                age_minutes = int((moment - entry.fetched_at) // 60)
            else:
                # No answer worth keeping: remember the failure so the next page view does not try again at once.
                _state.entries[key] = _Entry(listings=[], fetched_at=moment - STALE_SECONDS, fetched_wall=now(),
                                             next_attempt=moment + RETRY_SECONDS, stale=True)
                kept, age_minutes = [], None
        if age_minutes is None:
            log.warning("ordris listings: refresh failed for %s outward code%s and no earlier answer is kept: %s",
                        len(key), "" if len(key) == 1 else "s", message)
        else:
            log.warning("ordris listings: refresh failed for %s outward code%s; serving the answer fetched %s minutes "
                        "ago: %s", len(key), "" if len(key) == 1 else "s", age_minutes, message)
        return kept
    with _lock:
        _state.entries[key] = _Entry(listings=listings, fetched_at=moment, fetched_wall=now(), next_attempt=moment)
        _state.last_refreshed = now()
    log.info("ordris listings: %s listing%s for %s outward code%s", len(listings), "" if len(listings) == 1 else "s",
             len(key), "" if len(key) == 1 else "s")
    return _copy(listings)


# ---- the view the page renders ------------------------------------------------------------
def property_type_label(value: str) -> str:
    token = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in PROPERTY_TYPE_LABELS:
        return PROPERTY_TYPE_LABELS[token]
    words = " ".join(token.split("_")).strip()
    return words[:1].upper() + words[1:] if words else "Home"


def rent_label(value: float | None) -> str:
    if value is None:
        return ""
    return f"£{int(value):,}" if float(value).is_integer() else f"£{value:,.2f}"


def available_label(value: str | None) -> str:
    """"from 1 December 2026" for an ISO date, "now" for the words agents use for it, else the text as entered."""
    text = (value or "").strip()
    if not text:
        return ""
    match = _ISO_DATE.match(text)
    if match:
        try:
            when = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return text
        return f"from {when.day} {when.strftime('%B %Y')}"
    if text.lower() in ("now", "immediately", "immediate", "available now", "asap"):
        return "now"
    return text


def alt_text(bedrooms: int | None, type_label: str, outward: str) -> str:
    what = type_label.lower() if type_label else "home"
    if bedrooms:
        beds = NUMBER_WORDS.get(bedrooms, str(bedrooms))
        return f"A {beds}-bedroom {what} to rent in {outward}"
    article = "An" if what[:1] in "aeiou" else "A"
    return f"{article} {what} to rent in {outward}"


def for_template(listings: list[dict]) -> list[dict]:
    """The renderer's whitelist: each listing as the page shows it, built field by field with exactly VIEW_FIELDS."""
    view = []
    for item in listings:
        if not isinstance(item, dict) or not item.get("outward"):
            continue
        bedrooms = item.get("bedrooms") if isinstance(item.get("bedrooms"), int) else None
        type_label = property_type_label(str(item.get("property_type") or ""))
        photos = item.get("photos") if isinstance(item.get("photos"), list) else []
        view.append({
            "outward": str(item["outward"]),
            "bedrooms": bedrooms,
            "property_type": type_label,
            "rent": rent_label(item.get("rent_pcm") if isinstance(item.get("rent_pcm"), (int, float)) else None),
            "available": available_label(item.get("available_from") if isinstance(item.get("available_from"), str) else None),
            "photo": photos[0] if photos and isinstance(photos[0], str) else "",
            "alt": alt_text(bedrooms, type_label, str(item["outward"])),
        })
    return view
