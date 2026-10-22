"""The VAT wording on the public site and in the marketing kit: one switch, one place that says what changes.

Hello New Home 2 Ltd is not registered for VAT, and the public pages, the area content, the page meta
descriptions and the marketing kit say so in many ways ("no VAT", "nothing added for VAT", "we are not
VAT-registered"). Residential lettings fees must be shown including any tax (Consumer Rights Act 2015, s.83),
so the day the company registers, every one of those claims has to become "the fee includes VAT" at once,
and the residential figures have to be reviewed before that day. Much of the wording is not in a template:
it is in the area content head office edits in the admin (areas.content), the seeded area facts (rent notes),
the static page fragments and content/marketing-templates.json. So nothing is re-tokenised; instead the
settings switch `vat_registered` (Admin -> Settings) decides whether `wording()` rewrites the claims as a page
or a kit item is rendered:

* off (the default): `wording()` returns the text untouched, so the site reads exactly as it did;
* on: each claim in REWRITES becomes its VAT-inclusive form, and nothing else is touched. A claim about every
  fee becomes one about every residential fee, because the commercial fees are quoted plus VAT. Statements about
  other agents ("often plus VAT", "an agent adding VAT") and the commercial pages' "plus VAT" are left alone.

The admin panel always shows the stored wording. CLAIM_RE recognises a "no VAT" claim; the tests render every
public page and every kit item with the switch on and require that none survives, and that no rewrite claims VAT
is included in every fee (UNSCOPED_RE), so a new phrase that the list does not cover fails the suite rather than
reaching the site.
"""
from __future__ import annotations

import re

# (pattern, replacement), most specific first: a whole sentence is rewritten before the general rules see it.
REWRITES: list[tuple[re.Pattern, str]] = [(re.compile(p), r) for p, r in (
    (r"is not VAT-registered: no VAT is added to any fee",
     "is VAT-registered: residential fees include VAT and commercial fees are quoted plus VAT"),
    (r"is not VAT-registered: no VAT is added to any residential fee, and commercial fees are quoted plus VAT",
     "is VAT-registered: residential fees include VAT and commercial fees are quoted plus VAT"),
    (r"No hidden extras and no VAT: we are not VAT-registered, so", "No hidden extras, and VAT is included, so"),
    (r"Nothing is added for VAT, because we are not VAT-registered\.", "The fee includes VAT."),
    (r"\(we are not VAT-registered\)", "(net of VAT)"),
    (r"If Hello New Home 2 Ltd registers for VAT, we will tell you",
     "Hello New Home 2 Ltd is registered for VAT and every fee on this page includes it. We will tell you"),
    # A claim about every fee becomes one about every residential fee: commercial fees are quoted plus VAT.
    (r"(?:There is n|N)o VAT (?:is )?added to any Hello New Home fee", "Every Hello New Home residential fee includes VAT"),
    (r"No VAT is added to any fee", "Every residential fee includes VAT"),
    (r"No VAT is added to any of it", "VAT is included in all of it"),
    (r"No VAT is added", "VAT is included"),
    (r"No VAT on any fee", "VAT included in every residential fee"),
    (r"There is no VAT on top", "VAT is included"),
    (r"\band no VAT on top", "with VAT included"),
    (r"\b[Nn]o VAT on top", "VAT included"),
    (r"Nothing is added for VAT", "The fee includes VAT"),
    (r"\bwith nothing added for VAT", "with VAT included"),
    (r"\bnothing added for VAT", "VAT included"),
    (r"\badd no VAT", "include VAT"),
    (r"\bwith no VAT added", "with VAT included"),
    (r"\bwith no VAT\b", "with VAT included"),
    (r"\b[Nn]o VAT added", "VAT included"),
    (r"\b[Nn]o VAT\b", "VAT included"),
)]

# A claim that no VAT is charged, in any of the forms the content uses. Never true once registered.
CLAIM_RE = re.compile(r"\bno VAT\b|not VAT-registered|added for VAT|\badd no VAT", re.IGNORECASE)
# A VAT-inclusive claim about every fee without saying which: false beside the commercial fees, which are quoted plus
# VAT. A rewrite must say "every residential fee" (or "every fee on this page") instead.
UNSCOPED_RE = re.compile(r"\b(?:every|all|any)\s+(?:Hello New Home\s+)?fees?\s+includes?\s+VAT\b"
                         r"|\bVAT\s+(?:is\s+)?included\s+in\s+(?:every|all|any)\s+(?:Hello New Home\s+)?fees?\b", re.IGNORECASE)


def wording(text: str, registered: bool) -> str:
    """`text` as the public may read it: unchanged while the company is not VAT-registered; with every "no VAT"
    claim rewritten as VAT-inclusive once it is."""
    if not registered or not text or "VAT" not in text:
        return text
    for pattern, replacement in REWRITES:
        text = pattern.sub(replacement, text)
    return text


def wording_deep(obj, registered: bool):
    """wording() through a nested dict/list of strings (a rendered marketing kit item)."""
    if not registered:
        return obj
    if isinstance(obj, str):
        return wording(obj, registered)
    if isinstance(obj, list):
        return [wording_deep(x, registered) for x in obj]
    if isinstance(obj, dict):
        return {k: wording_deep(v, registered) for k, v in obj.items()}
    return obj


def claims(text: str) -> list[str]:
    """Every "no VAT" claim left in `text`, with a little context (for the tests and for reviewing content)."""
    return [text[max(0, m.start() - 40):m.end() + 40] for m in CLAIM_RE.finditer(text or "")]


def unscoped(text: str) -> list[str]:
    """Every VAT-inclusive claim in `text` that covers all fees, commercial ones included, with a little context."""
    return [text[max(0, m.start() - 40):m.end() + 40] for m in UNSCOPED_RE.finditer(text or "")]
