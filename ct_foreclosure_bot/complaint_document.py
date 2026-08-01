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
UNPAID_SINCE_PATTERNS = [
    # "...has failed to pay the installment(s) of principal and interest due June 1, 2025..."
    re.compile(rf"installments?\s+(?:of\s+principal\s+and\s+interest\s+)?due\s+(?:on\s+|thereon\s+)?{_DATE}", re.I),
    # "...the Note is in default since January 1, 2026..." / "...in default as of..."
    re.compile(rf"in\s+default\s+(?:since|as\s+of|beginning)\s+{_DATE}", re.I),
    re.compile(rf"failed\s+to\s+(?:pay|make)[^.]{{0,80}}?due\s+(?:on\s+)?{_DATE}", re.I),
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
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def extract_unpaid_since(text: str) -> str | None:
    """The date since which P&I has gone unpaid, as an ISO string when the
    matched text parses cleanly, else the raw matched text (still useful
    to a human reading the row).
    """
    for pattern in UNPAID_SINCE_PATTERNS:
        m = pattern.search(text)
        if m:
            parsed = _parse_date(m.group(1))
            return parsed.isoformat() if parsed else m.group(1).strip()
    return None
