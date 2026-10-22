"""Hello New Home to Ordris on "instructed" (plan, Phase 3 C3).

When an instruction's status becomes `instructed`, the landlord, the property and (later) the tenancy are
created in Hello New Home's own Ordris organisation so the agency work starts there with no re-typing.
The message is written to the outbox first and then sent; if the send fails, the row stays with its error
for `scripts/outbox_retry.py` and the admin's Resend button. Ordris is idempotent on `external_ref`, so a
resend never creates a second landlord or property.

The call:

    POST {ORDRIS_BASE_URL}/api/partner/hnh/instructions
    Authorization: Bearer <ORDRIS_PARTNER_TOKEN_HNH>      (and X-Service-Token with the same value)
    X-Partner-Client: hnh
    {"external_ref": "HNH-000123",
     "landlord": {"name", "email", "phone"},
     "property": {"name", "address", "postcode", "bedrooms", "property_type"},
     "tenancy": null}

and the answer carries `landlord_client_id`, `property_id` and `tenancy_id`, which are stored on the
instruction. Both header forms are sent because Ordris's partner routes read the bearer token (its
partnerAuth) while the plan names X-Service-Token; the value is the same secret either way.

Rules: the secret is read from config at send time and never stored in the outbox row, logged, or put in an
error message; errors recorded on the row carry the status code and Ordris's short reason, never the request
headers. With ORDRIS_BASE_URL or ORDRIS_PARTNER_TOKEN_HNH unset the sender is switched off: the row waits as
pending with a note saying so, and nothing is attempted. Head office can also switch sending off without touching
the environment ("Send instructed properties to Ordris" in Admin -> Settings, the ordris_sync_enabled setting):
while it is off every row waits as pending in the same way, no attempt is counted, and once it is back on the
Resend button or scripts/outbox_retry.py sends what waited. Network and HTTP errors never reach the caller.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from .. import config
from ..models import Instruction, Outbox, now
from . import settings

log = logging.getLogger("hnh.ordris")

KIND = "ordris.instruction"
CLIENT = "hnh"
PATH = "/api/partner/hnh/instructions"
TIMEOUT = 15.0
USER_AGENT = "HelloNewHomeHQ/1.0 (+https://hellonewhome.co.uk)"
MAX_ATTEMPTS = 8                  # the retry command stops trying after this many failures; Resend ignores it
NOT_CONFIGURED = "Ordris is not configured: set ORDRIS_BASE_URL and ORDRIS_PARTNER_TOKEN_HNH, then resend."
SWITCHED_OFF = ("Sending to Ordris is switched off in Admin, Settings: the message waits here. Switch it back on, then "
                "resend.")
ERROR_LIMIT = 500                 # characters of error kept on the row


def configured() -> bool:
    return bool(config.ORDRIS_BASE_URL and config.ORDRIS_PARTNER_TOKEN_HNH)


def enabled(db: Session) -> bool:
    """Whether head office has sending to Ordris switched on (Admin -> Settings; on unless switched off)."""
    return bool(settings.get(db, "ordris_sync_enabled"))


def target_for(instruction: Instruction) -> str:
    return f"instruction:{instruction.id}"


def instruction_id_from_target(target: str) -> int | None:
    name, _, value = (target or "").partition(":")
    return int(value) if name == "instruction" and value.isdigit() else None


def _property_name(instruction: Instruction) -> str:
    first = (instruction.address or "").split(",")[0].strip()
    return (first or instruction.address or instruction.ref or "")[:120]


def build_payload(instruction: Instruction) -> dict:
    """The body sent to Ordris: the plan's shape, nothing more (no fee figures, no introducer ids)."""
    return {
        "external_ref": instruction.ref,
        "landlord": {
            "name": instruction.landlord_name,
            "email": instruction.landlord_email,
            "phone": instruction.landlord_phone,
        },
        "property": {
            "name": _property_name(instruction),
            "address": instruction.address,
            "postcode": instruction.postcode,
            "bedrooms": instruction.bedrooms,
            "property_type": instruction.property_type,
        },
        "tenancy": None,
    }


def http_client() -> httpx.Client:
    """The HTTP client the sender uses. Tests replace this with one built on httpx.MockTransport."""
    return httpx.Client(timeout=TIMEOUT)


def _headers() -> dict:
    token = config.ORDRIS_PARTNER_TOKEN_HNH
    return {"Authorization": f"Bearer {token}", "X-Service-Token": token, "X-Partner-Client": CLIENT,
            "User-Agent": USER_AGENT, "Accept": "application/json"}


def _clip(text: str) -> str:
    return " ".join(str(text or "").split())[:ERROR_LIMIT]


def _pick(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", False):
            return str(value)[:60]
    return ""


def parse_ids(body) -> dict[str, str]:
    """The three ids from Ordris's answer. The plan names landlord_client_id, property_id and tenancy_id at the
    top level; an answer that nests them under `ids` or returns the records themselves is read too."""
    if not isinstance(body, dict):
        return {}
    source = body.get("ids") if isinstance(body.get("ids"), dict) else body
    landlord = _pick(source, "landlord_client_id", "landlordClientId")
    prop = _pick(source, "property_id", "propertyId")
    tenancy = _pick(source, "tenancy_id", "tenancyId")
    for key, current in (("landlord_client", landlord), ("property", prop), ("tenancy", tenancy)):
        record = body.get(key)
        if not current and isinstance(record, dict):
            current = _pick(record, "id")
        if key == "landlord_client":
            landlord = current
        elif key == "property":
            prop = current
        else:
            tenancy = current
    return {"landlord_client_id": landlord, "property_id": prop, "tenancy_id": tenancy}


def _failure_message(response: httpx.Response) -> str:
    """What is written on the outbox row for a non-2xx answer: the status and Ordris's reason, never a header."""
    reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            reason = " ".join(str(body.get(k)) for k in ("reason", "message") if body.get(k))
    except ValueError:
        reason = response.text[:120]
    if response.status_code == 503 and "not_configured" in reason:
        reason = "Ordris reports its HNH partner route is switched off (ORDRIS_PARTNER_TOKEN_HNH is not set there)"
    return _clip(f"Ordris answered {response.status_code}" + (f": {reason}" if reason else ""))


def enqueue(db: Session, instruction: Instruction) -> Outbox:
    """The pending outbox row for this instruction, reusing one that is still pending or failed rather than
    queueing the same message twice. The payload is refreshed from the instruction every time."""
    target = target_for(instruction)
    row = (db.query(Outbox).filter(Outbox.kind == KIND, Outbox.target == target, Outbox.state != "sent")
           .order_by(Outbox.id.desc()).first())
    if row is None:
        row = Outbox(kind=KIND, target=target, state="pending", attempts=0, last_error="")
    row.payload = build_payload(instruction)
    db.add(row)
    db.flush()
    return row


def deliver(db: Session, row: Outbox) -> tuple[bool, str]:
    """Send one outbox row now. Returns (sent, message); the row and the instruction are updated and committed
    either way. A row that is not configured to go anywhere is left pending, with the reason on it."""
    instruction = db.get(Instruction, instruction_id_from_target(row.target) or -1)
    if instruction is None:
        row.state, row.last_error = "failed", "The instruction this message is about no longer exists."
        db.add(row)
        db.commit()
        return False, row.last_error
    if not enabled(db):
        row.state, row.last_error = "pending", SWITCHED_OFF
        db.add(row)
        db.commit()
        log.info("ordris: instruction %s not sent; sending is switched off in the settings", instruction.ref)
        return False, SWITCHED_OFF
    if not configured():
        row.state, row.last_error = "pending", NOT_CONFIGURED
        db.add(row)
        db.commit()
        log.warning("ordris: instruction %s not sent; %s", instruction.ref, NOT_CONFIGURED)
        return False, NOT_CONFIGURED
    row.payload = build_payload(instruction)
    row.attempts = int(row.attempts or 0) + 1
    url = config.ORDRIS_BASE_URL + PATH
    try:
        with http_client() as client:
            response = client.post(url, json=row.payload, headers=_headers())
    except httpx.HTTPError as exc:
        # str(exc) names the URL at most; the headers, and so the secret, are never part of it.
        row.state, row.last_error = "failed", _clip(f"Could not reach Ordris: {exc.__class__.__name__}: {exc}")
        db.add(row)
        db.commit()
        log.warning("ordris: instruction %s attempt %s failed: %s", instruction.ref, row.attempts, row.last_error)
        return False, row.last_error
    if response.status_code not in (200, 201):
        row.state, row.last_error = "failed", _failure_message(response)
        db.add(row)
        db.commit()
        log.warning("ordris: instruction %s attempt %s refused: %s", instruction.ref, row.attempts, row.last_error)
        return False, row.last_error
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("ok") is False:
        row.state, row.last_error = "failed", _clip(f"Ordris answered {response.status_code} with ok false: "
                                                    f"{body.get('reason', '')} {body.get('message', '')}")
        db.add(row)
        db.commit()
        return False, row.last_error
    ids = parse_ids(body)
    if not ids.get("landlord_client_id") and not ids.get("property_id"):
        row.state, row.last_error = "failed", "Ordris answered without a landlord or property id."
        db.add(row)
        db.commit()
        log.warning("ordris: instruction %s attempt %s: %s", instruction.ref, row.attempts, row.last_error)
        return False, row.last_error
    instruction.ordris_landlord_client_id = ids["landlord_client_id"] or instruction.ordris_landlord_client_id
    instruction.ordris_property_id = ids["property_id"] or instruction.ordris_property_id
    instruction.ordris_tenancy_id = ids["tenancy_id"] or instruction.ordris_tenancy_id
    instruction.updated_at = now()
    row.state, row.last_error, row.sent_at = "sent", "", now()
    db.add_all([instruction, row])
    db.commit()
    log.info("ordris: instruction %s sent; landlord %s property %s", instruction.ref,
             instruction.ordris_landlord_client_id, instruction.ordris_property_id)
    return True, (f"Sent to Ordris: landlord client {instruction.ordris_landlord_client_id}, property "
                  f"{instruction.ordris_property_id}" + (f", tenancy {instruction.ordris_tenancy_id}" if instruction.ordris_tenancy_id else "") + ".")


def send_instruction(db: Session, instruction: Instruction) -> tuple[bool, str]:
    """Queue the instruction for Ordris and try to send it at once (the "Send to Ordris" button, and the status
    change to instructed)."""
    row = enqueue(db, instruction)
    db.commit()
    return deliver(db, row)


def on_status_change(db: Session, instruction: Instruction, previous: str, current: str) -> tuple[bool, str] | None:
    """Called after a status change has been committed. Becoming `instructed` sends the instruction to Ordris
    unless it has been sent already; anything else is no concern of Ordris's yet. Returns deliver()'s answer, or
    None when nothing was sent."""
    if current != "instructed" or previous == "instructed" or instruction.sent_to_ordris:
        return None
    return send_instruction(db, instruction)


def retryable(db: Session, *, limit: int = 50, include_exhausted: bool = False) -> list[Outbox]:
    q = db.query(Outbox).filter(Outbox.kind == KIND, Outbox.state.in_(("pending", "failed")))
    if not include_exhausted:
        q = q.filter(Outbox.attempts < MAX_ATTEMPTS)
    return q.order_by(Outbox.id).limit(limit).all()


def retry_pending(db: Session, *, limit: int = 50) -> list[tuple[Outbox, bool, str]]:
    """Send every pending or failed row that has attempts left (scripts/outbox_retry.py). Returns what happened
    to each row. With Ordris unconfigured, or sending switched off in the settings, nothing is attempted and every
    row keeps its place in the queue. Switched off, the rows are not even touched, so a failed one keeps its error."""
    if not enabled(db):
        return [(row, False, SWITCHED_OFF) for row in retryable(db, limit=limit)]
    results = []
    for row in retryable(db, limit=limit):
        sent, message = deliver(db, row)
        results.append((row, sent, message))
    return results


def fmt_when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "never"
