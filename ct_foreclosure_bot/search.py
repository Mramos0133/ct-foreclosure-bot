"""Drives PropertyAddressSearch.aspx: form submission + GridView paging.

Confirmed during manual exploration (see project notes):
  - Case Type dropdown: "Foreclosure Cases" | "Housing Cases"
  - City/Town is a required free-text field (min 3 chars) that does
    substring matching against the town name (e.g. "Hartford" also
    matches "East Hartford" / "West Hartford"), so results here are
    filtered back down to an exact City/Town match per row.
  - Results grid is ctl00_ContentPlaceHolder1_gvPropertyResults, a
    standard ASP.NET GridView, 200 rows/page, paged via
    __doPostBack('ctl00$ContentPlaceHolder1$gvPropertyResults','Page$N').
    The postback is a full page reload, not a partial AJAX update.
  - There is an AjaxControlToolkit "NoBot" control on the search form.
    A couple of synthetic mouse moves before clicking Search were enough
    to get through it in testing; no token/challenge extraction needed.
"""

import re
from playwright.async_api import Page

from .browser import BASE_URL
from .models import CaseListing
from .throttle import Throttle

SEARCH_URL = f"{BASE_URL}/PropertyAddressSearch.aspx"

RECORDS_RE = re.compile(r"Records:\s*[\d,]+\s*-\s*[\d,]+\s*of\s*([\d,]+)", re.I)
RESULTS_PER_PAGE = 200


async def _human_pause(page: Page) -> None:
    """A few synthetic mouse moves to satisfy the NoBot control."""
    await page.mouse.move(80, 120)
    await page.mouse.move(250, 260, steps=8)
    await page.mouse.move(420, 380, steps=8)


async def open_search_form(page: Page, throttle: Throttle) -> None:
    await throttle.wait()
    await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)


async def submit_town_search(
    page: Page,
    throttle: Throttle,
    town: str,
    case_type: str = "Foreclosure Cases",
    property_type: str = "All Properties",
    case_status: str = "All Cases",
) -> str:
    """Fill and submit the search form for one town. Returns result page HTML."""
    await open_search_form(page, throttle)
    await _human_pause(page)

    await page.select_option("#ctl00_ContentPlaceHolder1_ddlCaseType", label=case_type)
    await page.fill("#ctl00_ContentPlaceHolder1_txtCityTown", town)
    await page.select_option("#ctl00_ContentPlaceHolder1_ddlPropertyType", label=property_type)
    await page.select_option("#ctl00_ContentPlaceHolder1_ddlCaseStatus", label=case_status)

    await page.mouse.move(500, 450, steps=5)

    await throttle.wait()
    async with page.expect_navigation(wait_until="networkidle", timeout=30000):
        await page.click("#ctl00_ContentPlaceHolder1_btnSubmit")

    return await page.content()


async def goto_results_page(page: Page, throttle: Throttle, page_number: int) -> str:
    """Trigger the GridView postback for a given 1-based results page number."""
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError(f"invalid page_number: {page_number!r}")
    await throttle.wait()
    async with page.expect_navigation(wait_until="networkidle", timeout=30000):
        await page.evaluate(
            "__doPostBack('ctl00$ContentPlaceHolder1$gvPropertyResults', 'Page$%d')" % page_number
        )
    return await page.content()


def parse_total_records(html: str) -> int:
    """Parse the "Records: 1-200 of 2196" label.

    The number itself lives in a sibling <span>, separated from the label
    by HTML tags (not by plain whitespace), so this matches against the
    rendered text (BeautifulSoup get_text), not the raw HTML.
    """
    from bs4 import BeautifulSoup

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = RECORDS_RE.search(text)
    return int(m.group(1).replace(",", "")) if m else 0


def parse_results_table(html: str, search_town: str, case_type: str = "Foreclosure Cases") -> list[CaseListing]:
    """Parse the results grid.

    Columns (confirmed against the live site -- there is no separate "Case
    Type" column; that's a search filter, not a results column):
    City/Town | Street Address | Zip Code | Case Name | Docket No. |
    Property Type | Disposition -- 7 cells per data row.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="ctl00_ContentPlaceHolder1_gvPropertyResults")
    if table is None:
        return []

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 7:
            continue  # header / pager rows
        docket_link = cells[4].find("a")
        if docket_link is None:
            continue
        docket_no_match = re.search(r"DocketNo=([\w-]+)", docket_link.get("href", ""))
        if not docket_no_match:
            continue
        rows.append(
            CaseListing(
                city_town=cells[0].get_text(strip=True),
                street_address=cells[1].get_text(strip=True),
                zip_code=cells[2].get_text(strip=True),
                case_type=case_type,
                case_name=cells[3].get_text(strip=True),
                docket_no=docket_no_match.group(1),
                property_type=cells[5].get_text(strip=True),
                disposition=cells[6].get_text(" ", strip=True),
                search_town=search_town,
            )
        )
    return rows


_DIRECTION_ABBREVIATIONS = {
    "E.": "EAST",
    "W.": "WEST",
    "N.": "NORTH",
    "S.": "SOUTH",
}


def _norm_town(t: str) -> str:
    """Normalize a town name for comparison.

    The results grid abbreviates directional prefixes (e.g. "E. HARTFORD"
    for East Hartford, "N. BRANFORD" for North Branford), while the
    canonical names used to drive searches (towns.py) spell them out. Both
    sides are run through this before comparing.
    """
    upper = t.upper().strip()
    words = upper.split()
    if words and words[0] in _DIRECTION_ABBREVIATIONS:
        words[0] = _DIRECTION_ABBREVIATIONS[words[0]]
    normalized = " ".join(words)
    return re.sub(r"[^A-Z ]", "", normalized).strip()


async def iter_town_cases(
    page: Page,
    throttle: Throttle,
    town: str,
    case_type: str = "Foreclosure Cases",
    property_type: str = "All Properties",
    case_status: str = "All Cases",
):
    """Yield CaseListing rows for a town, across all result pages.

    Rows are filtered to those whose City/Town cell exactly matches the
    requested town (ignoring case/punctuation/"E."-style abbreviation),
    since the site's substring search over-matches (e.g. "Hartford" also
    returns East/West Hartford rows). This keeps a single town's results
    precise; cross-town duplicates from other town queries are handled by
    docket-number de-duplication at the checkpoint layer.
    """
    html = await submit_town_search(page, throttle, town, case_type, property_type, case_status)
    total = parse_total_records(html)
    num_pages = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE) if total else 1

    target_norm = _norm_town(town)
    page_num = 1
    while True:
        for row in parse_results_table(html, town, case_type):
            if _norm_town(row.city_town) == target_norm:
                yield row
        if page_num >= num_pages:
            break
        page_num += 1
        html = await goto_results_page(page, throttle, page_num)
