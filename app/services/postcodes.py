"""Postcode → region and area routing. Mirrors assets/site.js so server and browser agree."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Area

WALES = {"CF", "LD", "LL", "NP", "SA"}
SCOTLAND = {"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "TD", "ZE"}
NI = {"BT"}
_OUTWARD = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")


@dataclass
class Location:
    outward: str
    area_code: str
    region: str            # england | wales | scotland | northern-ireland
    area: Area | None

    @property
    def covered(self) -> bool:
        return self.area is not None


def normalise(postcode: str) -> str | None:
    """Return the outward code (e.g. 'DH1') or None if it isn't a plausible UK postcode."""
    clean = re.sub(r"[^A-Z0-9]", "", (postcode or "").upper())
    if len(clean) < 2:
        return None
    outward = clean[:-3] if len(clean) > 4 else clean
    return outward if _OUTWARD.match(outward) else None


def region_for(area_code: str) -> str:
    if area_code in WALES:
        return "wales"
    if area_code in SCOTLAND:
        return "scotland"
    if area_code in NI:
        return "northern-ireland"
    return "england"


def extract_postcode(text: str) -> str | None:
    """Pull a postcode out of free text such as an address line."""
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b", (text or "").upper())
    return (m.group(1) + (m.group(2) or "")) if m else None


def locate(db: Session, postcode: str, *, include_coming: bool = True) -> Location | None:
    outward = normalise(postcode)
    if not outward:
        return None
    area_code = re.match(r"^[A-Z]{1,2}", outward).group(0)
    statuses = ("active", "coming") if include_coming else ("active",)
    areas = db.query(Area).filter(Area.status.in_(statuses)).order_by(Area.sort, Area.id).all()
    match = None
    # Prefer an exact outward match over a whole-area match (so "SE" doesn't beat "SE1" if both exist).
    for a in areas:
        if outward in (a.postcodes or []):
            match = a
            break
    if match is None:
        for a in areas:
            if area_code in (a.postcodes or []):
                match = a
                break
    return Location(outward=outward, area_code=area_code, region=region_for(area_code), area=match)
