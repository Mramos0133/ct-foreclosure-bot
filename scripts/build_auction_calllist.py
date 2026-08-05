"""Turn the scraped auction board into a call list in master-sheet format.

Every listing on the board becomes a row, enriched from the checkpoint
where we have the case (debt, appraised value, owner name, lead bucket,
case summary) and standing on the board's own data where we don't -- the
board is the authority on what is actually being auctioned, so a listing
is never dropped just because the pipeline never matched that docket.

Sorted soonest-sale-first, which is the order to work a call list in.

  python3 scripts/build_auction_calllist.py \
      --board /tmp/auction_board.json --output ct_auctions.xlsx [--days 30]
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.auction_site import normalize_docket
from ct_foreclosure_bot.case_analysis import parse_defendant_name
from ct_foreclosure_bot.excel_export import export_to_xlsx
from ct_foreclosure_bot.models import CaseResult

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--board", required=True)
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--output", required=True)
    p.add_argument("--days", type=int, default=None,
                   help="Only sales within this many days. Default: every future sale on the board.")
    p.add_argument("--towns", nargs="*", default=None)
    return p.parse_args()


def split_address(full: str, town: str) -> tuple[str, str]:
    """'14 Hebron Avenue, Andover, CT 06232' -> ('14 Hebron Avenue', '06232')."""
    if not full:
        return "", ""
    zip_m = ZIP_RE.search(full)
    zip_code = zip_m.group(1) if zip_m else ""
    street = full.split(",")[0].strip()
    return street, zip_code


def main():
    args = parse_args()
    today = date.today()
    board = json.loads(Path(args.board).read_text())

    con = sqlite3.connect(args.checkpoint_db)
    try:
        stored = {}
        for (data,) in con.execute("SELECT data FROM case_results"):
            r = json.loads(data)
            stored[normalize_docket(r["docket_no"])] = r
    finally:
        con.close()

    rows = []
    for item in board:
        sale = datetime.strptime(item["sale_date"], "%Y-%m-%d").date()
        days = (sale - today).days
        if days < 0:
            continue  # already sold
        if args.days is not None and days > args.days:
            continue
        if args.towns and item["town"] not in set(args.towns):
            continue

        norm = normalize_docket(item["docket_site"])
        rec = stored.get(norm)
        street, zip_code = split_address(item["property_address"], item["town"])

        if rec:
            r = CaseResult(**rec)
            if not r.street_address:
                r.street_address, r.zip_code = street, zip_code
        else:
            r = CaseResult(
                town=item["town"], docket_no=item["docket_site"],
                street_address=street, zip_code=zip_code, case_caption="",
                motion_types_found=[], total_debt=None, appraised_value=None,
                encumbrances_subsequent_itemized="", encumbrances_subsequent_to_lien=None,
                attorney_fees=None, default_failure_to_appear=False,
                bankruptcy_stay_reopened=False, bankruptcy_supporting_text="",
                case_detail_url="", worksheet_doc_url=None,
            )
        if r.case_caption:
            r.owner_name = r.owner_name or parse_defendant_name(r.case_caption)

        # The board is authoritative for the sale itself.
        r.key_date = item["sale_date"]
        r.key_date_label = "Sale Date"
        r.days_to_key_date = days
        r.on_auction_site = True
        mt = list(r.motion_types_found)
        if "Scheduled Auction" not in mt:
            mt.append("Scheduled Auction")
        r.motion_types_found = mt

        when = f"- AUCTION {sale.strftime('%m/%d/%Y')}"
        if item.get("sale_time"):
            when += f" at {item['sale_time']}"
        when += f" ({days} days away)"
        head = [when]
        if item.get("sale_type"):
            head.append(f"- Sale type: {item['sale_type']}")
        if item.get("property_address"):
            head.append(f"- Property: {item['property_address']}")
        if not rec:
            head.append("- Not in the matched-case database: figures below are blank; see the auction notice.")
        r.case_summary = "\n".join(head) + (("\n" + r.case_summary) if r.case_summary else "")
        rows.append((days, r))

    rows.sort(key=lambda t: (t[0], t[1].town))
    results = [r for _, r in rows]
    counts = export_to_xlsx(results, args.output)
    enriched = sum(1 for _, r in rows if r.total_debt is not None or r.appraised_value is not None)
    print(f"exported {len(results)} upcoming auctions -> {args.output}")
    print("  " + "  ".join(f"{b}={n}" for b, n in counts.items() if n))
    print(f"  with debt/value from the database: {enriched}/{len(results)}")


if __name__ == "__main__":
    main()
