"""CLI entrypoint.

Usage examples:
    python -m fl_foreclosure_bot --county miami-dade --auction-date 07/08/2026
    python -m fl_foreclosure_bot --county broward --auction-date 07/08/2026
    python -m fl_foreclosure_bot --county both --auction-date 07/08/2026 --export-xlsx leads.xlsx

Run this from a normal home/office network, not a cloud VM or corporate
proxy -- see throttle.py for why (the site's WAF rejected every request
from this project's own dev sandbox regardless of request shape).
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from playwright.async_api import async_playwright

from .auction_page import iter_auction_listings
from .browser import launch_browser, new_context
from .counties import COUNTIES, county_choices
from .excel_export import export_to_xlsx
from .output import ResultWriter
from .throttle import Throttle

log = logging.getLogger("fl_foreclosure_bot")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fl_foreclosure_bot",
        description="Scrape a realforeclose.com auction-date preview page (Miami-Dade and/or Broward) for foreclosure sale listings.",
    )
    p.add_argument("--county", required=True, choices=[*county_choices(), "both"], help="Which county's auction calendar to scrape.")
    p.add_argument("--auction-date", required=True, help="MM/DD/YYYY, matching the site's own URL format (e.g. 07/08/2026).")
    p.add_argument("--output", default="fl_foreclosure_results.csv", help="Output CSV path (appended to).")
    p.add_argument("--export-xlsx", default="fl_foreclosure_leads.xlsx", help="Ranked Excel workbook, (re)written at the end of the run. Pass '' to skip.")
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


async def _main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    auction_date = _parse_auction_date(args.auction_date)
    county_keys = list(COUNTIES.keys()) if args.county == "both" else [args.county]

    result_writer = ResultWriter(args.output)
    throttle = Throttle(min_delay=args.min_delay, max_delay=args.max_delay)
    all_results = []

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
                    log.info("scraping %s for %s...", cfg["display_name"], args.auction_date)
                    count = 0
                    try:
                        async for listing in iter_auction_listings(
                            page, throttle, cfg["display_name"], cfg["base_url"], auction_date
                        ):
                            result_writer.write(listing)
                            all_results.append(listing)
                            count += 1
                    except Exception:  # noqa: BLE001 - one county's failure shouldn't sink the other
                        log.exception("failed scraping %s", cfg["display_name"])
                    log.info("%s: %d listings found", cfg["display_name"], count)
            finally:
                await browser.close()
    finally:
        result_writer.close()
        if args.export_xlsx:
            n = export_to_xlsx(all_results, args.export_xlsx)
            log.info("exported %s: %d listings", args.export_xlsx, n)


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
