"""Fetches and parses one county's auction-date preview page.

Built from screenshots of two real pages (Miami-Dade and Broward, both on
realforeclose.com), not raw HTML -- every direct fetch attempt from the
dev sandbox (curl, headless Chromium, a URL-fetch tool) was rejected with
a 403 at the network edge before any page content was returned, so exact
DOM structure (tag names, CSS classes/IDs) could not be confirmed. To
stay robust despite that, parsing here works against the page's own
rendered label text ("Case #:", "Final Judgment Amount:", etc. -- these
were directly read off real screenshots for both counties) rather than
guessed selectors, and hyperlinks (Case #, Parcel ID) are recovered by
matching each anchor's own visible text against the parsed field values,
not by anchor position/class.

Confirmed from screenshots:
  - Each auction date page has two sections in this order: "Running
    Auctions" (live right now -- often empty with a "There are no cases
    currently being auctioned" placeholder) and "Auctions Closed or
    Canceled" (the day's finished results).
  - Each case renders as a card with a left-hand status block and a
    right-hand details block:
      * Sold cards: "Auction Sold" + a date/time line, then "Amount:"
        and "Sold To:".
      * Non-sold cards: "Auction Status" + a free-text status ("Canceled
        per Order", "Canceled per Bankruptcy", ... -- not an exhaustive
        or fixed list, so this is captured as free text, not an enum).
      * Both card types then share: "Auction Type:", "Case #:", "Final
        Judgment Amount:", "Parcel ID:", "Property Address:" (2 lines:
        street, then city/zip), and "Plaintiff Max Bid:" (often "Hidden",
        but a real dollar figure was also observed on a real card, so
        this is kept as a raw string, not coerced to a number).
      * "Assessed Value:" appears on Miami-Dade cards but was confirmed
        ABSENT on at least one real Broward card -- treated as optional
        everywhere, not county-specific, since it may simply be
        case-dependent.
  - Pagination shows as "page N of M" with "PRE"/"NEXT" controls;
    confirmed present (e.g. "page 1 of 3") but the exact clickable
    element (button/link/image-input) was not confirmed since no raw
    HTML was available. goto_next_page() below tries a few reasonable
    ways to find a "NEXT"-labeled control and clicks the first one that
    looks enabled; if that guess is wrong on the real site, only page 1
    gets scraped and a warning is logged -- report the exact on-page
    control back to get this fixed rather than it silently hanging.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from playwright.async_api import Page

from .models import AuctionListing
from .throttle import Throttle

log = logging.getLogger("fl_foreclosure_bot")

_MONEY_RE = r"\$?\s*([\d,]+\.\d{2})"


def build_url(base_url: str, auction_date: date) -> str:
    return f"{base_url}?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={auction_date.strftime('%m/%d/%Y')}"


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _extract_anchor_map(html: str) -> dict[str, str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    anchors: dict[str, str] = {}
    for a in soup.find_all("a"):
        text = a.get_text(strip=True)
        href = a.get("href")
        if text and href:
            anchors[text] = href
    return anchors


def _page_text(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def parse_total_pages(page_text: str) -> int:
    m = re.search(r"page\s*(\d+)\s*of\s*(\d+)", page_text, re.I)
    return int(m.group(2)) if m else 1


def _parse_block(
    block: str,
    county_name: str,
    auction_date_iso: str,
    section: str,
    source_url: str,
    anchors: dict[str, str],
) -> AuctionListing | None:
    is_sold = bool(re.match(r"Auction\s+Sold", block, re.I))

    sold_date_time = sold_amount = sold_to = None
    auction_status = ""

    if is_sold:
        auction_status = "Sold"
        m = re.search(r"Auction\s+Sold\s*([\d/]{6,10}\s+[\d:]{3,5}\s*[AP]M\s*ET)", block, re.I)
        sold_date_time = m.group(1).strip() if m else None
        m = re.search(r"Amount:\s*" + _MONEY_RE, block, re.I)
        sold_amount = _parse_money(m.group(1)) if m else None
        m = re.search(r"Sold\s*To:\s*(.+?)\s*Auction\s*Type:", block, re.I | re.S)
        sold_to = m.group(1).strip() if m else None
    else:
        m = re.search(r"Auction\s*Status\s*(.+?)\s*Auction\s*Type:", block, re.I | re.S)
        auction_status = m.group(1).strip() if m else ""

    m = re.search(r"Auction\s*Type:\s*(\S+)", block, re.I)
    auction_type = m.group(1).strip() if m else ""

    m = re.search(r"Case\s*#:\s*(\S+)", block, re.I)
    if not m:
        return None  # not a real case card -- nothing worth salvaging
    case_no = m.group(1).strip()

    m = re.search(r"Final\s*Judgment\s*Amount:\s*" + _MONEY_RE, block, re.I)
    final_judgment_amount = _parse_money(m.group(1)) if m else None

    m = re.search(r"Parcel\s*ID:\s*(\S+)", block, re.I)
    parcel_id = m.group(1).strip() if m else None

    m = re.search(
        r"Property\s*Address:\s*(.+?)\s*(?:Assessed\s*Value:|Plaintiff\s*Max\s*Bid:)",
        block, re.I | re.S,
    )
    property_address = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    m = re.search(r"Assessed\s*Value:\s*" + _MONEY_RE, block, re.I)
    assessed_value = _parse_money(m.group(1)) if m else None

    m = re.search(r"Plaintiff\s*Max\s*Bid:\s*(.+)$", block, re.I | re.S)
    plaintiff_max_bid = m.group(1).strip() if m else ""

    return AuctionListing(
        county=county_name,
        auction_date=auction_date_iso,
        section=section,
        auction_status=auction_status,
        sold_date_time=sold_date_time,
        sold_amount=sold_amount,
        sold_to=sold_to,
        auction_type=auction_type,
        case_no=case_no,
        case_no_url=anchors.get(case_no),
        final_judgment_amount=final_judgment_amount,
        parcel_id=parcel_id,
        parcel_id_url=anchors.get(parcel_id) if parcel_id else None,
        property_address=property_address,
        assessed_value=assessed_value,
        plaintiff_max_bid=plaintiff_max_bid,
        source_url=source_url,
    )


def _parse_section(
    section_text: str,
    section_label: str,
    county_name: str,
    auction_date_iso: str,
    source_url: str,
    anchors: dict[str, str],
) -> list[AuctionListing]:
    blocks = re.split(r"(?=Auction\s+(?:Status|Sold)\b)", section_text, flags=re.I)
    results = []
    for block in blocks:
        if not re.search(r"Case\s*#:", block, re.I):
            continue  # header text / "no cases" placeholder, not a card
        listing = _parse_block(block, county_name, auction_date_iso, section_label, source_url, anchors)
        if listing is not None:
            results.append(listing)
    return results


# Section headers observed on real (past-date) pages: "Running Auctions"
# and "Auctions Closed or Canceled". A future date's page lists that day's
# *scheduled* auctions instead, and its section header was never directly
# observed (only past-date screenshots were available during development)
# -- "Auctions Waiting" is the RealAuction platform's usual wording, so
# it's included here best-effort. Any cards outside every recognized
# section still get captured by the trailing catch-all pass below rather
# than dropped, so an unrecognized header only mislabels the `section`
# column, never loses listings.
_SECTION_HEADERS = [
    ("running auctions", "Running"),
    ("auctions waiting", "Waiting"),
    ("auctions closed or canceled", "Closed or Canceled"),
]


def parse_listings(
    html: str, county_name: str, auction_date: date, source_url: str
) -> list[AuctionListing]:
    text = _page_text(html)
    anchors = _extract_anchor_map(html)
    auction_date_iso = auction_date.isoformat()
    lower = text.lower()

    found = sorted(
        (idx, label)
        for header, label in _SECTION_HEADERS
        if (idx := lower.find(header)) != -1
    )

    results: list[AuctionListing] = []
    seen: set[str] = set()

    def _add(section_text: str, label: str) -> None:
        for listing in _parse_section(section_text, label, county_name, auction_date_iso, source_url, anchors):
            if listing.case_no not in seen:
                seen.add(listing.case_no)
                results.append(listing)

    for i, (idx, label) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(text)
        _add(text[idx:end], label)

    # Catch-all: any cards before the first recognized header, or on a page
    # with no recognized headers at all (e.g. a future-date layout that
    # words its section differently than guessed above).
    _add(text[: found[0][0]] if found else text, "Unknown")
    return results


async def fetch_page_html(page: Page, throttle: Throttle, url: str) -> str:
    await throttle.wait()
    await page.goto(url, wait_until="networkidle", timeout=30000)
    return await page.content()


async def goto_next_page(page: Page, throttle: Throttle) -> bool:
    """Best-effort click of a "NEXT"-labeled pagination control.

    Returns True if a click happened (caller should re-fetch page.content()
    and re-parse), False if no such control could be found -- not
    confirmed against real HTML (see module docstring), so this tries a
    few reasonable element shapes rather than one hard-coded selector.
    """
    candidates = [
        "input[value*='NEXT' i]",
        "button:has-text('NEXT')",
        "a:has-text('NEXT')",
        "input[type=image][alt*='NEXT' i]",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception:  # noqa: BLE001
            continue
        if count == 0:
            continue
        try:
            await throttle.wait()
            async with page.expect_navigation(wait_until="networkidle", timeout=30000):
                await locator.first.click()
            return True
        except Exception:  # noqa: BLE001
            log.warning("found a %r control but clicking/navigating it failed", selector, exc_info=True)
            continue
    log.warning("no pagination 'NEXT' control found by any known selector -- only this page was scraped")
    return False


async def iter_auction_listings(
    page: Page, throttle: Throttle, county_name: str, base_url: str, auction_date: date
):
    """Yield every AuctionListing across all pages for one county+date."""
    url = build_url(base_url, auction_date)
    html = await fetch_page_html(page, throttle, url)
    text = _page_text(html)
    total_pages = parse_total_pages(text)

    seen_case_nos: set[str] = set()
    page_num = 1
    while True:
        for listing in parse_listings(html, county_name, auction_date, url):
            if listing.case_no in seen_case_nos:
                continue
            seen_case_nos.add(listing.case_no)
            yield listing

        if page_num >= total_pages:
            break
        advanced = await goto_next_page(page, throttle)
        if not advanced:
            break
        html = await page.content()
        page_num += 1
