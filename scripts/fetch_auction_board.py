"""Scrape the complete statewide pending-foreclosure-sale board, with the
address and property type each listing carries -- not just the docket/date
pairs that auction_site.fetch_statewide_auction_listing() returns.

fetch_statewide_auction_listing() exists to answer one yes/no question per
case ("is this docket on the auction site"), so it throws away everything
but {docket: sale_date}. A standalone auction call list needs the rest:
the property address, the sale type, and the notice link -- and it must
cover every listing on the board, including cases the pipeline has never
matched and therefore has no record of.

Row shape observed on PendPostbyTownDetails.aspx:
  # | Sale Date "08/08/2026 12:00PM" | Docket "TTDCV256033863S"
    | "PUBLIC AUCTION FORECLOSURE SALE: Residential ADDRESS: 14 Hebron
       Avenue, Andover, CT 06232"
    | View Full Notice (link)

  python3 scripts/fetch_auction_board.py --output /tmp/auction_board.json
"""
import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from ct_foreclosure_bot.auction_site import BASE_URL, INDEX_URL, _fetch, _parse_town_links, _parse_sale_date
from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.throttle import Throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("fetch_auction_board")

ADDRESS_RE = re.compile(r"ADDRESS:\s*(.+)$", re.I)
TYPE_RE = re.compile(r"SALE:\s*([A-Za-z /&-]+?)\s*(?:ADDRESS:|$)", re.I)
TOWN_RE = re.compile(r"town=([^&]+)", re.I)


def parse_rows(html: str, town: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find(id="ctl00_cphBody_GridView1")
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        raw_date = " ".join(cells[1].get_text(" ", strip=True).split())
        sale_date = _parse_sale_date(raw_date)
        docket_raw = " ".join(cells[2].get_text(" ", strip=True).split())
        blurb = " ".join(cells[3].get_text(" ", strip=True).split())
        if not sale_date or not docket_raw:
            continue
        addr_m = ADDRESS_RE.search(blurb)
        type_m = TYPE_RE.search(blurb)
        notice = None
        link = cells[4].find("a") if len(cells) > 4 else None
        if link and link.get("href"):
            notice = link["href"].strip()
        rows.append({
            "town": town,
            "sale_date": sale_date.isoformat(),
            "sale_time": raw_date[10:].strip() or None,
            "docket_site": docket_raw,
            "property_address": addr_m.group(1).strip() if addr_m else "",
            "sale_type": type_m.group(1).strip() if type_m else "",
            "blurb": blurb,
            "notice_url": notice,
        })
    return rows


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    args = p.parse_args()

    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    all_rows = []
    async with async_playwright() as pw:
        browser = await launch_browser(pw, headless=True)
        try:
            context = await new_context(browser)
            index_html = await _fetch(context, throttle, INDEX_URL)
            links = _parse_town_links(index_html)
            log.info("%d towns with pending sales", len(links))
            for i, href in enumerate(links, 1):
                m = TOWN_RE.search(href)
                town = unquote(m.group(1)).replace("+", " ").strip() if m else "?"
                url = f"{BASE_URL}/{href.strip()}"
                try:
                    html = await _fetch(context, throttle, url)
                except Exception:
                    log.exception("failed %s", town)
                    continue
                rows = parse_rows(html, town, url)
                all_rows.extend(rows)
                log.info("[%d/%d] %-22s %d listings", i, len(links), town, len(rows))
        finally:
            await browser.close()

    Path(args.output).write_text(json.dumps(all_rows, indent=1))
    log.info("total %d listings -> %s", len(all_rows), args.output)


asyncio.run(main())
