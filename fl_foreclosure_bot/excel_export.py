"""Builds a single-sheet ranked Excel workbook from scraped listings.

Ranked by equity_estimate (assessed value minus final judgment) descending
-- a rough "biggest cushion first" ordering, same spirit as the CT bot's
lead ranking but far simpler since this data source doesn't expose case
motions/status the way a docket does. Listings with no equity_estimate
(missing assessed value or final judgment) sort last, not dropped.
"""

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .models import AuctionListing

COLUMNS = [
    ("County", lambda r: r.county),
    ("Auction Date", lambda r: r.auction_date),
    ("Section", lambda r: r.section),
    ("Auction Status", lambda r: r.auction_status),
    ("Sold Date/Time", lambda r: r.sold_date_time or ""),
    ("Sold Amount", lambda r: r.sold_amount if r.sold_amount is not None else ""),
    ("Sold To", lambda r: r.sold_to or ""),
    ("Auction Type", lambda r: r.auction_type),
    ("Case No.", lambda r: r.case_no),
    ("Case URL", lambda r: r.case_no_url or ""),
    ("Final Judgment Amount", lambda r: r.final_judgment_amount if r.final_judgment_amount is not None else ""),
    ("Parcel ID", lambda r: r.parcel_id or ""),
    ("Parcel URL", lambda r: r.parcel_id_url or ""),
    ("Property Address", lambda r: r.property_address),
    ("Assessed Value", lambda r: r.assessed_value if r.assessed_value is not None else ""),
    ("Equity Estimate", lambda r: r.equity_estimate if r.equity_estimate is not None else ""),
    ("Plaintiff Max Bid", lambda r: r.plaintiff_max_bid),
    ("Source URL", lambda r: r.source_url),
]


def _sort_key(r: AuctionListing):
    equity = r.equity_estimate
    return (equity is None, -(equity or 0))


def build_workbook(results: list[AuctionListing]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Auction Listings"

    for col_idx, (header, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = Font(bold=True)

    for row_idx, result in enumerate(sorted(results, key=_sort_key), start=2):
        for col_idx, (_, getter) in enumerate(COLUMNS, start=1):
            ws.cell(row=row_idx, column=col_idx, value=getter(result))

    for col_idx in range(1, len(COLUMNS) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 22
    ws.freeze_panes = "A2"
    return wb


def export_to_xlsx(results: list[AuctionListing], output_path: str) -> int:
    wb = build_workbook(results)
    wb.save(output_path)
    return len(results)
