"""Step 2: capture listing detail from SmartMLS (connectMLS).

>>> THE DETAIL-PAGE URL IS NOT KNOWN, AND IS NOT GUESSED <<<
SmartMLS runs on connectMLS (dynaConnections), not Matrix -- confirmed
2026-08-13 from an alert email's link, which resolves to
smartmls-portal.connectmls.com and redirects to a connectMLS login. An
earlier version of this module aimed at
`matrix.smartmls.com/Matrix/Public/Portal.aspx?ID=<n>`, which is the
wrong system entirely.

Rather than swap one guessed URL for another, there is now no default:
`--listing-url-template` must be supplied, e.g.

    --listing-url-template "https://<host>/listing/detail?mls={mls_no}"

Without it every row is marked MLS_PULL_FAILED with a message saying so,
the run still completes, and the alert and assessor steps still produce
usable rows. That is deliberate -- a plausible-looking wrong URL would
404 every listing and read like an outage instead of a missing setting.

To find the template: open any listing while logged into connectMLS and
copy the address bar, replacing the MLS number with `{mls_no}`.

>>> THE FIELD LABELS ARE ALSO UNCONFIRMED <<<
Extraction is label-driven rather than selector-driven: it reads the
rendered text and pairs each known label with the value that follows it.
That survives markup changes far better than a CSS path would, but the
labels below were written for a Matrix-style layout and have not been
checked against connectMLS's display.

Confirm them with:
    python -m ct_expired_bot --mls 24139663 \
        --listing-url-template "..." --dump-html out.html

then adjust `LABELS` below, or override without editing code by passing
`--label-overrides labels.json` ({"beds": ["Bedrooms", "Total Beds"]}).

Failure policy, per the spec: a page that will not load marks the row
MLS_PULL_FAILED and the run continues. One bad listing never stops a
batch, and a failed pull never produces a partially-real row.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from .models import NA, MLS_PULL_FAILED, MlsDetail, PriceChange

log = logging.getLogger(__name__)

REMARKS_LIMIT = 200  # spec: first 200 chars of public remarks

# field name -> candidate labels as printed on the detail page, tried in
# order. First label that matches wins.
LABELS: dict[str, list[str]] = {
    # CONFIRMED 2026-08-14 off a real connectMLS "Listing" tab (302 Dover
    # Street, Bridgeport) -- the "Marketing History" block prints exactly
    # these labels. Anything still carrying an alternate spelling below is
    # a fallback, not an observation.
    "list_price_final": ["List Price", "Current List Price"],
    "list_price_original": ["Original List Price", "Orig List Price"],
    "previous_list_price": ["Previous List Price"],
    "price_last_updated": ["Price Last Updated"],
    "days_on_market": ["Days on Market", "DOM"],           # not seen on the Listing tab
    "cumulative_dom": ["Cumulative Days on Market", "CDOM", "Total DOM"],
    "list_date": ["Entered in MLS", "Start Marketing Date", "List Date"],
    "expiration_date": ["Expiration Date", "Expire Date", "Off Market Date"],
    "beds": ["Total Bedrooms", "Bedrooms", "Beds"],
    "full_baths": ["Full Baths", "Total Full Baths"],
    "half_baths": ["Half Baths", "Total Half Baths"],
    "sqft_above_grade": [
        "Sq Ft Est Heated Above Grade",
        "Above Grade Finished Area",
        "Total Sq Ft",
        "Living Area",
    ],
    "lot_size": ["Acres", "Lot Size", "Lot Size Area"],
    "year_built": ["Year Built"],
    "property_type": ["Property Type", "Property Sub Type"],
    "town": ["Town", "City"],
    "zip_code": ["Zip Code", "Postal Code", "Zip"],
    "listing_agent": ["List Agent", "Listing Agent", "Listing Member Name"],
    "brokerage": ["List Office", "Listing Office", "Brokerage"],
    "public_remarks": ["Public Remarks", "Remarks", "Marketing Remarks"],
}


def load_label_overrides(path: str | Path | None) -> dict[str, list[str]]:
    """Merge a JSON overrides file over LABELS (overrides win)."""
    if not path:
        return dict(LABELS)
    merged = dict(LABELS)
    data = json.loads(Path(path).read_text())
    for field_name, labels in data.items():
        merged[field_name] = list(labels) if isinstance(labels, list) else [str(labels)]
    return merged


def _value_after_label(text: str, label: str) -> str | None:
    """Find `label:` and return the value after it.

    The label must start a line or follow a column gap -- it is NOT
    matched mid-string. That matters more than it looks: connectMLS
    prints "List Price", "Original List Price" and "Previous List Price"
    on adjacent lines, and "List Price" is a substring of both others. A
    free-floating search finds whichever appears first in the document,
    so asking for the final list price could quietly hand back the
    original one. Anchoring makes each label match only itself.

    The value stops at a column gap or end of line, so a field printed
    with no value (connectMLS leaves "Price Last Updated:" blank when a
    listing never changed price) cannot swallow the next field's text.
    """
    pattern = re.compile(
        rf"(?:^|\n|\s{{2,}}|\|)[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.*?)"
        rf"(?=\s{{2,}}|\s*\|\s*|\n|$)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip(" :\t")
    return value or None


def extract_fields(page_text: str, labels: dict[str, list[str]]) -> dict[str, str]:
    """Label-driven extraction. Fields not found are simply absent, and
    the caller leaves them at NA -- nothing is inferred.
    """
    found: dict[str, str] = {}
    for field_name, candidates in labels.items():
        for label in candidates:
            value = _value_after_label(page_text, label)
            if value:
                found[field_name] = value
                break
    return found


_PRICE_ROW_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\D{0,40}?"
    r"(\$?[\d,]{3,})\D{1,20}?(\$?[\d,]{3,})"
)


def extract_price_history(page_text: str) -> list[PriceChange]:
    """Pull date/old/new triples out of the price-history block.

    Scoped to the text following a "Price History"-ish heading so ordinary
    price mentions elsewhere on the page cannot be mistaken for changes.
    """
    heading = re.search(r"(price history|listing history|change history)", page_text, re.I)
    scope = page_text[heading.end():] if heading else ""
    if not scope:
        return []
    changes: list[PriceChange] = []
    for match in _PRICE_ROW_RE.finditer(scope[:4000]):
        changes.append(
            PriceChange(
                date=match.group(1),
                old_price=match.group(2),
                new_price=match.group(3),
            )
        )
    return changes


class ListingUrlUnknown(RuntimeError):
    """No --listing-url-template was supplied. See the module docstring."""


def listing_url(mls_no: str, template: str | None) -> str:
    """Build the detail URL from the operator-supplied template."""
    if not template:
        raise ListingUrlUnknown(
            "No listing URL template configured. Open a listing while logged "
            "into connectMLS, copy the address, and pass it with the MLS "
            "number replaced by {mls_no}: "
            '--listing-url-template "https://<host>/...?mls={mls_no}"'
        )
    if "{mls_no}" not in template:
        raise ListingUrlUnknown(
            f"--listing-url-template must contain the {{mls_no}} placeholder; got {template!r}"
        )
    return template.replace("{mls_no}", mls_no)


async def fetch_detail(
    context: BrowserContext,
    mls_no: str,
    labels: dict[str, list[str]] | None = None,
    timeout_ms: int = 45000,
    dump_html_to: str | Path | None = None,
    url_template: str | None = None,
) -> MlsDetail:
    """Open one listing and capture it. Never raises for a bad page."""
    labels = labels or dict(LABELS)
    detail = MlsDetail(mls_no=mls_no)
    try:
        target = listing_url(mls_no, url_template)
    except ListingUrlUnknown as exc:
        detail.pull_status = MLS_PULL_FAILED
        detail.pull_error = str(exc)[:300]
        return detail
    page: Page | None = None
    try:
        page = await context.new_page()
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)  # Matrix renders detail panels client-side
        html = await page.content()
        if dump_html_to:
            Path(dump_html_to).write_text(html)
        page_text = await page.evaluate("() => document.body.innerText")
    except Exception as exc:
        log.warning("MLS pull failed for %s: %s", mls_no, exc)
        detail.pull_status = MLS_PULL_FAILED
        detail.pull_error = str(exc)[:300]
        return detail
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    normalized = re.sub(r"[ \t]+", " ", page_text or "")
    if not normalized.strip():
        detail.pull_status = MLS_PULL_FAILED
        detail.pull_error = "empty page body"
        return detail

    for field_name, value in extract_fields(normalized, labels).items():
        if field_name == "public_remarks":
            value = value[:REMARKS_LIMIT]
        setattr(detail, field_name, value)
    detail.price_history = extract_price_history(normalized)
    return detail


async def fetch_many(
    context: BrowserContext,
    mls_numbers: list[str],
    labels: dict[str, list[str]] | None = None,
    delay_seconds: float = 2.0,
    url_template: str | None = None,
) -> dict[str, MlsDetail]:
    """Sequential by design -- one authenticated session, no parallel load
    on the MLS, same posture as ct_foreclosure_bot's Throttle.
    """
    results: dict[str, MlsDetail] = {}
    for index, mls_no in enumerate(mls_numbers):
        if index:
            await asyncio.sleep(delay_seconds)
        results[mls_no] = await fetch_detail(
            context, mls_no, labels=labels, url_template=url_template
        )
        log.info(
            "[%d/%d] MLS %s: %s",
            index + 1, len(mls_numbers), mls_no, results[mls_no].pull_status,
        )
    return results
