"""Shared data structures passed between expired-listing pipeline stages.

Two conventions here that the rest of the package depends on:

1. `NA` ("N/A") is the value for a field the source did not print. It is
   never a placeholder for "we could not be bothered" or "we guessed" --
   the whole point of this bot is that a wrong owner name costs money at
   the skip trace vendor, so a missing field stays missing. Nothing in
   this package ever writes an estimated value into a captured field.

2. Captured fields are kept as the raw string exactly as the source
   printed it, and parsed into numbers only at the point of use (see
   `parse_money`/`parse_int`). That means a price history row that reads
   "$425,000" round-trips to the workbook unchanged, while scoring still
   gets a float to compare -- rather than reformatting the user's source
   data on the way through.
"""

from dataclasses import dataclass, field, fields
import re

NA = "N/A"

# Row-level status markers. These travel in the status fields rather than
# in the captured data, so a failed pull never looks like a real value.
MLS_PULL_FAILED = "MLS_PULL_FAILED"
NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
PORTAL_UNAVAILABLE = "PORTAL_UNAVAILABLE"

# Statuses that count as "no longer listed" -- the listings this bot is for.
#
# connectMLS prints two different spellings depending on where you are.
# The alert email and the API's MlsStatus say "Expired"; the agent-side
# results grid uses the short code "EXPD" (confirmed 2026-08-14 from a
# 180-match saved search). Both are accepted. The other short codes are
# the obvious analogues and are NOT yet confirmed against a real row --
# they cost nothing if wrong, since an unrecognised status is simply
# skipped rather than mis-filed.
EXPIRED_STATUSES = {
    "expired", "expd",
    "withdrawn", "with", "wdrn",
    "canceled", "cancelled", "canc",
    "temporarily off market", "temp off market", "tom",
}


def parse_money(raw: str | None) -> float | None:
    """"$425,000" -> 425000.0; NA/blank/unparseable -> None (never 0.0).

    Returning None rather than 0.0 matters: 0.0 would silently become a
    real data point in a price-reduction calculation.
    """
    if not raw or raw == NA:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw))
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_int(raw: str | None) -> int | None:
    value = parse_money(raw)
    return int(value) if value is not None else None


@dataclass
class AlertListing:
    """One Expired/Withdrawn/Canceled row scraped out of an alert email."""
    mls_no: str
    street_address: str
    # Unit/apt designator split off the street address. Kept because
    # a condo building has one parcel per unit on the assessor site,
    # and without it every unit in the building looks equally likely.
    unit: str = ""
    town: str = ""
    zip_code: str = ""
    status: str = ""
    received_at: str = ""  # ISO timestamp of the alert email it came from
    source_email: str = ""  # message id / filename, for tracing a bad row back


@dataclass
class PriceChange:
    """One row of the MLS price history, as printed."""
    date: str = NA
    old_price: str = NA
    new_price: str = NA

    def as_text(self) -> str:
        return f"{self.date}: {self.old_price} -> {self.new_price}"


@dataclass
class MlsDetail:
    """Step 2 capture. Every field defaults to NA, so a field the page did
    not show stays NA instead of becoming an empty string that reads like
    a real blank value.
    """
    mls_no: str
    pull_status: str = "ok"  # "ok" | MLS_PULL_FAILED
    pull_error: str = ""
    list_price_final: str = NA
    list_price_original: str = NA
    # connectMLS prints a 'Previous List Price' and the date it last
    # changed. Together they prove whether a drop happened and when,
    # even when the full history is not available.
    previous_list_price: str = NA
    price_last_updated: str = NA
    days_on_market: str = NA
    cumulative_dom: str = NA
    list_date: str = NA
    expiration_date: str = NA
    price_history: list[PriceChange] = field(default_factory=list)
    # False means the source never gave us a history -- distinct from
    # 'a history that shows no drops'. See scoring.count_price_drops.
    price_history_available: bool = False
    beds: str = NA
    full_baths: str = NA
    half_baths: str = NA
    sqft_above_grade: str = NA
    lot_size: str = NA
    year_built: str = NA
    property_type: str = NA
    town: str = NA
    zip_code: str = NA
    listing_agent: str = NA
    brokerage: str = NA
    public_remarks: str = NA  # first 200 chars only, per the spec
    # A connectMLS export mixes expired RENTALS in with expired sales;
    # the observed one listed at $2,375/mo. Priced as a sale that is a
    # $2.46/sqft property, so it is flagged and sent to review.
    likely_rental: bool = False

    def price_history_text(self) -> str:
        return " | ".join(c.as_text() for c in self.price_history) if self.price_history else NA


@dataclass
class OwnerRecord:
    """Step 3 capture from the town assessor's card.

    `status` is the gate for the vendor CSV: anything other than "ok"
    routes the row to the review file instead (see skiptrace.py).
    """
    status: str = "ok"  # "ok" | NEEDS_MANUAL_REVIEW | PORTAL_UNAVAILABLE
    owner_name: str = NA  # exactly as printed -- never reformatted or split here
    mailing_address: str = NA
    mailing_city: str = NA
    mailing_state: str = NA
    mailing_zip: str = NA
    assessed_value: str = NA
    last_sale_date: str = NA
    last_sale_price: str = NA
    assessor_living_area: str = NA
    assessor_year_built: str = NA  # not exported; used to disambiguate parcels
    assessor_beds: str = NA
    assessor_baths: str = NA
    portal_url: str = NA
    notes: list[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)

    def notes_text(self) -> str:
        return "; ".join(self.notes)


@dataclass
class TownPortal:
    """A row of the Town Portals tab -- looked up once, reused forever."""
    town: str
    portal_url: str
    platform: str  # "Vision" | "QDS" | "Northeast" | "Tyler" | "Custom" | "Unknown"
    search_notes: str = ""


@dataclass
class Lead:
    """A fully-assembled row of the Leads tab: alert + MLS + assessor +
    the Step 4 derived scores.
    """
    alert: AlertListing
    mls: MlsDetail
    owner: OwnerRecord
    price_drops: int | None = 0  # None = unknown, not zero
    total_reduction_dollars: str = NA
    total_reduction_pct: str = NA
    price_per_sqft: str = NA
    absentee: str = NA  # "Yes" | "No" | NA
    days_since_expired: str = NA
    lead_score: str = NA  # "High" | "Medium" | "Low"
    notes: str = ""

    @property
    def mls_no(self) -> str:
        return self.alert.mls_no

    @property
    def needs_review(self) -> bool:
        """Rows that must never reach the vendor CSV (spec Step 5)."""
        if self.owner.status in (NEEDS_MANUAL_REVIEW, PORTAL_UNAVAILABLE):
            return True
        # A rental priced as a sale would ship a nonsense lead to the
        # vendor; a human decides whether it is worth pursuing.
        return bool(getattr(self.mls, "likely_rental", False))


def dataclass_field_names(cls) -> list[str]:
    return [f.name for f in fields(cls)]
