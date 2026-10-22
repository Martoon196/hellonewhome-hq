"""Where a lead or a property instruction goes once the postcode has named its area: the one rule for the
partner it is given to and for who is told about it.

The postcode lookup (services.postcodes.locate) decides the area. This module decides the partner:

* a head-office area, an area with no partner, or no area at all: head office (partner None);
* an enquiry any partner can take (a tenant find, a general question) in a partner's area: that partner;
* an enquiry for a service (SERVICES) in a partner's area: that partner if they offer it, otherwise head
  office. Head office sets what each partner offers on the admin partner page, because management is offered
  area by area depending on who holds the area: residential full management, HMO management and short-let
  management (`Partner.*_management_enabled`, revision 0005), commercial letting (`Partner.commercial_enabled`,
  revision 0004) and commercial management (`Partner.commercial_management_enabled`). The area is kept either
  way, so the record still says where the property is and head office can hand it over once the partner offers
  the service, and the record carries a routing note saying why head office has it.

The public lead form, the Partner API, the admin reassignment screens, the notification emails, the partner
portal and the public area pages all call in here, so a service never reaches a partner by one path and not
another.
"""
from __future__ import annotations

from ..models import Area, Instruction, Lead, Partner

COMMERCIAL_LEAD_KIND = "commercial_appraisal"
COMMERCIAL_SERVICE_LINE = "commercial"

RESIDENTIAL_MANAGEMENT = "residential_management"
HMO_MANAGEMENT = "hmo_management"
SHORTLET_MANAGEMENT = "shortlet_management"
COMMERCIAL_LETTING = "commercial_letting"
COMMERCIAL_MANAGEMENT = "commercial_management"
# Each service, the Partner column that switches it on, and how it is named to people.
SERVICES = (RESIDENTIAL_MANAGEMENT, HMO_MANAGEMENT, SHORTLET_MANAGEMENT, COMMERCIAL_LETTING, COMMERCIAL_MANAGEMENT)
SERVICE_FLAGS = {
    RESIDENTIAL_MANAGEMENT: "residential_management_enabled",
    HMO_MANAGEMENT: "hmo_management_enabled",
    SHORTLET_MANAGEMENT: "shortlet_management_enabled",
    COMMERCIAL_LETTING: "commercial_enabled",
    COMMERCIAL_MANAGEMENT: "commercial_management_enabled",
}
SERVICE_LABELS = {
    RESIDENTIAL_MANAGEMENT: "residential full management",
    HMO_MANAGEMENT: "HMO management",
    SHORTLET_MANAGEMENT: "short-let management",
    COMMERCIAL_LETTING: "commercial letting",
    COMMERCIAL_MANAGEMENT: "commercial management",
}
# The switches added in revision 0005, as the admin form, the Partner API and the audit log name them.
SWITCHED_SERVICES = (RESIDENTIAL_MANAGEMENT, HMO_MANAGEMENT, SHORTLET_MANAGEMENT, COMMERCIAL_MANAGEMENT)

# The public area pages that describe one service: the page key (public._area_page) and the service it sells.
# The commercial page is not here: it already hands the landlord to head office when the partner is not cleared.
AREA_PAGE_SERVICES = {"lettings": RESIDENTIAL_MANAGEMENT, "shortlets": SHORTLET_MANAGEMENT}

# Instruction service lines and the service each one asks for; a residential letting is any partner's.
INSTRUCTION_SERVICES = {"hmo": HMO_MANAGEMENT, "sa": SHORTLET_MANAGEMENT, "commercial": COMMERCIAL_LETTING}

# The rent review form's "I'm interested in" answers, lower-cased.
_INTEREST_SERVICES = {"full management": RESIDENTIAL_MANAGEMENT, "rent guarantee": RESIDENTIAL_MANAGEMENT,
                      "hmo": HMO_MANAGEMENT, "short let": SHORTLET_MANAGEMENT}


def is_commercial_lead(lead: Lead) -> bool:
    return lead.kind == COMMERCIAL_LEAD_KIND


def is_commercial_instruction(instruction: Instruction) -> bool:
    return instruction.service_line == COMMERCIAL_SERVICE_LINE


def lead_service(kind: str, fields: dict | None = None) -> str:
    """The service an enquiry is for, from its kind and the fields the form sent; "" when any partner can take it.

    A short-let appraisal is short-let management. A commercial appraisal is commercial management when the
    property is already let (the landlord wants it run), commercial letting otherwise. A rent review is what the
    landlord ticked under "I'm interested in": full management or the rent guarantee (which rides on full
    management) is residential management, or HMO management for a shared house; HMO and short let are those
    services; a tenant find, "not sure" or no answer is any partner's."""
    fields = fields if isinstance(fields, dict) else {}
    if kind == "sa_appraisal":
        return SHORTLET_MANAGEMENT
    if kind == COMMERCIAL_LEAD_KIND:
        return COMMERCIAL_MANAGEMENT if str(fields.get("lease_status") or "").strip().lower() == "let" else COMMERCIAL_LETTING
    if kind != "rent_review":
        return ""
    service = _INTEREST_SERVICES.get(" ".join(str(fields.get("interest") or "").lower().split()), "")
    if service == RESIDENTIAL_MANAGEMENT and str(fields.get("type") or "").strip().upper().startswith("HMO"):
        return HMO_MANAGEMENT
    return service


def service_of(lead: Lead) -> str:
    """The service a stored lead is for: the one recorded at intake, or read again from its kind and fields for a
    lead that has none recorded (one stored before revision 0005 whose kind did not say)."""
    return lead.service or lead_service(lead.kind, lead.payload)


def instruction_service(service_line: str) -> str:
    return INSTRUCTION_SERVICES.get(service_line or "", "")


def _service(service: str, commercial: bool) -> str:
    """The service a call names: `service`, or commercial letting for the older `commercial=True` form."""
    return service or (COMMERCIAL_LETTING if commercial else "")


def partner_offers(partner: Partner | None, service: str) -> bool:
    """Whether this partner offers `service` ("" is any partner's). A missing partner offers nothing."""
    if partner is None:
        return False
    if not service:
        return True
    return bool(getattr(partner, SERVICE_FLAGS[service]))


def partner_can_take(partner: Partner | None, *, commercial: bool = False, service: str = "") -> bool:
    """Whether this partner may be given the enquiry: any partner for one any partner can take, a partner who
    offers the service otherwise (commercial=True means commercial letting). A missing partner can take nothing."""
    return partner_offers(partner, _service(service, commercial))


def not_offered(partner: Partner | None) -> list[str]:
    """The services this partner does not offer: what their portal leaves out. Every service for no partner."""
    return [s for s in SERVICES if not partner_offers(partner, s)]


def partner_for(area: Area | None, *, commercial: bool = False, service: str = "") -> Partner | None:
    """The partner an enquiry in `area` is routed to, or None for head office."""
    if area is None or area.is_hq or area.partner is None:
        return None
    return area.partner if partner_can_take(area.partner, commercial=commercial, service=service) else None


def partner_id_for(area: Area | None, *, commercial: bool = False, service: str = "") -> int | None:
    partner = partner_for(area, commercial=commercial, service=service)
    return partner.id if partner is not None else None


def routing_note(area: Area | None, *, commercial: bool = False, service: str = "") -> str:
    """Why an enquiry in a partner's area went to head office instead ("" when it did not): the record keeps it,
    so whoever opens it knows the partner was passed over on purpose."""
    service = _service(service, commercial)
    if area is None or area.is_hq or area.partner is None or partner_offers(area.partner, service):
        return ""
    return f"Routed to head office: {area.partner.name} does not offer {SERVICE_LABELS[service]} in {area.name}."


def area_offers(area: Area, service: str) -> bool:
    """Whether the public pages for `area` sell `service` locally: always for a head-office area or an area
    without a partner (head office covers them), otherwise only when the area's partner offers it."""
    if area.is_hq or area.partner is None:
        return True
    return partner_offers(area.partner, service)


def area_page_offered(area: Area, page: str) -> bool:
    """Whether an area page is a live service page (hub and commercial always are)."""
    service = AREA_PAGE_SERVICES.get(page)
    return area_offers(area, service) if service else True


def notified_partner(area: Area | None, *, commercial: bool = False, service: str = "") -> Partner | None:
    """The partner who is emailed about a new enquiry in `area`: the one it routes to, provided they are active
    and have an address. An enquiry for a service the partner does not offer therefore reaches head office only."""
    partner = partner_for(area, commercial=commercial, service=service)
    if partner is None or partner.status != "active" or not partner.email:
        return None
    return partner
