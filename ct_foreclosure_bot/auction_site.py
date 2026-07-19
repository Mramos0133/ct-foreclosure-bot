"""Cross-references matched cases against the CT Judicial Branch's public
pending-foreclosure-sale listing, to tell whether a case's sale has
already been publicly posted (relevant to the HOT lead criterion: judgment
entered on a foreclosure-by-sale case, but not yet publicly listed).

This lives on a different subdomain (sso.eservices.jud.ct.gov) than the
case lookup site, with its own robots.txt -- checked, and it carries the
same blanket "Disallow: /" as the main site, so it gets the same
conservative treatment (shared Throttle, sequential requests only).

Structure (confirmed by inspecting the live pages): an index page lists
every CT town that currently has at least one pending sale; each town has
its own detail page with a table of (sale date, docket number, property
address). Docket numbers there are in compact form with no punctuation
(e.g. "HHBCV236082544S"), unlike the dashed form used elsewhere in this
project ("HHB-CV-23-6082544-S"), so lookups normalize both sides before
comparing. There is no per-case lookup -- the only way to check a given
docket is to have already pulled the full statewide listing, so this
builds one dict up front and callers do O(1) lookups against it rather
than hitting the site again per case.
"""

import re
from datetime import date, datetime

from playwright.async_api import BrowserContext

from .throttle import Throttle

BASE_URL = "https://sso.eservices.jud.ct.gov/foreclosures/Public"
INDEX_URL = f"{BASE_URL}/PendPostbyTownList.aspx?Caller=Public"


def normalize_docket(docket_no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", docket_no.upper())


async def _fetch(context: BrowserContext, throttle: Throttle, url: str) -> str:
    await throttle.wait()
    resp = await context.request.get(url, timeout=30000)
    return await resp.text()


def _parse_town_links(index_html: str) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(index_html, "html.parser")
    hrefs = []
    for a in soup.find_all("a", href=re.compile(r"PendPostbyTownDetails")):
        href = a.get("href", "").strip()
        if href:
            hrefs.append(href)
    return hrefs


def _parse_sale_date(raw: str) -> date | None:
    # Observed format: "07/25/202612:00PM" -- date and time glued together
    # with no separator.
    m = re.match(r"(\d{1,2}/\d{1,2}/\d{4})", raw.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_town_detail(detail_html: str) -> dict[str, date]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(detail_html, "html.parser")
    table = soup.find(id="ctl00_cphBody_GridView1")
    if table is None:
        return {}

    result = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue  # header row
        sale_date = _parse_sale_date(cells[1].get_text(strip=True))
        docket_no = normalize_docket(cells[2].get_text(strip=True))
        if docket_no and sale_date:
            result[docket_no] = sale_date
    return result


async def fetch_statewide_auction_listing(
    context: BrowserContext, throttle: Throttle
) -> dict[str, date]:
    """Fetch the full current pending-sale listing, statewide.

    Returns {normalized_docket_no: sale_date}. Costs roughly one request
    per CT town that currently has a pending sale (observed ~90), each
    throttled the same as every other request this tool makes -- call this
    once per run and reuse the result, not per case.
    """
    index_html = await _fetch(context, throttle, INDEX_URL)
    town_links = _parse_town_links(index_html)

    listing: dict[str, date] = {}
    for href in town_links:
        detail_url = f"{BASE_URL}/{href.strip()}"
        detail_html = await _fetch(context, throttle, detail_url)
        listing.update(_parse_town_detail(detail_html))
    return listing
