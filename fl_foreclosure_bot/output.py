"""Incremental CSV output, written/flushed as each listing is parsed."""

from __future__ import annotations

import csv
import os

COLUMNS = [
    "County",
    "Auction Date",
    "Section",
    "Auction Status",
    "Auction Start Time",
    "Sold Date/Time",
    "Sold Amount",
    "Sold To",
    "Auction Type",
    "Case No.",
    "Case URL",
    "Final Judgment Amount",
    "Parcel ID",
    "Parcel URL",
    "Property Address",
    "Assessed Value",
    "Equity Estimate",
    "Plaintiff Max Bid",
    "Source URL",
]


class ResultWriter:
    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(COLUMNS)
            self._fh.flush()

    def write(self, listing) -> None:
        row = [
            listing.county,
            listing.auction_date,
            listing.section,
            listing.auction_status,
            listing.auction_start_time or "",
            listing.sold_date_time or "",
            f"{listing.sold_amount:.2f}" if listing.sold_amount is not None else "",
            listing.sold_to or "",
            listing.auction_type,
            listing.case_no,
            listing.case_no_url or "",
            f"{listing.final_judgment_amount:.2f}" if listing.final_judgment_amount is not None else "",
            listing.parcel_id or "",
            listing.parcel_id_url or "",
            listing.property_address,
            f"{listing.assessed_value:.2f}" if listing.assessed_value is not None else "",
            f"{listing.equity_estimate:.2f}" if listing.equity_estimate is not None else "",
            listing.plaintiff_max_bid,
            listing.source_url,
        ]
        self._writer.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
