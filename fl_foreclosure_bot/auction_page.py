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
  - Pagination shows as "page N of M" with "PRE"/"NEXT" controls.

Confirmed later from real saved page HTML (fl_debug captures -- the
first raw HTML ever obtained for this site, see README):
  - The auction cards are NOT in the initial server HTML: the page ships
    an empty `<div id="docPgContainer">` and JavaScript fills it in via
    AJAX afterwards. page.goto(wait_until="networkidle") happens to wait
    long enough for that in practice (cards were successfully parsed on
    real runs), but it means pager clicks swap content in place with NO
    page navigation -- so advancing pages must wait for the DOM's cards
    to change, never for a navigation event.
  - The pager's current-page number is an <input> box, so the rendered
    text is "page  of 3" (no current number!) -- parse_total_pages()
    must treat the current-page digits as optional or it concludes
    every date is single-page and never paginates at all. This was the
    actual root cause of a real run under-counting a date's auctions.
  - The page also carries a "Next Auction > >" link that navigates to a
    DIFFERENT DATE -- any next-control matching must exclude it (and
    the "Previous Auction"/"Current" links) or pagination would silently
    walk off the requested date. Matching below therefore excludes
    elements whose text mentions "auction" and anything in the BLHeader
    date-navigation bar.
  - The pager's own clickable element markup is still not directly
    confirmed (the captured pages were a quiet Saturday with no cards);
    _find_next_page_controls() casts a wide net over likely shapes and
    the run saves a with-cards HTML sample per county so a still-wrong
    guess can be pinned down from evidence on the next round.
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
# "07/08/2026 10:03 AM ET" (sold cards) / "08/03/2026 09:00 AM ET"
# (scheduled cards) -- both confirmed on real screenshots.
_DATETIME_RE = r"([\d/]{6,10}\s+[\d:]{3,5}\s*[AP]M(?:\s*ET)?)"


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
    # Current page number is an <input>, so it's usually MISSING from the
    # rendered text ("page  of 3") -- optional group, or nothing paginates.
    m = re.search(r"page\s*(?:\d+\s*)?of\s*(\d+)", page_text, re.I)
    return int(m.group(1)) if m else 1


def _parse_block(
    block: str,
    county_name: str,
    auction_date_iso: str,
    section: str,
    source_url: str,
    anchors: dict[str, str],
) -> AuctionListing | None:
    # Three confirmed left-block shapes (all from real screenshots):
    #   "Auction Sold <datetime> Amount: $N Sold To: X"  -- completed sale
    #   "Auction Status <free text>"                     -- canceled/etc.
    #   "Auction Starts <datetime>"                      -- UPCOMING auction.
    # The third was only discovered from a user's screenshots after two
    # real runs silently under-counted: the card splitter didn't know
    # "Auction Starts", so every waiting auction on a page merged into
    # one block and only its first case survived parsing.
    is_sold = bool(re.match(r"Auction\s+Sold", block, re.I))
    is_scheduled = bool(re.match(r"Auction\s+Starts", block, re.I))

    sold_date_time = sold_amount = sold_to = auction_start_time = None
    auction_status = ""

    if is_sold:
        auction_status = "Sold"
        m = re.search(r"Auction\s+Sold\s*" + _DATETIME_RE, block, re.I)
        sold_date_time = m.group(1).strip() if m else None
        m = re.search(r"Amount:\s*" + _MONEY_RE, block, re.I)
        sold_amount = _parse_money(m.group(1)) if m else None
        m = re.search(r"Sold\s*To:\s*(.+?)\s*Auction\s*Type:", block, re.I | re.S)
        sold_to = m.group(1).strip() if m else None
    elif is_scheduled:
        auction_status = "Scheduled"
        m = re.search(r"Auction\s+Starts\s*" + _DATETIME_RE, block, re.I)
        auction_start_time = m.group(1).strip() if m else None
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
    # Confirmed on a real card: some listings have no parcel number at all
    # -- the Parcel ID line just links the words "Property Appraiser".
    # Don't record the word "Property" as a parcel ID (and don't look up
    # its anchor -- the left nav has its own unrelated "Property
    # Appraiser" link that would wrongly match).
    if parcel_id and parcel_id.lower() in ("property", "appraiser"):
        parcel_id = None

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
        auction_start_time=auction_start_time,
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
    blocks = re.split(r"(?=Auction\s+(?:Status|Sold|Starts)\b)", section_text, flags=re.I)
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
    resp = await page.goto(url, wait_until="networkidle", timeout=30000)
    status = resp.status if resp else None
    html = await page.content()
    if status is not None and status >= 400:
        # Most likely the WAF rejecting the request -- surfaced loudly
        # because the symptom downstream is just "0 listings", which is
        # indistinguishable from a legitimately quiet auction date.
        log.warning("HTTP %s fetching %s (page text starts: %r)", status, url, _page_text(html)[:120])
    return html


# The date-navigation bar ("Previous Auction" / "Current" / "Next
# Auction > >") changes DATES, not result pages -- clicking it would
# silently walk the scrape onto the wrong day. Confirmed classes from
# real HTML: BLHeaderPrev/BLHeaderNext/BLHeaderToday inside .BLNav.
_NEXT_TEXT_RE = re.compile(r"NEXT", re.I)
_DATE_NAV_TEXT_RE = re.compile(r"auction|previous|current", re.I)

_NEXT_CONTROL_SELECTORS = [
    "[class*='PageRight']",
    "[class*='PageNext']",
    "[class*='pgnext' i]",
    "img[alt*='next' i]",
    "input[type='image'][alt*='next' i]",
    "input[value*='NEXT' i]",  # text-filter below can't see input values
]

# Cheap textual hint that a pager exists at all, so quiet dates (most of
# a 90-day sweep) skip the click-hunt entirely.
_PAGER_HINT_RE = re.compile(r"PageRight|PageNext|pgnext|page\s*(?:\d+\s*)?of\s*\d+", re.I)


async def _find_next_page_controls(page: Page) -> list:
    """Collect plausible next-page controls, excluding date navigation.

    Wide-net matching (see module docstring: the pager's exact markup is
    unconfirmed): class-based candidates first, then anything whose own
    text is essentially just "NEXT". Every candidate is filtered against
    the date-nav bar's classes/wording.
    """
    handles = []
    for sel in _NEXT_CONTROL_SELECTORS:
        try:
            handles += await page.locator(sel).element_handles()
        except Exception:  # noqa: BLE001
            continue
    try:
        loc = page.locator("a, button, span, div, input").filter(
            has_text=re.compile(r"^\s*NEXT\s*\W{0,3}$", re.I)
        )
        handles += await loc.element_handles()
    except Exception:  # noqa: BLE001
        pass

    result = []
    for h in handles:
        try:
            cls = (await h.get_attribute("class")) or ""
            if "BLHeader" in cls or "BLNav" in cls:
                continue
            text = (await h.inner_text() or "").strip()
            if _DATE_NAV_TEXT_RE.search(text):
                continue
            if not await h.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue
        result.append(h)
        if len(result) >= 6:
            break
    return result


async def iter_auction_listings(
    page: Page, throttle: Throttle, county_name: str, base_url: str, auction_date: date,
    save_debug_html: str | None = None,
    save_debug_with_cards: str | None = None,
):
    """Yield every AuctionListing across all result pages for one
    county+date.

    Pagination strategy (AJAX-aware -- clicks swap content in place, no
    navigation happens): repeatedly click each candidate next-control,
    wait briefly for the swapped-in content, re-parse the whole DOM, and
    yield only cases not yet seen. Stops when a full round of clicking
    yields nothing new (covers both "last page reached" and "controls
    were no-ops"), with a hard cap as a runaway guard. Clicking every
    candidate each round also covers the possibility that each section
    (Running / Waiting / Closed) has its own independent pager.

    `save_debug_html`: dump the first fetched page's raw HTML here (the
    caller passes it once per county). `save_debug_with_cards`: same,
    but only when the date actually has listings -- a quiet date's page
    has no cards and no pager, so only a with-cards capture can answer
    what the pager's real markup is if pagination still under-delivers.
    """
    url = build_url(base_url, auction_date)
    html = await fetch_page_html(page, throttle, url)
    if save_debug_html:
        try:
            with open(save_debug_html, "w", encoding="utf-8") as f:
                f.write(html)
            log.info("saved raw page HTML sample to %s", save_debug_html)
        except OSError:
            log.warning("could not save debug HTML to %s", save_debug_html, exc_info=True)

    seen_case_nos: set[str] = set()

    def _new_listings(current_html: str) -> list[AuctionListing]:
        fresh = []
        for listing in parse_listings(current_html, county_name, auction_date, url):
            if listing.case_no not in seen_case_nos:
                seen_case_nos.add(listing.case_no)
                fresh.append(listing)
        return fresh

    first_page_listings = _new_listings(html)
    if first_page_listings and save_debug_with_cards:
        try:
            with open(save_debug_with_cards, "w", encoding="utf-8") as f:
                f.write(html)
            log.info("saved with-cards page HTML sample to %s", save_debug_with_cards)
        except OSError:
            log.warning("could not save debug HTML to %s", save_debug_with_cards, exc_info=True)
    for listing in first_page_listings:
        yield listing

    total_pages = parse_total_pages(_page_text(html))
    if total_pages <= 1 and not _PAGER_HINT_RE.search(html):
        return
    if total_pages > 1:
        log.info("%s %s: pager reports %d pages", county_name, auction_date.isoformat(), total_pages)

    rounds = 0
    while rounds < 40:
        rounds += 1
        controls = await _find_next_page_controls(page)
        if not controls:
            if total_pages > 1 and rounds == 1:
                log.warning(
                    "%s %s: pager reports %d pages but no next-control matched -- "
                    "only page 1 scraped; send the fl_debug_*_withcards.html file to fix this",
                    county_name, auction_date.isoformat(), total_pages,
                )
            return
        new_this_round = 0
        for control in controls:
            try:
                await throttle.wait()  # each click is a server request
                await control.click()
                await page.wait_for_timeout(1500)  # let the AJAX swap finish
            except Exception:  # noqa: BLE001 -- a dead control just contributes nothing
                continue
            html = await page.content()
            for listing in _new_listings(html):
                new_this_round += 1
                yield listing
        if new_this_round == 0:
            return
