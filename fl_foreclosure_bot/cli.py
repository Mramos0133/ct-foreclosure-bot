"""CLI entrypoint.

Two modes:

  ROLLING (default -- no --auction-date given). Keeps the store covering
  today .. today+--horizon days, and only fetches dates it hasn't
  already got, so the first run does the full horizon (~90 days) and a
  run a week later only fetches the ~7 newly-in-range days. The
  workbook is still exported from the store's FULL history, not just
  what this run fetched:
      python -m fl_foreclosure_bot --county both               # horizon 90
      python -m fl_foreclosure_bot --county both --horizon 120

  MANUAL (--auction-date given). Fetches exactly the dates asked for,
  ignoring what's already stored -- for spot-checking one date or
  re-pulling a specific range:
      python -m fl_foreclosure_bot --county miami-dade --auction-date 08/03/2026
      python -m fl_foreclosure_bot --county both --auction-date 08/03/2026 --days 30

Rolling mode also always re-fetches a window around today
(--refresh-days, default: 7 days back through 14 days ahead) even
though those dates are already stored: statuses change (an auction gets
canceled, sold, or rescheduled), and dates that have just passed need
one more look to record their outcome. Everything outside that window
is fetched once and reused.

Future dates work the same as past ones -- the site's AUCTIONDATE URL
parameter accepts any date, and a future date's page lists that day's
*scheduled* (upcoming) auctions. Dates with no auctions (weekends etc.)
simply contribute zero rows.

Run this from a normal home/office network, not a cloud VM or corporate
proxy -- see throttle.py for why (the site's WAF rejected every request
from this project's own dev sandbox regardless of request shape).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

from playwright.async_api import async_playwright

from .auction_page import iter_auction_listings
from .browser import launch_browser, new_context
from .counties import COUNTIES, county_choices
from .excel_export import export_to_xlsx
from .output import ResultWriter
from .store import Store
from .throttle import Throttle

log = logging.getLogger("fl_foreclosure_bot")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fl_foreclosure_bot",
        description="Scrape a realforeclose.com auction-date preview page (Miami-Dade and/or Broward) for foreclosure sale listings.",
    )
    p.add_argument("--county", required=True, choices=[*county_choices(), "both"], help="Which county's auction calendar to scrape.")
    p.add_argument("--auction-date", default=None, help="MANUAL mode: start date, MM/DD/YYYY (e.g. 08/03/2026). Omit for rolling mode.")
    p.add_argument("--days", type=int, default=1, help="MANUAL mode only: how many consecutive days to scrape from --auction-date (default 1).")
    p.add_argument("--horizon", type=int, default=90, help="ROLLING mode: how many days ahead to keep covered (default 90). Only dates not already in --store get fetched.")
    p.add_argument("--refresh-days", type=int, default=14, help="ROLLING mode: always re-fetch dates from 7 days ago through this many days ahead (default 14), to catch status changes and record outcomes for dates that just passed. 0 disables re-fetching.")
    p.add_argument("--store", default="fl_auction_store.sqlite3", help="SQLite file accumulating every listing found and which dates have been fetched. Pass '' to disable (rolling mode then has no memory and is not useful).")
    p.add_argument("--output", default="fl_foreclosure_results.csv", help="CSV log of rows fetched THIS run (appended to). The Excel workbook, not this, is the full picture.")
    p.add_argument("--export-xlsx", default="fl_foreclosure_leads.xlsx", help="Ranked Excel workbook, (re)written at the end of the run from the full store. Pass '' to skip.")
    p.add_argument("--headed", action="store_true", help="Run the browser headed (debugging only).")
    p.add_argument("--proxy", default=None, help="Explicit proxy server (defaults to HTTPS_PROXY env var if set).")
    p.add_argument("--force-tls12", action="store_true", help="Force Chromium's max TLS version to 1.2 -- only needed if you're behind a TLS-intercepting corporate proxy/VPN and see connection resets.")
    p.add_argument("--min-delay", type=float, default=1.5, help="Minimum seconds between requests.")
    p.add_argument("--max-delay", type=float, default=2.5, help="Maximum seconds between requests.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _parse_auction_date(raw: str):
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        raise SystemExit(f"--auction-date must be MM/DD/YYYY, got {raw!r}")


# How far back rolling mode re-checks. Sized to the intended weekly
# cadence: a date that was upcoming at the last run has since passed,
# and needs one more look to record whether it sold or was canceled.
REFRESH_LOOKBACK_DAYS = 7


def dates_to_scrape(
    store, county_name: str, today: date, horizon: int, refresh_days: int
) -> list[date]:
    """Which dates rolling mode should fetch for one county.

    Always: the refresh window (recent past through near future), since
    those pages change. Otherwise: only dates the store has never seen,
    which on a weekly cadence is just the few days that newly came into
    the horizon.
    """
    wanted = []
    for offset in range(-REFRESH_LOOKBACK_DAYS if refresh_days else 0, horizon + 1):
        day = today + timedelta(days=offset)
        if refresh_days and offset <= refresh_days:
            wanted.append(day)
        elif offset >= 0 and (store is None or not store.is_date_scraped(county_name, day)):
            wanted.append(day)
    return wanted


async def _main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.days < 1:
        raise SystemExit("--days must be >= 1")
    if args.horizon < 0:
        raise SystemExit("--horizon must be >= 0")

    today = date.today()
    county_keys = list(COUNTIES.keys()) if args.county == "both" else [args.county]
    store = Store(args.store) if args.store else None
    rolling = args.auction_date is None

    if rolling and store is None:
        raise SystemExit(
            "rolling mode needs a --store to know which dates it already has; "
            "either drop --store '' or pass --auction-date for a manual range."
        )

    manual_dates = None
    if not rolling:
        start_date = _parse_auction_date(args.auction_date)
        manual_dates = [start_date + timedelta(days=i) for i in range(args.days)]

    result_writer = ResultWriter(args.output)
    throttle = Throttle(min_delay=args.min_delay, max_delay=args.max_delay)
    fetched_count = 0
    fetched_listings = []  # only used to export when running without a store

    try:
        async with async_playwright() as p:
            browser = await launch_browser(
                p, headless=not args.headed, proxy_server=args.proxy, force_tls12=args.force_tls12,
            )
            try:
                context = await new_context(browser)
                page = await context.new_page()
                for county_key in county_keys:
                    cfg = COUNTIES[county_key]
                    county_name = cfg["display_name"]
                    if rolling:
                        auction_dates = dates_to_scrape(
                            store, county_name, today, args.horizon, args.refresh_days
                        )
                        log.info(
                            "%s: %d date(s) to fetch (horizon %d days; the rest are already stored)",
                            county_name, len(auction_dates), args.horizon,
                        )
                    else:
                        auction_dates = manual_dates

                    county_count = 0
                    cards_captured = False
                    for i, auction_date in enumerate(auction_dates):
                        date_str = auction_date.strftime("%m/%d/%Y")
                        log.info("scraping %s for %s...", county_name, date_str)
                        found = []
                        failed = False
                        try:
                            async for listing in iter_auction_listings(
                                page, throttle, county_name, cfg["base_url"], auction_date,
                                save_debug_html=f"fl_debug_{county_key}.html" if i == 0 else None,
                                save_debug_with_cards=(
                                    None if cards_captured
                                    else f"fl_debug_{county_key}_withcards.html"
                                ),
                            ):
                                result_writer.write(listing)
                                found.append(listing)
                        except Exception:  # noqa: BLE001 - one date's failure shouldn't sink the rest
                            log.exception("failed scraping %s %s", county_name, date_str)
                            failed = True

                        # Only record the date as fetched when it actually
                        # completed -- otherwise a transient failure would
                        # be remembered as "no auctions that day" forever.
                        if store is not None and not failed:
                            store.replace_date(county_name, auction_date, found)
                        if found:
                            log.info("%s %s: %d listings", county_name, date_str, len(found))
                            cards_captured = True
                        county_count += len(found)
                        fetched_count += len(found)
                        if store is None:
                            fetched_listings.extend(found)
                    log.info(
                        "%s: %d listings from %d date(s) fetched this run",
                        county_name, county_count, len(auction_dates),
                    )
            finally:
                await browser.close()
    finally:
        result_writer.close()
        if args.export_xlsx:
            # Export the full accumulated picture, not just this run's
            # rows -- an incremental run fetches a handful of dates but
            # the workbook must still show everything known.
            results = store.all_listings() if store is not None else fetched_listings
            counts = export_to_xlsx(results, args.export_xlsx, today)
            log.info(
                "exported %s: ACTIVE=%d SOLD=%d CANCELED=%d PAST_UNRESOLVED=%d "
                "(%d listings fetched this run)",
                args.export_xlsx, counts["ACTIVE"], counts["SOLD"], counts["CANCELED"],
                counts["PAST_UNRESOLVED"], fetched_count,
            )
        if store is not None:
            s = store.stats()
            log.info("store: %d listings across %d fetched date(s)", s["listings"], s["dates_scraped"])
            store.close()


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
