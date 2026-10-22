"""Site settings with defaults (stored in the settings table)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS: dict[str, object] = {
    "hq_name": "Hello New Home head office",
    "hq_phone": "01792 807 051",
    "hq_whatsapp": "447513552133",
    "hq_whatsapp_display": "0751 355 2133",
    "hq_email": "lettings@hellonewhome.co.uk",
    "hq_address": "Unit 54, First Floor, Menzies Distribution, Mill Stream Way, Central Business Park, Swansea SA7 0AG",
    "company_name": "Hello New Home 2 Limited",
    "company_number": "13752357",
    "registered_office": "[confirm registered office address]",
    "rsw_licence": "[LR-00000-00000]",
    "cmp_scheme": "[scheme]",
    "cmp_number": "[000000]",
    "redress_scheme": "[The Property Ombudsman / Property Redress Scheme]",
    "ico_number": "[ZA000000]",
    "facebook_url": "https://www.facebook.com/hellonewhome/",
    "instagram_url": "https://www.instagram.com/hellonewhome/",
    "lead_sla_hq_hours": 24,
    "lead_sla_partner_hours": 4,
    # Agency management system (contracts, tenancies, inspections, statements). Linked from the partner portal.
    "ordris_url": "https://ordris.io",
    "ordris_label": "Ordris: contracts, tenancies, inspections and statements",
    # The founder signed the fee sheet off on 19 October 2026. While False the marketing kit carries a hold notice.
    "fees_confirmed": True,
    "fees": {
        "tenant_find": "£495",
        "full_management_pct": "12%",
        "full_management_cap": "£150",
        "setup": "£295",
        "rent_guarantee_addon": "£25",
        "hmo_pct": "15%",
        "room_let_only": "£249",
        "sa_launch": "£495",
        "sa_full_pct": "18%",
        "sa_cohost_pct": "12%",
    },
    # Commercial property lettings (plan, Phase 5). The founder approved the sheet on 22 October 2026; the seed
    # revision "settings:commercial-fees-approved" (seed.SETTINGS_REVISIONS) ticked the confirmed box once on the
    # live database, so an untick in Admin -> Settings sticks. While unticked the public pages say only that fees
    # are quoted after the appraisal and the settings page carries a [CONFIRM] badge. FEES_COMMERCIAL_CONFIRMED=1
    # in the environment still confirms it on every boot, as FEES_CONFIRMED does for the residential sheet.
    # Commercial fees are quoted plus VAT, so registering for VAT later changes nothing a customer was told.
    "fees_commercial_confirmed": True,
    "fees_commercial": {
        "letting_pct": "10%",
        "letting_basis": "of the first year's rent",
        "letting_minimum": "£1,500",
        "management_pct": "8%",
        "management_basis": "of rent collected",
        "lease_renewal": "£750",
        "schedule_of_condition": "£350",
        "vat_note": "plus VAT",
    },
    # Whether Hello New Home 2 Ltd is registered for VAT. Residential fees must be shown including any tax
    # (Consumer Rights Act 2015, s.83), so while this is off the public site and the marketing kit say no VAT is
    # added, and once it is on every such claim says the fee includes VAT instead (services.vat). Switching it on
    # changes the public wording at once: review the residential figures first.
    "vat_registered": False,
    # Instructed properties are sent to Ordris (services.ordris_sync). Off: nothing is sent and the outbox rows
    # wait; on again, the Resend button or scripts/outbox_retry.py sends them.
    "ordris_sync_enabled": True,
}

# The commercial VAT note before the founder's decision of 22 October 2026. The seed revision replaces a stored
# note only while it still reads exactly this, so a note head office has edited is never overwritten.
OLD_COMMERCIAL_VAT_NOTE = "plus VAT only if Hello New Home 2 Ltd registers for VAT"

# The exact sentence the public pages show in place of the commercial fee sheet until it is confirmed.
FEES_PENDING_SENTENCE = "Fees are quoted after the appraisal"


def vat_registered(current: dict) -> bool:
    """The VAT switch from a get_all dict (False when absent)."""
    return bool(current.get("vat_registered"))


def commercial_fees(current: dict) -> dict | None:
    """The commercial fee sheet as the public pages may show it: the figures when confirmed, None otherwise.
    `current` is the dict from get_all; a stored sheet missing a key falls back to the default for that key."""
    if not current.get("fees_commercial_confirmed"):
        return None
    stored = current.get("fees_commercial")
    sheet = dict(DEFAULTS["fees_commercial"])
    if isinstance(stored, dict):
        sheet.update({k: v for k, v in stored.items() if k in sheet and v not in (None, "")})
    return sheet


def get_all(db: Session) -> dict:
    stored = {s.key: s.value for s in db.query(Setting).all()}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if v is not None})
    return out


def get(db: Session, key: str):
    s = db.get(Setting, key)
    return s.value if s and s.value is not None else DEFAULTS.get(key)


def put(db: Session, key: str, value) -> None:
    s = db.get(Setting, key)
    if s is None:
        s = Setting(key=key, value=value)
    else:
        s.value = value
    db.add(s)


def ensure_defaults(db: Session) -> None:
    existing = {s.key for s in db.query(Setting).all()}
    for k, v in DEFAULTS.items():
        if k not in existing:
            db.add(Setting(key=k, value=v))
    db.commit()
