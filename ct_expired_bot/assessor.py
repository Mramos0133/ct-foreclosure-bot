"""Step 3: owner name and mailing address from the town assessor's card.

Unlike alerts.py and mls.py, the selectors in here are CONFIRMED, not
guessed. They were read off a live Vision (VGSI) property card on
2026-08-04 -- https://gis.vgsi.com/stamfordct/Parcel.aspx?pid=1146 -- and
every ID below was observed returning a real value on that page:

    MainContent_lblLocation            '19 ABEL AVENUE'
    MainContent_lblGenAssessment       '$468,670'
    MainContent_lblPrice               '$725,000'
    MainContent_lblSaleDate            '12/12/2024'
    MainContent_ctl02_lblYearBuilt     '1973'
    MainContent_ctl02_lblBldArea       '2,544'
    MainContent_ctl02_grdCns           'Total Bedrooms:' / 'Total Bthrms:' / ...

Only Vision is implemented. That is a deliberate scope line, not an
oversight: 77 of the 169 CT towns are on Vision (see portals.py), and the
other vendors have incompatible layouts that would each need their own
confirmed selector set. A town on any other platform returns
PORTAL_UNAVAILABLE and lands in the review file, which is the spec's own
answer for a portal this bot cannot read -- far better than a plausible
guess at a layout nobody has checked.

Verification is not optional here (spec Step 3). A card is only accepted
when its printed location matches the listing address; when several
parcels match, year built and living area from the MLS row must single
one out, or the row becomes NEEDS_MANUAL_REVIEW with the reason recorded.
"""

import logging
import re

from playwright.async_api import BrowserContext, Page

from .models import (
    NA,
    NEEDS_MANUAL_REVIEW,
    PORTAL_UNAVAILABLE,
    MlsDetail,
    OwnerRecord,
    TownPortal,
    parse_money,
)
from .scoring import normalize_address, sqft_mismatch_note

log = logging.getLogger(__name__)

MAX_CANDIDATES = 6  # cards fetched while disambiguating one address

# Street-type words dropped from the SEARCH query. Vision matches the
# address string literally against whatever the town happens to store,
# and towns disagree: Bridgeport holds "575 BURNSFORD AV", Hamden holds
# "75 WASHINGTON AVE". Searching "575 Burnsford Avenue" returns zero
# hits in both. Searching "575 Burnsford" returns the right parcel in
# both, and the full address is still used to verify the match, so
# dropping the suffix widens the search without loosening the check.
_STREET_TYPE_WORDS = {
    "ST", "STREET", "AVE", "AV", "AVENUE", "RD", "ROAD", "DR", "DRIVE",
    "LN", "LANE", "CT", "COURT", "PL", "PLACE", "BLVD", "BOULEVARD",
    "TER", "TERR", "TERRACE", "CIR", "CIRCLE", "HWY", "HIGHWAY",
    "PKWY", "PARKWAY", "TRL", "TRAIL", "SQ", "SQUARE", "WAY",
    "TPKE", "TURNPIKE", "EXT", "EXTENSION", "PATH", "ROW", "RUN",
}


def search_query(address: str) -> str:
    """Street number + name, with the street-type suffix removed."""
    tokens = re.sub(r"[^A-Za-z0-9 ]", " ", (address or "")).split()
    while tokens and tokens[-1].upper() in _STREET_TYPE_WORDS:
        tokens.pop()
    return " ".join(tokens) if tokens else (address or "").strip()

# Confirmed Vision card element IDs (see module docstring).
VISION_IDS = {
    "location": "MainContent_lblLocation",
    "owner": "MainContent_lblOwner",
    "co_owner": "MainContent_lblCoOwner",
    "mailing": "MainContent_lblAddr1",
    "assessed_value": "MainContent_lblGenAssessment",
    "last_sale_price": "MainContent_lblPrice",
    "last_sale_date": "MainContent_lblSaleDate",
    "year_built": "MainContent_ctl02_lblYearBuilt",
    "living_area": "MainContent_ctl02_lblBldArea",
    "construction_grid": "MainContent_ctl02_grdCns",
}
SEARCH_ADDRESS_INPUT = "#MainContent_txtSearchAddress"
# The real submit input sits inside <span style="display: none;">; the
# visible control is a styled span whose onclick is literally
# "$('#MainContent_btnSubmit').click();". Playwright's normal click
# refuses a hidden element, so the submit is dispatched in-page exactly
# as the site's own button does it. Confirmed against Stamford 2026-08-04.
SEARCH_SUBMIT = "#MainContent_btnSubmit"


async def _text(page: Page, element_id: str) -> str:
    """Text of an element by ID, or NA when it is absent/blank."""
    try:
        locator = page.locator(f"#{element_id}")
        if await locator.count() == 0:
            return NA
        value = (await locator.first.inner_text()).strip()
        return value or NA
    except Exception:
        return NA


async def _construction_value(page: Page, *labels: str) -> str:
    """Read a row out of the construction-details grid by its label."""
    try:
        grid = page.locator(f"#{VISION_IDS['construction_grid']}")
        if await grid.count() == 0:
            return NA
        text = await grid.first.inner_text()
    except Exception:
        return NA
    for line in text.splitlines():
        for label in labels:
            if line.strip().lower().startswith(label.lower()):
                value = line.split(":", 1)[-1] if ":" in line else line[len(label):]
                # "6 Bedrooms" -> "6"; a bare "2" stays "2".
                number = re.search(r"\d+(?:\.\d+)?", value)
                return number.group(0) if number else (value.strip() or NA)
    return NA


def _split_mailing(raw: str) -> tuple[str, str, str, str]:
    """Vision prints the mailing address as street / "CITY, ST ZIP" lines."""
    if not raw or raw == NA:
        return NA, NA, NA, NA
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return NA, NA, NA, NA
    street = lines[0]
    city = state = zip_code = NA
    if len(lines) > 1:
        match = re.match(r"^(.*?),?\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", lines[-1].strip())
        if match:
            city, state, zip_code = match.group(1).strip(), match.group(2), match.group(3)
        else:
            city = lines[-1]
    return street, city, state, zip_code


async def _search_candidates(page: Page, portal_url: str, street_address: str) -> list[tuple[str, str]]:
    """Return [(printed_address, parcel_url)] for an address search."""
    search_url = portal_url.rstrip("/") + "/Search.aspx"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
    await page.fill(SEARCH_ADDRESS_INPUT, search_query(street_address))
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=40000):
            await page.eval_on_selector(SEARCH_SUBMIT, "el => el.click()")
    except Exception:
        # No navigation fired: either the search returned in place or the
        # submit did not take. Both are handled by reading whatever the
        # page shows now rather than assuming a result.
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

    # A unique hit redirects straight to the card; several hits render a grid.
    if "Parcel.aspx" in (page.url or ""):
        return [(await _text(page, VISION_IDS["location"]), page.url)]

    rows = await page.eval_on_selector_all(
        "a[href*='Parcel.aspx']",
        "els => els.map(e => [e.textContent.trim(), e.href])",
    )
    return [(text, href) for text, href in rows if text]


async def _read_card(page: Page, parcel_url: str) -> OwnerRecord:
    await page.goto(parcel_url, wait_until="domcontentloaded", timeout=40000)
    owner = await _text(page, VISION_IDS["owner"])
    co_owner = await _text(page, VISION_IDS["co_owner"])
    # A co-owner is part of the printed name, and names.py treats the
    # combined string as multiple owners -- which is exactly right, it
    # must not be split into one person for the vendor.
    if co_owner not in (NA, "") and owner != NA:
        owner = f"{owner} & {co_owner}"

    street, city, state, zip_code = _split_mailing(await _text(page, VISION_IDS["mailing"]))
    return OwnerRecord(
        owner_name=owner,
        mailing_address=street,
        mailing_city=city,
        mailing_state=state,
        mailing_zip=zip_code,
        assessed_value=await _text(page, VISION_IDS["assessed_value"]),
        last_sale_date=await _text(page, VISION_IDS["last_sale_date"]),
        last_sale_price=await _text(page, VISION_IDS["last_sale_price"]),
        assessor_living_area=await _text(page, VISION_IDS["living_area"]),
        assessor_year_built=await _text(page, VISION_IDS["year_built"]),
        assessor_beds=await _construction_value(page, "Total Bedrooms"),
        assessor_baths=await _construction_value(page, "Total Bthrms", "Total Baths"),
        portal_url=parcel_url,
    )


def _year_built_matches(card_year: str, mls: MlsDetail) -> bool:
    a, b = parse_money(card_year), parse_money(mls.year_built)
    return a is not None and b is not None and int(a) == int(b)


def _sqft_close(card_sqft: str, mls: MlsDetail, tolerance_pct: float = 25.0) -> bool:
    a, b = parse_money(card_sqft), parse_money(mls.sqft_above_grade)
    if a is None or b is None or b <= 0:
        return False
    return abs(a - b) / b * 100.0 <= tolerance_pct


async def lookup_owner(
    context: BrowserContext,
    portal: TownPortal,
    street_address: str,
    mls: MlsDetail,
    unit: str = "",
) -> OwnerRecord:
    """Find and verify the assessor card for one listing.

    Never raises: a portal that is down, blocking, or on an unsupported
    platform returns a PORTAL_UNAVAILABLE record and the run continues.
    """
    if portal.platform != "Vision" or not portal.portal_url:
        return OwnerRecord(
            status=PORTAL_UNAVAILABLE,
            owner_name=PORTAL_UNAVAILABLE,
            notes=[
                f"{portal.town} is on '{portal.platform}', which this bot does "
                f"not read. Look the card up manually: {portal.portal_url or portal.search_notes}"
            ],
        )
    if not street_address:
        return OwnerRecord(
            status=NEEDS_MANUAL_REVIEW,
            owner_name=NEEDS_MANUAL_REVIEW,
            notes=["No street address captured from the alert email to search on."],
        )

    page = await context.new_page()
    try:
        candidates = await _search_candidates(page, portal.portal_url, street_address)
    except Exception as exc:
        log.warning("assessor search failed for %s (%s): %s", street_address, portal.town, exc)
        await page.close()
        return OwnerRecord(
            status=PORTAL_UNAVAILABLE,
            owner_name=PORTAL_UNAVAILABLE,
            notes=[f"Portal error during search: {str(exc)[:200]}"],
        )

    try:
        if not candidates:
            return OwnerRecord(
                status=NEEDS_MANUAL_REVIEW,
                owner_name=NEEDS_MANUAL_REVIEW,
                notes=[f"No parcel found for '{street_address}' on {portal.portal_url}"],
            )

        target = normalize_address(street_address)
        exact = [c for c in candidates if normalize_address(c[0]) == target]

        # A condo building lists one parcel per unit ("75 WASHINGTON AVE
        # #1101" ... "#1210"), so the street address alone matches twenty
        # parcels equally well and year-built/sqft cannot separate them --
        # they are usually identical. The unit number is the only thing
        # that can, and the export carries it.
        if unit and len(candidates) > 1:
            wanted = normalize_address(unit)
            unit_hits = [
                c for c in candidates
                if wanted and wanted in normalize_address(c[0]).split()
            ]
            if len(unit_hits) == 1:
                record = await _read_card(page, unit_hits[0][1])
                record.add_note(f"Matched unit {unit!r} among {len(candidates)} parcels.")
                return _finalize(record, street_address, mls)

        if len(exact) == 1:
            record = await _read_card(page, exact[0][1])
            return _finalize(record, street_address, mls)

        # Either several parcels print the same address (condo/multi-family)
        # or none match exactly. Both need the MLS data to single one out.
        pool = (exact or candidates)[:MAX_CANDIDATES]
        confirmed: list[OwnerRecord] = []
        for _, url in pool:
            try:
                record = await _read_card(page, url)
            except Exception:
                continue
            if _year_built_matches(record.assessor_year_built, mls) and _sqft_close(
                record.assessor_living_area, mls
            ):
                confirmed.append(record)

        if len(confirmed) == 1:
            record = confirmed[0]
            record.add_note(
                f"{len(pool)} parcels matched the address; confirmed one by "
                "year built + living area against the MLS row."
            )
            return _finalize(record, street_address, mls)

        return OwnerRecord(
            status=NEEDS_MANUAL_REVIEW,
            owner_name=NEEDS_MANUAL_REVIEW,
            portal_url=portal.portal_url,
            notes=[
                f"{len(candidates)} parcels matched '{street_address}' and "
                f"{len(confirmed)} could be confirmed against MLS year built "
                "+ sqft. Refusing to pick the closest guess."
            ],
        )
    finally:
        try:
            await page.close()
        except Exception:
            pass


def _finalize(record: OwnerRecord, street_address: str, mls: MlsDetail) -> OwnerRecord:
    """Apply the spec's post-match checks to an accepted card."""
    note = sqft_mismatch_note(mls, record)
    if note:
        record.add_note(note)
    if record.owner_name in (NA, ""):
        record.status = NEEDS_MANUAL_REVIEW
        record.owner_name = NEEDS_MANUAL_REVIEW
        record.add_note("Card loaded but printed no owner name.")
    return record
