"""Display helpers for timestamps.

Every datetime the app stores is naive UTC (models.now). Partners and head office read the
clock in UK time, so anything shown to a person goes through `fmt_uk`, registered as the
Jinja filter `uk`: {{ lead.created_at | uk("%d %b %Y %H:%M") }}. SLA maths stays in UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")


def to_uk(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UK)


def fmt_uk(dt: datetime | None, fmt: str = "%d %b %Y %H:%M") -> str:
    local = to_uk(dt)
    return local.strftime(fmt) if local else ""


def now_uk_stamp(fmt: str = "%d %b %Y %H:%M") -> str:
    return fmt_uk(datetime.utcnow(), fmt)
