"""Homes to rent on the area pages, read from Ordris's listings feed (plan, Phase 6 C5): the block on
/areas/{slug}/lettings, the hour-long in-process cache, the stale-while-error rule, the whitelist that keeps
anything personal out of the template, the request itself and the admin status line.

Ordris is never called for real: `ordris_listings.request_listings` (the one function that performs the HTTP
call) is replaced by a stub the test controls, and the request test replaces `http_client` with an httpx client
on a MockTransport. The cache is reset before and after every test."""
from __future__ import annotations

import logging
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.models import Area
from app.services import ordris_listings as listings
from tests.conftest import session

ORDRIS_URL = "https://ordris.test"
ORDRIS_TOKEN = "ordris-secret-for-hnh-0123456789abcdefghijklmn"
LOGGER = "hnh.ordris.listings"
# What a feed must never carry, and what the renderer must drop if one day it does.
PERSONAL = {"landlord_name": "Lena Landlord", "landlord_email": "lena@example.test", "phone": "07700 900900",
            "address": "12 Mumbles Road, Swansea", "postcode": "SA1 3AA", "name": "Marina View Apartments",
            "notes": "Keys with the neighbour at number 14", "landlord": {"name": "Lena Landlord"}}
SWANSEA_KEY = ("SA1", "SA10", "SA11", "SA12", "SA13", "SA14", "SA15", "SA16", "SA17", "SA18",
               "SA2", "SA3", "SA4", "SA5", "SA6", "SA7", "SA8", "SA9")


def _listing(**overrides) -> dict:
    item = {"id": 7, "outward": "SA1", "bedrooms": 3, "property_type": "terraced", "rent_pcm": 1150,
            "available_from": "2026-12-01", "photos": ["https://img.test/a.jpg", "https://img.test/b.jpg"]}
    item.update(overrides)
    return item


def _answer(*items) -> dict:
    return {"ok": True, "listings": list(items)}


def _block(html: str) -> str:
    """The homes block as rendered, or an empty string when the page has none."""
    match = re.search(r'<div class="homes" id="homes-to-rent">.*?<p class="homes-cta">.*?</p>\s*</div>', html, re.S)
    return match.group(0) if match else ""


@pytest.fixture(autouse=True)
def _fresh_cache():
    listings.reset()
    yield
    listings.reset()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", ORDRIS_TOKEN)


def _stub(monkeypatch, answer) -> list:
    """Replace the fetch. `answer` is the body to return, an exception to raise, or a callable given the
    outward codes; the list returned records every call's outward codes."""
    calls: list[tuple[str, ...]] = []

    def fake(outwards):
        calls.append(tuple(outwards))
        value = answer(outwards) if callable(answer) and not isinstance(answer, Exception) else answer
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(listings, "request_listings", fake)
    return calls


def _clock(monkeypatch, start: float = 1_000_000.0) -> dict:
    state = {"t": start}
    monkeypatch.setattr(listings, "clock", lambda: state["t"])
    return state


# ---- unconfigured: the page is exactly as before ------------------------------------------
def test_block_absent_when_unconfigured(client: TestClient, monkeypatch):
    assert not listings.configured() and config.ORDRIS_BASE_URL == "" and config.ORDRIS_PARTNER_TOKEN_HNH == ""
    calls = _stub(monkeypatch, _answer(_listing()))
    first = client.get("/areas/swansea/lettings")
    assert first.status_code == 200
    assert "Homes to rent" not in first.text and "homes-to-rent" not in first.text and _block(first.text) == ""
    assert calls == [] and listings.fetch_listings(["SA1"]) == [] and calls == []
    # one of the two variables alone is still unconfigured: nothing is asked for
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    assert listings.fetch_listings(["SA1"]) == [] and calls == []
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", "")
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN_HNH", ORDRIS_TOKEN)
    assert listings.fetch_listings(["SA1"]) == [] and calls == []
    # the page renders the same bytes whether Ordris is unconfigured or configured and answering with nothing
    assert client.get("/areas/swansea/lettings").text == first.text
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", ORDRIS_URL)
    _stub(monkeypatch, _answer())
    assert client.get("/areas/swansea/lettings").text == first.text
    assert listings.status()["last_refreshed"] is not None


# ---- configured and answering: the block and its fields ----------------------------------
def test_block_present_with_the_fields(client: TestClient, configured, monkeypatch):
    calls = _stub(monkeypatch, _answer(
        _listing(**PERSONAL),
        _listing(id=8, outward="SA2", bedrooms=None, property_type="studio", rent_pcm=None, available_from="now", photos=[]),
    ))
    page = client.get("/areas/swansea/lettings")
    assert page.status_code == 200
    assert calls == [SWANSEA_KEY]                       # the area's outward codes, sorted, in one call
    block = _block(page.text)
    assert block, page.text[:200]
    assert "<h2>Homes to rent in Swansea</h2>" in block
    assert '<img class="listing-photo" src="https://img.test/a.jpg" alt="A three-bedroom terraced house to rent in SA1" loading="lazy">' in block
    assert "https://img.test/b.jpg" not in block         # the first photo only
    assert "£1,150 <small>pcm</small>" in block and "3 bedrooms · Terraced house" in block
    assert "SA1 · Available from 1 December 2026" in block
    assert "Studio" in block and "SA2 · Available now" in block and '<span class="tag">To let</span>SA2' in block
    assert 'href="/valuation?area=swansea"' in block
    assert block.count("<article") == 2 and block.count("<a ") == 1       # the valuation link and nothing else
    for value in PERSONAL.values():
        if isinstance(value, str):
            assert value not in page.text, value
    assert "!" not in block and chr(0x2014) not in block
    # under the fee section: after its heading and before the next section's
    fee = page.text.index("<h2>The fee, worked out on Swansea rents</h2>")
    assert fee < page.text.index('<div class="homes"') < page.text.index("<h2>The Welsh paperwork we do for you</h2>")
    # the hub, short-let and commercial pages never show homes, and ask Ordris for nothing
    for path in ("/areas/swansea", "/areas/swansea/short-lets", "/areas/swansea/commercial"):
        r = client.get(path)
        assert r.status_code == 200 and "homes-to-rent" not in r.text, path
    assert calls == [SWANSEA_KEY]


# ---- the cache ---------------------------------------------------------------------------
def test_cached_within_the_hour(client: TestClient, configured, monkeypatch):
    clock = _clock(monkeypatch)
    calls = _stub(monkeypatch, _answer(_listing()))
    assert "homes-to-rent" in client.get("/areas/swansea/lettings").text
    assert "homes-to-rent" in client.get("/areas/swansea/lettings").text
    assert len(calls) == 1
    # the same outward set in any order, case or spelling is the same cache entry
    assert listings.fetch_listings(["sa2", "SA1", " sa1 "]) == listings.fetch_listings(["SA1", "SA2"])
    assert len(calls) == 2 and calls[1] == ("SA1", "SA2")
    clock["t"] += 59 * 60
    client.get("/areas/swansea/lettings")
    assert len(calls) == 2
    clock["t"] += 2 * 60                                 # 61 minutes after the first read
    client.get("/areas/swansea/lettings")
    assert len(calls) == 3 and calls[2] == SWANSEA_KEY
    assert listings.status()["cached"] == 2 and listings.status()["stale"] == 0
    # the caller gets its own copy: changing it never changes what the next page view sees
    got = listings.fetch_listings(["SA1", "SA2"])
    got[0]["photos"].clear()
    got[0]["outward"] = "XX1"
    assert listings.fetch_listings(["SA1", "SA2"])[0]["photos"] == ["https://img.test/a.jpg", "https://img.test/b.jpg"]


def test_stale_answer_kept_on_error_with_a_warning(client: TestClient, configured, monkeypatch, caplog):
    clock = _clock(monkeypatch)
    calls = _stub(monkeypatch, _answer(_listing()))
    assert "£1,150" in _block(client.get("/areas/swansea/lettings").text)
    clock["t"] += 61 * 60
    _stub(monkeypatch, listings.ListingsError("Ordris answered 503: not_configured ORDRIS_HNH_ORG_ID is not set."))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        page = client.get("/areas/swansea/lettings")
    assert page.status_code == 200 and "£1,150" in _block(page.text)       # the last good answer, still shown
    warning = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warning) == 1 and "serving the answer fetched 61 minutes ago" in warning[0].getMessage()
    assert "Ordris answered 503" in warning[0].getMessage() and ORDRIS_TOKEN not in caplog.text
    status = listings.status()
    assert status["stale"] == 1 and status["last_error"].startswith("Ordris answered 503") and status["last_error_at"] is not None
    # while Ordris is down the next attempt waits five minutes; the page is served from the cache meanwhile
    failing = _stub(monkeypatch, listings.ListingsError("Could not reach Ordris: ConnectTimeout"))
    clock["t"] += 60
    client.get("/areas/swansea/lettings")
    assert failing == []
    clock["t"] += listings.RETRY_SECONDS
    assert "£1,150" in _block(client.get("/areas/swansea/lettings").text) and len(failing) == 1
    # an unexpected exception is treated the same: one warning, never a 500
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _stub(monkeypatch, RuntimeError("boom"))
        clock["t"] += listings.RETRY_SECONDS
        assert client.get("/areas/swansea/lettings").status_code == 200
    assert "RuntimeError: boom" in caplog.records[-1].getMessage()
    # a day after it was fetched the last good answer is dropped: nothing is shown, and a warning says so
    clock["t"] += 24 * 60 * 60
    _stub(monkeypatch, listings.ListingsError("Could not reach Ordris: ConnectError"))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        page = client.get("/areas/swansea/lettings")
    assert page.status_code == 200 and "homes-to-rent" not in page.text
    assert "no earlier answer is kept" in caplog.records[-1].getMessage()
    # and a recovered Ordris is read again once the retry wait has passed
    recovered = _stub(monkeypatch, _answer(_listing(rent_pcm=995)))
    clock["t"] += listings.RETRY_SECONDS
    assert "£995" in _block(client.get("/areas/swansea/lettings").text) and len(recovered) == 1
    assert listings.status()["stale"] == 0
    assert len(calls) == 1                               # the first stub was called exactly once throughout


@pytest.mark.parametrize("answer", [
    None, "text", [], 42, {"ok": False, "listings": [_listing()]}, {"ok": "true", "listings": [_listing()]},
    {"ok": True}, {"ok": True, "listings": "nope"}, {"ok": True, "listings": {"id": 7}},
    {"ok": True, "listings": ["x", 5, None, {"outward": "not a code"}, {"id": 1, "bedrooms": 2}, {"outward": ""}]},
    {"ok": True, "listings": [_listing(outward="TA1")]},          # an outward code the page did not ask for
])
def test_malformed_answer_renders_nothing(client: TestClient, configured, monkeypatch, answer):
    calls = _stub(monkeypatch, answer)
    page = client.get("/areas/swansea/lettings")
    assert page.status_code == 200 and "homes-to-rent" not in page.text and "Homes to rent" not in page.text
    assert listings.fetch_listings(list(SWANSEA_KEY)) == []
    assert len(calls) == 1                               # an empty answer is cached like any other
    assert listings.status()["last_error"] == ""         # and is not a failure


# ---- the whitelist -----------------------------------------------------------------------
def test_no_personal_fields_reach_the_template(client: TestClient, configured, monkeypatch):
    sent = _listing(**PERSONAL, bedrooms="3", rent_pcm="1,150.50", property_type="x" * 80, id="prop_9",
                    photos=["http://plain.test/a.jpg", "javascript:alert(1)", 5, None, "https://img.test/ok.jpg ", "https://img.test/ok.jpg"],
                    extra={"deep": "value"})
    _stub(monkeypatch, _answer(sent, _listing(id=True, outward="sa2 ", bedrooms=True, rent_pcm=False, available_from=12, photos="nope")))
    got = listings.fetch_listings(list(SWANSEA_KEY))
    assert [set(item) for item in got] == [set(listings.FIELDS)] * 2
    assert got[0] == {"id": "prop_9", "outward": "SA1", "bedrooms": 3, "property_type": "x" * 40, "rent_pcm": 1150.5,
                      "available_from": "2026-12-01", "photos": ["https://img.test/ok.jpg"]}
    assert got[1] == {"id": "", "outward": "SA2", "bedrooms": None, "property_type": "terraced", "rent_pcm": None,
                      "available_from": None, "photos": []}
    view = listings.for_template(got)
    assert [set(item) for item in view] == [set(listings.VIEW_FIELDS)] * 2
    assert view[0]["rent"] == "£1,150.50" and view[0]["photo"] == "https://img.test/ok.jpg" and view[0]["alt"].startswith("A three-bedroom")
    assert view[1] == {"outward": "SA2", "bedrooms": None, "property_type": "Terraced house", "rent": "", "available": "",
                       "photo": "", "alt": "A terraced house to rent in SA2"}
    # the renderer builds from the whitelist, so a key smuggled into the dictionary later never reaches a page either
    assert set(listings.for_template([dict(got[0], address="12 Mumbles Road")])[0]) == set(listings.VIEW_FIELDS)
    page = client.get("/areas/swansea/lettings").text
    assert "homes-to-rent" in page
    for value in PERSONAL.values():
        if isinstance(value, str):
            assert value not in page, value
    assert "plain.test" not in page and "javascript:" not in page and "deep" not in _block(page)


# ---- the request ---------------------------------------------------------------------------
def test_request_headers_timeout_and_errors(configured, monkeypatch):
    seen: list[httpx.Request] = []
    answers: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(listings, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=listings.TIMEOUT))
    assert httpx.Client(timeout=listings.TIMEOUT).timeout == listings.http_client().timeout == httpx.Timeout(5.0)
    answers.append(httpx.Response(200, json=_answer(_listing())))
    body = listings.request_listings(("SA1", "SA2"))
    assert body["ok"] is True and len(body["listings"]) == 1
    request = seen[-1]
    assert str(request.url) == f"{ORDRIS_URL}/api/partner/hnh/listings?outward=SA1%2CSA2" and request.method == "GET"
    assert request.headers["Authorization"] == f"Bearer {ORDRIS_TOKEN}"
    # the credential travels once: Ordris reads the bearer only, and a second copy would only widen what a header log sees
    assert "X-Service-Token" not in request.headers
    assert [k for k, v in request.headers.items() if ORDRIS_TOKEN in v] == ["authorization"]
    assert request.headers["X-Partner-Client"] == "hnh" and request.headers["User-Agent"].startswith("HelloNewHomeHQ/")
    assert "Origin" not in request.headers
    # a refused call is an error naming the status and Ordris's short reason, never a header
    answers.append(httpx.Response(503, json={"ok": False, "reason": "not_configured", "message": "ORDRIS_HNH_ORG_ID is not set."}))
    with pytest.raises(listings.ListingsError) as refused:
        listings.request_listings(("SA1",))
    assert str(refused.value) == "Ordris answered 503: not_configured ORDRIS_HNH_ORG_ID is not set." and ORDRIS_TOKEN not in str(refused.value)
    answers.append(httpx.Response(400, text="<html>Bad request</html>"))
    with pytest.raises(listings.ListingsError, match="Ordris answered 400: <html>Bad request</html>"):
        listings.request_listings(("SA1",))
    answers.append(httpx.ConnectError("All connection attempts failed"))
    with pytest.raises(listings.ListingsError, match="Could not reach Ordris: ConnectError") as down:
        listings.request_listings(("SA1",))
    assert ORDRIS_TOKEN not in str(down.value)
    # a 200 that is not JSON is an empty answer, not a failure
    answers.append(httpx.Response(200, text="<html>maintenance</html>"))
    assert listings.request_listings(("SA1",)) is None and listings.parse_answer(None) == []
    # and through the cache the connect error becomes one warning and an empty page, never an exception
    answers.append(httpx.ConnectError("All connection attempts failed"))
    assert listings.fetch_listings(["SA1"]) == [] and listings.status()["last_error"].startswith("Could not reach Ordris")


def test_whole_area_codes_are_not_sent_and_large_sets_are_batched(client: TestClient, configured, monkeypatch):
    with session() as db:
        london = db.query(Area).filter(Area.slug == "london").one().postcodes
        somerset = db.query(Area).filter(Area.slug == "somerset").one().postcodes
    assert "SE" in london and "E" in london and "TA" in somerset        # whole postcode areas, which Ordris cannot look up
    key = listings.outward_key(london)
    assert key and all(len(code) >= 3 and code[-1].isdigit() or code[-2].isdigit() for code in key)
    assert "SE" not in key and "E" not in key and "KT1" in key and "TW10" in key
    assert listings.outward_key(somerset) == ("BS20", "BS21", "BS22", "BS23", "BS24", "BS25", "BS26", "BS27", "BS28", "BS29",
                                              "BS39", "BS40", "BS41", "BS48", "BS49")
    calls = _stub(monkeypatch, _answer())
    assert listings.fetch_listings(["TA", "BA", "", None, "E"]) == [] and calls == []       # nothing to ask for
    assert listings.outward_key("SA1") == () and listings.outward_key(None) == ()
    # more codes than Ordris takes in one call are asked in batches of sixty, under one cache entry
    many = [f"AB{i}" for i in range(1, 71)]
    assert listings.fetch_listings(many) == [] and len(calls) == 2
    assert len(calls[0]) == 60 and len(calls[1]) == 10 and set(calls[0] + calls[1]) == set(many)
    assert listings.fetch_listings(many) == [] and len(calls) == 2


# ---- the admin status line ---------------------------------------------------------------
def test_admin_status_line(admin_client: TestClient, client: TestClient, configured, monkeypatch):
    _stub(monkeypatch, _answer(_listing()))
    line = re.search(r'<p class="help" id="listings-status">(.*?)</p>', admin_client.get("/admin/instructions").text, re.S).group(1)
    assert "not refreshed yet; the first lettings page view will read them" in line and "Last problem" not in line
    client.get("/areas/swansea/lettings")
    line = re.search(r'<p class="help" id="listings-status">(.*?)</p>', admin_client.get("/admin/instructions").text, re.S).group(1)
    assert re.search(r"last refreshed \d{2} \w{3} \d{4} \d{2}:\d{2}, 1 area set cached\.$", line), line
    clock = _clock(monkeypatch)
    clock["t"] += 61 * 60
    _stub(monkeypatch, listings.ListingsError("Ordris answered 502"))
    client.get("/areas/swansea/lettings")
    line = re.search(r'<p class="help" id="listings-status">(.*?)</p>', admin_client.get("/admin/instructions").text, re.S).group(1)
    assert "1 area set cached, 1 kept from an earlier answer" in line and "Last problem at " in line and "Ordris answered 502" in line
    assert ORDRIS_TOKEN not in line and "!" not in line and chr(0x2014) not in line
    monkeypatch.setattr(config, "ORDRIS_BASE_URL", "")
    listings.reset()
    line = re.search(r'<p class="help" id="listings-status">(.*?)</p>', admin_client.get("/admin/instructions").text, re.S).group(1)
    assert "Ordris is not configured, so the area pages show none" in line
