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
    # Set by the founder once the fee sheet is signed off; until then the marketing kit carries a hold notice.
    "fees_confirmed": False,
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
}


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
