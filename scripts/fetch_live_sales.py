"""Re-read the statewide pending-sale listing and write the sale dates as
JSON for scripts/city_pull.py --live-auction.

Why this exists: a CaseResult's stored key_date came from the Order
document whenever that case was last scraped, so it is frequently blank
for a case that was posted for sale afterwards. The auction site is the
authoritative, current source. Re-reading it before building an outreach
list is what turned three blank sale dates into real ones on the last
pull, two of them only days out.

Writes {docket_no: "YYYY-MM-DD"} using the checkpoint's own docket
formatting (the auction site strips dashes, so entries are matched back
through auction_site.normalize_docket).

  python3 scripts/fetch_live_sales.py --output /tmp/live_sales.json
  python3 scripts/fetch_live_sales.py --output /tmp/live_sales.json --towns Ansonia Naugatuck
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.auction_site import fetch_statewide_auction_listing, normalize_docket
from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.throttle import Throttle


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--output", required=True)
    p.add_argument("--towns", nargs="*", default=None,
                   help="Limit output to these towns. Default: every town in the checkpoint.")
    return p.parse_args()


async def main():
    args = parse_args()

    con = sqlite3.connect(args.checkpoint_db)
    try:
        known = {}
        for (data,) in con.execute("SELECT data FROM case_results"):
            d = json.loads(data)
            if args.towns and d["town"] not in set(args.towns):
                continue
            known[normalize_docket(d["docket_no"])] = d["docket_no"]
    finally:
        con.close()
    print(f"{len(known)} known dockets to match against")

    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=True)
        try:
            context = await new_context(browser)
            listing = await fetch_statewide_auction_listing(context, throttle)
        finally:
            await browser.close()
    print(f"live listing: {len(listing)} pending sales statewide")

    out = {}
    for norm, sale in listing.items():
        if norm in known:
            out[known[norm]] = sale.isoformat()

    Path(args.output).write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"wrote {len(out)} matched pending sales -> {args.output}")
    for docket, sale in sorted(out.items(), key=lambda kv: kv[1]):
        print(f"  {sale}  {docket}")


asyncio.run(main())
