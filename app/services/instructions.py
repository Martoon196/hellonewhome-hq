"""Property instructions: the vocabulary and the helpers the Partner API, the admin panel and the partner
portal share, so a status change made anywhere is recorded the same way and reaches Ordris the same way."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import false, or_
from sqlalchemy.orm import Session

from .. import config
from ..models import (INSTRUCTION_ORIGINS, INSTRUCTION_SERVICE_LINES, INSTRUCTION_STATUSES, Area, Instruction, User, now)
from . import mail, routing

STATUSES = INSTRUCTION_STATUSES
ORIGINS = INSTRUCTION_ORIGINS
SERVICE_LINES = INSTRUCTION_SERVICE_LINES
OPEN_STATUSES = ("new", "contacted", "appraised")
STATUS_LABELS = {"new": "New", "contacted": "Contacted", "appraised": "Appraised", "instructed": "Instructed",
                 "let": "Let", "lost": "Lost"}
ORIGIN_LABELS = {"pp": "Property Predator", "kp": "Key Properties", "web": "Website"}
SERVICE_LINE_LABELS = {"residential": "Residential letting", "hmo": "HMO", "sa": "Short let", "commercial": "Commercial"}
FURNISHED_LABELS = {"furnished": "Furnished", "unfurnished": "Unfurnished", "part": "Part furnished", "": "Not said"}
# Hello New Home's own instructions, made by head office from a landlord's enquiry (Admin -> the lead -> Create
# instruction), carry the external_ref "lead:<id>". The namespace is head office's alone: the Partner API refuses a
# reference in it and its lookup never answers for one, so a sending system can neither read those instructions nor
# take a lead's reference before head office does.
HEAD_OFFICE_REF_PREFIX = "lead:"


def head_office_ref(external_ref: str | None) -> bool:
    """Whether `external_ref` is in head office's own namespace (compared without case or surrounding space)."""
    return str(external_ref or "").strip().lower().startswith(HEAD_OFFICE_REF_PREFIX)


def iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds") + "Z"
    return value.isoformat()


def history_entry(previous: str | None, current: str, by: str, note: str = "") -> dict:
    entry = {"at": iso(now()), "from": previous, "to": current, "by": by}
    if note:
        entry["note"] = note[:300]
    return entry


def set_status(instruction: Instruction, status: str, by: str, note: str = "") -> bool:
    """Move the instruction to `status`, recording who and when. Returns False (and records nothing) when the
    status is unchanged. The history list is replaced, not appended in place, so the JSON column is written."""
    if status not in STATUSES:
        raise ValueError(f"unknown instruction status {status!r}")
    if status == instruction.status:
        return False
    instruction.status_history = [*(instruction.status_history or []), history_entry(instruction.status, status, by, note)]
    instruction.status = status
    instruction.updated_at = now()
    return True


def area_record(area: Area | None) -> dict | None:
    if area is None:
        return None
    return {"id": area.id, "slug": area.slug, "name": area.name, "status": area.status, "hq": area.is_hq}


def public_by(by) -> str:
    """Who moved an instruction, as the sending system may see it: a role, never a Hello New Home email address.
    The stored history keeps `admin:<email>` for the audit screens; the Partner API maps it here."""
    text = str(by or "")
    if text.startswith("admin:"):
        return "head office"
    if text.startswith("partner:"):
        return "partner"
    if text.startswith("api:") and "@" not in text:
        return text
    return "head office" if ("@" in text or not text) else text


def api_record(instruction: Instruction) -> dict:
    """What the Partner API answers with: the reference and where it stands. No landlord details, no fee figures,
    no introducer ids, no staff email addresses: the sender already holds its own copy of what it sent."""
    return {
        "ref": instruction.ref,
        "external_ref": instruction.external_ref,
        "origin_system": instruction.origin_system,
        "service_line": instruction.service_line,
        "status": instruction.status,
        "status_history": [{**entry, "by": public_by(entry.get("by"))} for entry in (instruction.status_history or [])],
        "area": area_record(instruction.area),
        "partner_first": instruction.partner_first,
        "covered": instruction.area_id is not None,
        "ordris": {
            "landlord_client_id": instruction.ordris_landlord_client_id or None,
            "property_id": instruction.ordris_property_id or None,
            "tenancy_id": instruction.ordris_tenancy_id or None,
        },
        "created_at": iso(instruction.created_at),
        "updated_at": iso(instruction.updated_at),
    }


def scoped(db: Session, user: User, areas: list[Area]):
    """The instructions this user may see: everything for an admin; for a partner, those routed to one of their
    areas or to them directly. An instruction for a service the partner does not offer (a commercial one without
    commercial clearance, an HMO or a short let they do not manage) is head office's, so they see nothing of it
    even when it sits in their area."""
    q = db.query(Instruction)
    if user.role == "admin":
        return q
    ids = [a.id for a in areas]
    clauses = []
    if ids:
        clauses.append(Instruction.area_id.in_(ids))
    if user.partner_id:
        clauses.append(Instruction.partner_id == user.partner_id)
    if not clauses:
        return q.filter(false())
    q = q.filter(or_(*clauses))
    hidden = [line for line, service in routing.INSTRUCTION_SERVICES.items()
              if not routing.partner_offers(user.partner, service)]
    if hidden:
        q = q.filter(Instruction.service_line.notin_(hidden))
    return q


def routing_note(instruction: Instruction) -> str:
    """Why an instruction in a partner's area sits with head office ("" when it does not): the partner does not
    offer the service it asks for (services.routing)."""
    if instruction.partner_id is not None:
        return ""
    return routing.routing_note(instruction.area, service=routing.instruction_service(instruction.service_line))


def summary_lines(instruction: Instruction) -> list[str]:
    """The plain-text summary head office and the partner are emailed when an instruction arrives."""
    area = instruction.area
    return [
        f"Reference: {instruction.ref}",
        f"From: {ORIGIN_LABELS.get(instruction.origin_system, instruction.origin_system)}"
        + (f" ({instruction.origin_url})" if instruction.origin_url else ""),
        f"Landlord: {instruction.landlord_name}",
        f"Email: {instruction.landlord_email or 'none'}",
        f"Phone: {instruction.landlord_phone or 'none'}",
        f"Consent: {iso(instruction.consent_at)} (text {instruction.consent_text_version})",
        f"Address: {instruction.address}",
        f"Postcode: {instruction.postcode}",
        f"Bedrooms: {instruction.bedrooms if instruction.bedrooms is not None else 'not said'}",
        f"Type: {instruction.property_type or 'not said'}",
        f"Furnished: {FURNISHED_LABELS.get(instruction.furnished, instruction.furnished)}",
        f"Rent expectation: {('£' + format(instruction.rent_expectation, ',') + ' a month') if instruction.rent_expectation else 'not said'}",
        f"Available from: {instruction.available_from.isoformat() if instruction.available_from else 'not said'}",
        f"Service: {SERVICE_LINE_LABELS.get(instruction.service_line, instruction.service_line)}",
        f"Area: {area.name if area else 'not covered'}",
        f"Partner: {instruction.partner.name if instruction.partner else ('head office' if area and area.is_hq else 'none')}",
        *([f"Routing: {note}"] if (note := routing_note(instruction)) else []),
        f"Admin: {config.BASE_URL}/admin/instructions/{instruction.id}",
        f"Portal: {config.BASE_URL}/partners/instructions/{instruction.id}",
    ]


def notify(instruction: Instruction) -> bool:
    """Tell head office and the area's active partner that a property has arrived, by the same SMTP path leads
    use. Returns whether a message went out; nothing is raised."""
    # The reference, postcode and area only: the subject is what mail.send logs when nothing can be sent, and
    # log lines carry references and postcodes, not names (data sharing agreement, section 8).
    commercial = routing.is_commercial_instruction(instruction)
    subject = (f"[HNH property{' commercial' if commercial else ''}] {instruction.ref}, {instruction.postcode} "
               f"({instruction.area.name if instruction.area else 'not covered'})")
    recipients = mail.notify_recipients(instruction.area, service=routing.instruction_service(instruction.service_line))
    return mail.send(subject, "\n".join(summary_lines(instruction)), recipients)
