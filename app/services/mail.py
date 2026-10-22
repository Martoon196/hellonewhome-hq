"""The one SMTP path: lead notifications and instruction notifications both go through `send`.

SMTP_HOST unset means nothing is sent and the call says so (False), which is what the test suite and a
fresh deploy run with. A failure to send is logged and returned, never raised: no lead or instruction is
ever lost over email. Nothing here logs a message body, and no credential is ever part of a log line.
"""
from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage

from .. import config

log = logging.getLogger("hnh.mail")


def configured() -> bool:
    return bool(config.SMTP_HOST)


def notify_recipients(area, *, commercial: bool = False, service: str = "") -> list[str]:
    """Who hears about a new lead or instruction: head office (LEAD_NOTIFY_TO), plus the partner the enquiry
    routes to when they are active with an address (services.routing: an enquiry for a service only reaches a
    partner who offers it, and a commercial one only a partner cleared to take commercial instructions)."""
    from .routing import notified_partner       # imported here: routing needs the models, mail is imported by them
    to = [config.LEAD_NOTIFY_TO]
    partner = notified_partner(area, commercial=commercial, service=service)
    if partner is not None:
        to.append(partner.email)
    return to


_LABEL_RE = re.compile(r"^\s*(\[[^\]]*\]\s*[^:,(]*)")


def log_label(subject: str) -> str:
    """What a log line says about a message: its kind tag and the token after it ("[HNH property] HNH-000123",
    "[HNH lead] Rent review"), never the rest of the subject, which may name a person."""
    m = _LABEL_RE.match(subject or "")
    return m.group(1).strip() if m else "message"


def send(subject: str, body: str, to: list[str]) -> bool:
    """Deliver one plain-text message. Returns True when SMTP accepted it, False when SMTP is not configured,
    no recipient was given, or the send failed (logged as a warning). Log lines name the message by its kind
    and reference (log_label), never by the full subject or the body."""
    recipients = [r for r in dict.fromkeys(a.strip() for a in to) if r]
    label = log_label(subject)
    if not configured():
        log.info("mail not sent (SMTP not configured): %s", label)
        return False
    if not recipients:
        log.warning("mail not sent (no recipient): %s", label)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 - never lose a record over email
        log.warning("mail failed (%s): %s: %s", label, e.__class__.__name__, e)
        return False
