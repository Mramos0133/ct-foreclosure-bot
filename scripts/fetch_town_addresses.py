"""Build a {docket_no: {street_address, zip_code, case_name}} map by
walking a town's property-address search results.

Needed because street address lives only on the search-results grid, not
on the docket page -- so any docket the pipeline skipped as unmatched has
no address on record. A list built from docket-text analysis alone (e.g.
the mediation-terminated scan) is useless for outreach without this.

Costs one request per search-results page, no per-docket fetching.

  python3 scripts/fetch_town_addresses.py Milford Bridgeport Stratford \
      --output /tmp/addresses.json
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.search import iter_town_cases
from ct_foreclosure_bot.throttle import Throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("fetch_town_addresses")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("towns", nargs="+")
    p.add_argument("--output", required=True)
    p.add_argument("--case-status", default="Active Cases")
    p.add_argument("--property-type", default="All Properties")
    return p.parse_args()


async def main():
    args = parse_args()
    out = {}
    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=True)
        try:
            context = await new_context(browser)
            page = await context.new_page()
            for town in args.towns:
                n = 0
                async for listing in iter_town_cases(
                    page, throttle, town,
                    case_status=args.case_status, property_type=args.property_type,
                ):
                    out[listing.docket_no] = {
                        "street_address": listing.street_address,
                        "zip_code": listing.zip_code,
                        "case_name": listing.case_name,
                        "town": listing.city_town or town,
                    }
                    n += 1
                log.info("%s: %d listings", town, n)
        finally:
            await browser.close()

    Path(args.output).write_text(json.dumps(out, indent=1, sort_keys=True))
    log.info("wrote %d docket->address entries -> %s", len(out), args.output)


asyncio.run(main())
