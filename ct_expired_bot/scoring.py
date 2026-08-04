"""Step 4: derived lead metrics and the High/Medium/Low score.

Address normalization (for the absentee test) is the only judgement call
in here, and it is deliberately conservative in the opposite direction
from names.py: a mailing address that merely *formats* differently from
the property address ("123 Main St" vs "123 MAIN STREET") is the same
address and must not raise an absentee flag, because absentee is a
High-score trigger on its own. So normalization folds case, punctuation,
and the common street-type abbreviations before comparing, and anything
still different after that is treated as genuinely different.

Every metric returns NA rather than a number when its inputs are missing.
A missing sqft yields NA price-per-sqft, not a divide-by-zero or a zero.
"""

import re
from datetime import date, datetime

from .models import NA, Lead, MlsDetail, OwnerRecord, parse_money

# Street-type and unit abbreviations folded to a canonical token before
# comparing two addresses.
_STREET_ABBREV = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "LANE": "LN", "COURT": "CT", "PLACE": "PL", "BOULEVARD": "BLVD",
    "TERRACE": "TER", "CIRCLE": "CIR", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "TRAIL": "TRL", "SQUARE": "SQ", "EXTENSION": "EXT", "TURNPIKE": "TPKE",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "APARTMENT": "APT", "UNIT": "UNIT", "SUITE": "STE", "NUMBER": "NO",
}


def normalize_address(raw: str | None) -> str:
    """Fold an address to a comparable form. Empty/NA -> ""."""
    if not raw or raw == NA:
        return ""
    upper = re.sub(r"[^A-Z0-9 ]", " ", str(raw).upper())
    tokens = [_STREET_ABBREV.get(t, t) for t in upper.split()]
    return " ".join(tokens)


def addresses_match(a: str | None, b: str | None) -> bool | None:
    """True/False, or None when either side is missing (absentee unknown)."""
    na, nb = normalize_address(a), normalize_address(b)
    if not na or not nb:
        return None
    if na == nb:
        return True
    # A mailing address is often the property address plus/minus the town
    # and state tail; treat a clean prefix relationship as the same place.
    return na.startswith(nb) or nb.startswith(na)


def count_price_drops(mls: MlsDetail) -> int:
    """Number of history rows where the price actually decreased."""
    drops = 0
    for change in mls.price_history:
        old, new = parse_money(change.old_price), parse_money(change.new_price)
        if old is not None and new is not None and new < old:
            drops += 1
    return drops


def _reduction(mls: MlsDetail) -> tuple[float | None, float | None]:
    original = parse_money(mls.list_price_original)
    final = parse_money(mls.list_price_final)
    if original is None or final is None or original <= 0:
        return None, None
    delta = original - final
    return delta, (delta / original) * 100.0


def price_per_sqft(mls: MlsDetail) -> str:
    final = parse_money(mls.list_price_final)
    sqft = parse_money(mls.sqft_above_grade)
    if final is None or sqft is None or sqft <= 0:
        return NA
    return f"{final / sqft:.2f}"


def days_since_expired(mls: MlsDetail, today: date | None = None) -> str:
    parsed = parse_date(mls.expiration_date)
    if parsed is None:
        return NA
    return str(((today or date.today()) - parsed).days)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y")


def parse_date(raw: str | None) -> date | None:
    if not raw or raw == NA:
        return None
    text = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _dom(mls: MlsDetail) -> int | None:
    """Prefer cumulative DOM -- it is the one that reflects relisting."""
    for candidate in (mls.cumulative_dom, mls.days_on_market):
        value = parse_money(candidate)
        if value is not None:
            return int(value)
    return None


def score_lead(lead: Lead, today: date | None = None) -> Lead:
    """Populate every Step 4 field on `lead` in place, and return it."""
    mls, owner = lead.mls, lead.owner

    lead.price_drops = count_price_drops(mls)
    delta, pct = _reduction(mls)
    lead.total_reduction_dollars = f"{delta:.0f}" if delta is not None else NA
    lead.total_reduction_pct = f"{pct:.1f}%" if pct is not None else NA
    lead.price_per_sqft = price_per_sqft(mls)
    lead.days_since_expired = days_since_expired(mls, today=today)

    match = addresses_match(owner.mailing_address, lead.alert.street_address)
    if match is None:
        lead.absentee = NA
    else:
        lead.absentee = "No" if match else "Yes"

    dom = _dom(mls)
    if lead.absentee == "Yes" or lead.price_drops >= 2 or (dom is not None and dom >= 120):
        lead.lead_score = "High"
    elif lead.price_drops == 1 or (dom is not None and 60 <= dom <= 119):
        lead.lead_score = "Medium"
    else:
        lead.lead_score = "Low"

    return lead


def sqft_mismatch_note(mls: MlsDetail, owner: OwnerRecord) -> str | None:
    """Spec Step 3: assessor vs MLS sqft off by >25% usually means the
    wrong parcel was matched. Returns a note string, or None.
    """
    mls_sqft = parse_money(mls.sqft_above_grade)
    assessor_sqft = parse_money(owner.assessor_living_area)
    if mls_sqft is None or assessor_sqft is None or mls_sqft <= 0:
        return None
    diff_pct = abs(assessor_sqft - mls_sqft) / mls_sqft * 100.0
    if diff_pct > 25.0:
        return (
            f"Assessor living area {assessor_sqft:.0f} sqft vs MLS "
            f"{mls_sqft:.0f} sqft ({diff_pct:.0f}% apart) -- possible wrong parcel"
        )
    return None
