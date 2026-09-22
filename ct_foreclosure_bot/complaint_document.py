"""Fetches and reads the case-initiating Complaint document.

Used for the recent-lender-complaint HOT rule (see lead_ranking.py): the
complaint pleading itself is the only place two facts the user wants on
the spreadsheet actually appear -- the principal balance owed on the
note, and the date since which principal & interest installments have
gone unpaid. Neither shows up in docket entry text.

Unlike the court-generated Order document (clean typeset text, single
known template), complaints are attorney-drafted freeform pleadings, so:

  - The relevant paragraphs ("The ... Note is in default ... has failed
    to pay installments of principal and interest due June 1, 2025 and
    every installment thereafter"; "The principal balance due ... is
    $XXX,XXX.XX") are usually within the first few pages, but not on a
    fixed page -- the first MAX_PAGES pages are OCR'd, not just page 1.
  - The phrasing patterns below are a best-effort net over the common
    CT foreclosure-complaint boilerplate variants, same caveat as the
    EMAP patterns in motions.py: a miss just leaves the cell blank (the
    complaint doc URL is on the row for manual reading), and a match is
    a strong hint, not a guaranteed parse.
"""

import re
from datetime import date, datetime

import fitz
import pytesseract
from PIL import Image
from playwright.async_api import BrowserContext

from .throttle import Throttle
from .worksheet import RENDER_DPI

MAX_PAGES = 4  # complaint bodies run a handful of pages; the money/default paragraphs are early

# Ordered, first match wins. All matched against the raw (not uppercased)
# OCR text with re.I, so "$ 123,456.78" spacing/casing variants are fine.
PRINCIPAL_PATTERNS = [
    re.compile(r"principal\s+balance\s+(?:due|owed|of|in\s+the\s+amount\s+of)?[^$\d]{0,40}\$?\s*([\d,]{4,}(?:\.\d{2})?)", re.I),
    re.compile(r"unpaid\s+principal[^$\d]{0,60}\$?\s*([\d,]{4,}(?:\.\d{2})?)", re.I),
    re.compile(r"principal\s+(?:amount|sum)\s+of\s+\$?\s*([\d,]{4,}(?:\.\d{2})?)", re.I),
]

_DATE = r"([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})"

# Day-of-month digits tesseract routinely mangles on these scans: 1 -> l/I/|
# and 0 -> o/O. Confined to the day position, so a month name like "October"
# is never touched.
_DAY = r"[0-9lI|oO]{1,2}"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*"
# Slashes come back with stray spaces ("07/01/ 2025"), so allow them.
_DATE_OCR = (
    rf"({_MONTH}\s+{_DAY}\s*,?\s*\d{{4}}"
    rf"|\d{{1,2}}\s*/\s*\d{{1,2}}\s*/\s*\d{{2,4}})"
)

# Confirmed against 20 real CT complaints (scripts/sample_complaints.py),
# every one of which OCR'd cleanly enough for the principal patterns to hit
# while these missed. The dominant boilerplate is:
#
#   "The installment of principal and interest that was due on December 1,
#    2025, and each and every month thereafter..."
#
# The old patterns lost it three ways, all visible in that sample:
#   1. "principal interest" with no "and" -- over half the date-bearing
#      complaints drop the conjunction, and the old pattern required it.
#   2. "that was due on" -- the old pattern demanded "due" adjacent to
#      "interest" and could not cross interposed words.
#   3. OCR digit mangling in the day -- "February |, 2026", "07/01/ 2025".
#
# The gap before "due" stays short and non-greedy on purpose. The same
# paragraph often continues "...installments of principal and interest, and
# the Plaintiff has exercised its option to accelerate the balance due on
# said Note", and a loose gap would walk past the comma and bind "due" to
# the acceleration clause, inventing a date from an unrelated sentence.
UNPAID_SINCE_PATTERNS = [
    re.compile(
        rf"installments?\s+of\s+principal\s+(?:and\s+)?interest"
        rf"[^.]{{0,30}}?\bdue\s+(?:on\s+)?{_DATE_OCR}", re.I),
    # "...has failed to pay the installment due June 1, 2025..."
    re.compile(rf"installments?\s+(?:of\s+principal\s+and\s+interest\s+)?due\s+(?:on\s+|thereon\s+)?{_DATE_OCR}", re.I),
    # "...the Note is in default since January 1, 2026..." / "...as of..."
    re.compile(rf"in\s+default\s+(?:since|as\s+of|beginning)\s+{_DATE_OCR}", re.I),
    re.compile(rf"failed\s+to\s+(?:pay|make)[^.]{{0,80}}?due\s+(?:on\s+)?{_DATE_OCR}", re.I),
]


async def fetch_complaint_text(context: BrowserContext, throttle: Throttle, document_url: str) -> str:
    await throttle.wait()
    resp = await context.request.get(document_url, timeout=30000)
    pdf_bytes = await resp.body()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    texts = []
    for page in doc.pages(0, min(MAX_PAGES, doc.page_count)):
        pix = page.get_pixmap(dpi=RENDER_DPI)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        texts.append(pytesseract.image_to_string(image))
    return "\n".join(texts)


def extract_principal_amount(text: str) -> float | None:
    for pattern in PRINCIPAL_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            # Sanity bound: a mortgage principal under $1,000 or over $100M
            # is far more likely an OCR mangle than a real figure.
            if 1_000 <= value <= 100_000_000:
                return value
    return None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip().rstrip(",.")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return _parse_date_ocr(raw)


def _parse_date_ocr(raw: str) -> date | None:
    """Second pass for scans tesseract mangled.

    Only two corrections, both confined to positions that must be numeric,
    so a month name can never be rewritten: stray whitespace around the
    slashes of a numeric date, and letter-for-digit substitutions in the
    day (1 -> l/I/|, 0 -> o/O) when a comma and four-digit year follow.
    """
    cleaned = re.sub(r"\s*/\s*", "/", raw)
    cleaned = cleaned.replace("|", "1")
    cleaned = re.sub(r"(?<=\s)([lI])(?=\s*,?\s*\d{4})", "1", cleaned)
    cleaned = re.sub(r"(?<=\s)([oO])(?=\s*,?\s*\d{4})", "0", cleaned)
    # "Sept 1, 2025" parses under neither %b ("Sep") nor %B ("September").
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.I)
    if cleaned == raw:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _plausible(d: date, today: date | None = None) -> bool:
    """A default date in the future, or older than the modern mortgage
    record, is a mis-parse rather than a fact about the loan. Rejecting it
    keeps a bad OCR out of the sheet instead of onto a call list.
    """
    today = today or date.today()
    return date(1990, 1, 1) <= d <= today


def extract_unpaid_since(text: str, today: date | None = None) -> str | None:
    """The date since which P&I has gone unpaid, as an ISO string.

    Returns None rather than the raw matched text when the date will not
    parse: an unparseable fragment in a date column is worse than a blank,
    because the row still carries complaint_doc_url for manual reading.
    """
    for pattern in UNPAID_SINCE_PATTERNS:
        for m in pattern.finditer(text):
            parsed = _parse_date(m.group(1))
            if parsed and _plausible(parsed, today):
                return parsed.isoformat()
    return None
