"""Where a lead or a property instruction goes once the postcode has named its area: the one rule for the
partner it is given to and for who is told about it.

The postcode lookup (services.postcodes.locate) decides the area. This module decides the partner:

* a head-office area, an area with no partner, or no area at all: head office (partner None);
* a residential, HMO or short-let enquiry in a partner's area: that partner;
* a commercial enquiry (lead kind `commercial_appraisal`, instruction service line `commercial`) in a
  partner's area: head office unless the partner has been cleared to take commercial instructions
  (`Partner.commercial_enabled`, set on the admin partner page). The area is kept either way, so the
  record still says where the property is and head office can hand it over once the partner is cleared.

The public lead form, the Partner API, the admin reassignment screens and the notification emails all
call in here, so a commercial property never reaches a partner by one path and not another.
"""
from __future__ import annotations

from ..models import Area, Instruction, Lead, Partner

COMMERCIAL_LEAD_KIND = "commercial_appraisal"
COMMERCIAL_SERVICE_LINE = "commercial"


def is_commercial_lead(lead: Lead) -> bool:
    return lead.kind == COMMERCIAL_LEAD_KIND


def is_commercial_instruction(instruction: Instruction) -> bool:
    return instruction.service_line == COMMERCIAL_SERVICE_LINE


def partner_can_take(partner: Partner | None, *, commercial: bool) -> bool:
    """Whether this partner may be given the enquiry: any partner for a residential one, a cleared partner for
    a commercial one. A missing partner can take nothing."""
    if partner is None:
        return False
    return bool(partner.commercial_enabled) if commercial else True


def partner_for(area: Area | None, *, commercial: bool = False) -> Partner | None:
    """The partner an enquiry in `area` is routed to, or None for head office."""
    if area is None or area.is_hq or area.partner is None:
        return None
    return area.partner if partner_can_take(area.partner, commercial=commercial) else None


def partner_id_for(area: Area | None, *, commercial: bool = False) -> int | None:
    partner = partner_for(area, commercial=commercial)
    return partner.id if partner is not None else None


def notified_partner(area: Area | None, *, commercial: bool = False) -> Partner | None:
    """The partner who is emailed about a new enquiry in `area`: the one it routes to, provided they are active
    and have an address. A commercial enquiry in an uncleared partner's area therefore reaches head office only."""
    partner = partner_for(area, commercial=commercial)
    if partner is None or partner.status != "active" or not partner.email:
        return None
    return partner
