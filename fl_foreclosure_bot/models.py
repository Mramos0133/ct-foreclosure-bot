"""Shared data structures for a single scraped auction listing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuctionListing:
    """One case card from a realforeclose.com auction-date preview page.

    Field presence varies by county and by case status -- see
    auction_page.py's module docstring for what was actually confirmed
    on real pages (Miami-Dade and Broward) vs. best-effort. Money fields
    are floats when parsed successfully, else None; `plaintiff_max_bid`
    is kept as the raw display string ("Hidden" is a common real value,
    not a missing one) rather than forced into a float.
    """
    county: str  # "Miami-Dade" | "Broward"
    auction_date: str  # ISO "YYYY-MM-DD"
    section: str  # "Running" | "Waiting" | "Closed or Canceled" | "Unknown"
    auction_status: str  # e.g. "Scheduled" (upcoming), "Sold", "Canceled per Order", "Canceled per Bankruptcy"
    sold_date_time: str | None  # raw display text, only present when auction_status == "Sold"
    sold_amount: float | None
    sold_to: str | None
    auction_type: str
    case_no: str
    case_no_url: str | None
    final_judgment_amount: float | None
    parcel_id: str | None
    parcel_id_url: str | None
    property_address: str
    assessed_value: float | None  # not always present (confirmed absent on a real Broward card)
    plaintiff_max_bid: str
    source_url: str  # the auction-date page URL this listing was scraped from
    auction_start_time: str | None = None  # raw display text (e.g. "08/03/2026 09:00 AM ET"), upcoming ("Auction Starts") cards only

    @property
    def equity_estimate(self) -> float | None:
        """assessed_value - final_judgment_amount, a rough equity-cushion
        signal for ranking leads -- None when either figure is missing.
        Not a substitute for a real valuation; assessed value is a tax-roll
        figure, not market value.
        """
        if self.assessed_value is None or self.final_judgment_amount is None:
            return None
        return self.assessed_value - self.final_judgment_amount
