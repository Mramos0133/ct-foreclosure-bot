"""Pull a subset of towns out of the checkpoint into a workbook that is
byte-for-byte the same FORMAT as the master statewide sheet.

Deliberately calls excel_export.export_to_xlsx -- the same function the
master sheet uses -- rather than building a bespoke workbook, so a city
pull always has the same columns, the same HOT/WARM/COLD/
POTENTIAL_SHORT_SALE/UNCLASSIFIED tabs, the same sort order within each
tab, and the same green(new)/yellow(updated) highlighting. Any future
column added to the master sheet shows up here for free.

Two corrections applied to the stored records before export, both cases
where the checkpoint is knowably stale rather than wrong-at-write-time:

  1. --live-auction: sale dates re-read from the auction site. A stored
     key_date came from the Order document whenever that case was last
     scraped and can be missing entirely for a case that was only later
     posted for sale.
  2. days_to_key_date is recomputed against today. It is frozen at scrape
     time, so a row can claim "8 days" on a sale that has already
     happened -- actively misleading on a call/knock list.

Usage:
  python3 scripts/city_pull.py Watertown Woodbury Middlebury Naugatuck Seymour \
      --output watertown_area_leads.xlsx \
      [--door-knock] [--complaint-days 30] \
      [--live-auction live_sales.json] [--new-since old_checkpoint.sqlite3]

--door-knock narrows to the field-visit subset: a lender complaint filed
within --complaint-days, or a property heading to auction (listed on the
auction site, or carrying a future sale date). Without it, every matched
case in those towns is exported.
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.excel_export import export_to_xlsx
from ct_foreclosure_bot.models import CaseResult


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("towns", nargs="+", help="Town names exactly as they appear in the checkpoint (e.g. 'New Haven').")
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--output", required=True, help="Output .xlsx path.")
    p.add_argument("--door-knock", action="store_true",
                   help="Only recent-complaint and going-to-auction cases (the field-visit subset).")
    p.add_argument("--complaint-days", type=int, default=30,
                   help="With --door-knock: how recent a complaint must be. Default 30.")
    p.add_argument("--live-auction", default=None,
                   help="JSON {docket_no: 'YYYY-MM-DD'} of freshly-read auction sale dates to overlay.")
    p.add_argument("--new-since", default=None,
                   help="An older checkpoint .sqlite3; cases absent from it are highlighted green as new.")
    return p.parse_args()


def load_results(db_path: str, towns: set[str]) -> list[CaseResult]:
    con = sqlite3.connect(db_path)
    try:
        out = []
        for (data,) in con.execute("SELECT data FROM case_results"):
            d = json.loads(data)
            if d["town"] in towns:
                out.append(CaseResult(**d))
        return out
    finally:
        con.close()


def days_until(iso: str | None, today: date) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.strptime(iso, "%Y-%m-%d").date() - today).days
    except ValueError:
        return None


def main():
    args = parse_args()
    today = date.today()
    towns = set(args.towns)

    results = load_results(args.checkpoint_db, towns)
    missing = towns - {r.town for r in results}
    if missing:
        print(f"note: no matched cases on record for {sorted(missing)}", file=sys.stderr)

    if args.live_auction:
        live = json.load(open(args.live_auction))
        for r in results:
            if live.get(r.docket_no):
                r.key_date = live[r.docket_no]
                r.key_date_label = "Sale Date"
                r.on_auction_site = True

    # Recompute against today -- see module docstring.
    for r in results:
        r.days_to_key_date = days_until(r.key_date, today)

    if args.door_knock:
        cutoff = (today - timedelta(days=args.complaint_days)).isoformat()
        kept = []
        for r in results:
            recent_complaint = bool(r.complaint_filed_date and r.complaint_filed_date >= cutoff)
            d = r.days_to_key_date if r.key_date_label == "Sale Date" else None
            going_to_auction = (d is not None and d >= 0) or (
                r.on_auction_site and not (d is not None and d < 0)
            )
            if recent_complaint or going_to_auction:
                kept.append(r)
        results = kept

    new_dockets = set()
    if args.new_since:
        old = sqlite3.connect(args.new_since)
        try:
            before = {r[0] for r in old.execute("SELECT docket_no FROM case_results")}
        finally:
            old.close()
        new_dockets = {r.docket_no for r in results} - before

    counts = export_to_xlsx(results, args.output, new_dockets=new_dockets)
    print(f"exported {len(results)} cases from {sorted(towns)} -> {args.output}")
    print("  " + "  ".join(f"{b}={n}" for b, n in counts.items() if n))
    if new_dockets:
        print(f"  green (new): {len(new_dockets)}")


if __name__ == "__main__":
    main()
