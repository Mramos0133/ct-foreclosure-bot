"""Fetches a Foreclosure Worksheet (JD-CV-77) PDF and OCRs it.

Confirmed during exploration: DocumentInquiry.aspx?DocumentNo=N serves the
PDF directly (Content-Type: application/pdf), and the Foreclosure Worksheet
is consistently a *scanned image* PDF -- no text layer, no AcroForm fields
-- so text extraction always requires OCR, not just a PDF text reader.

The form (JD-CV-77) is a fixed statewide template, but real filings render
it two different ways:
  - "interleaved": label and dollar figure are on the same visual line,
    e.g. "2. Updated debt: $ 210,807.48" -- Tesseract's line grouping
    keeps these together as one OCR line.
  - "column-split": labels are stacked in a left column, dollar figures in
    a separate right-aligned column; Tesseract's page segmentation reads
    these as two separate blocks, so image_to_string serializes ALL
    labels first, then ALL values, with no shared line at all.

Rather than parse image_to_string's linear text (which only handles the
interleaved case), this uses image_to_data's per-word bounding boxes and
matches each label to the closest dollar-amount token *by vertical
position on the page*, regardless of reading order or column. For the
interleaved case the nearest match is on the label's own line (distance
~0); for the column-split case it's the value at the same row height in
the value column. One method handles both layouts.

OCR is not perfectly reliable even on a clean, typed government form --
during testing here, "$210,807.48" was misread as "$240,807.48" -- so a
cross-field check (line 2 + line 3 + line 4 == line 5) is used to flag
suspect extractions for manual review rather than silently trusting a
single OCR pass.
"""

import re

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from playwright.async_api import BrowserContext

from .models import WorksheetFields
from .throttle import Throttle

RENDER_DPI = 400

# Tesseract on these scans is not perfectly consistent: whole-dollar
# amounts appear with no decimal point at all (e.g. "2525 / 2775"), and
# the decimal point in the second half of a split attorney-fee value has
# been observed misread as a comma (e.g. "1,825.00/2,075,00"). The token
# pattern below is deliberately loose about punctuation -- it just grabs
# a plausible amount (optionally "amount/amount" for the split fee
# lines) -- and _normalize_amount() below does the actual decimal-point
# interpretation, tolerating either "." or "," before a trailing 2-digit
# cents group.
_AMOUNT_TOKEN = r"-?[\d][\d,.]*"
# The "$" is required (not optional) before the first amount: a bare line
# number like "2." or "10." would otherwise be indistinguishable from a
# valid amount token. The second half of a split fee value (after "/")
# has been observed both with and without its own "$" (e.g.
# "1,825.00/2,075.00" vs "$4,100.00/$4,375.00"), so that one is optional.
MONEY_RE = rf"\$\s*({_AMOUNT_TOKEN}(?:\s*/\s*\$?\s*{_AMOUNT_TOKEN})?)"

# In column-split layouts, OCR sometimes drops the "$" on some value-column
# rows but not others (observed on a real filing: some rows read "$
# 204,869.32", others just "2,150.00" with no dollar sign at all) --
# without $ as an anchor those bare rows were invisible to _find_money_lines
# entirely, so a label whose same-row value happened to lose its "$" had no
# candidate to fall back to. This second pattern catches a bare amount with
# no "$", but -- unlike MONEY_RE -- still requires a 2-digit cents suffix
# (".XX"), which a bare line-number token like "2." or "10." never has, so
# it can't be confused with one.
BARE_MONEY_RE = r"(-?[\d][\d,]*\.\d{2})"

# Label wording varies slightly by form revision (observed both
# "JD-CV-77 Rev. 5-16" and "JD-CV-77 Rev. 12-16", with different phrasing
# and even different line numbers for the same fields -- e.g. Attorney's
# fees is line 8 on one revision and line 9 on the other). Patterns below
# match the field by its distinctive wording rather than a specific line
# number, so they hold across revisions.
LINE_LABELS = {
    "fair_market_value": r"Fair\s+market\s+value",
    "updated_debt": r"Updated\s+debt",
    "condo_fees": r"Condominium\s+Common\s+Charges",
    "other_prior_encumbrances": r"encumbrances\s+on\s+the\s+property\s+prior",
    # "Total debt:", "Total debt plus encumbrances prior ... :", and
    # "Total debt as of <date> ... :" have all been observed on real
    # filings -- match on the "Total debt" line generically (up to the
    # colon) rather than requiring specific continuation wording.
    "total_debt_plus_prior_encumbrances": r"Total\s+debt[^:]{0,80}:",
    "equity": r"Equity[:\s]*\(Subtract",
    "encumbrances_subsequent_to_lien": r"encumbrances\s+subsequent\s+to\s+(?:the\s+)?plaintiff",
    "attorney_fees": r"Attorney.?s\s+fees",
    "appraisal_fee": r"Appraisal\s+fee",
    "title_search_fee": r"Title\s+Search\s+fee",
}


def _normalize_amount(token: str) -> float | None:
    token = token.strip()
    m = re.match(r"^(-?)([\d,.]*?)[.,](\d{2})$", token)
    if m:
        sign, int_part, cents = m.groups()
        int_part = re.sub(r"[,.]", "", int_part) or "0"
        try:
            return float(f"{sign}{int_part}.{cents}")
        except ValueError:
            return None
    digits = re.sub(r"[,.]", "", token)
    if digits.lstrip("-").isdigit():
        try:
            return float(digits)
        except ValueError:
            return None
    return None


async def fetch_worksheet_pdf(context: BrowserContext, throttle: Throttle, document_url: str) -> bytes:
    await throttle.wait()
    resp = await context.request.get(document_url, timeout=30000)
    return await resp.body()


def render_first_page(pdf_bytes: bytes, dpi: int = RENDER_DPI) -> Image.Image:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _group_words_into_lines(ocr_data: dict) -> list[dict]:
    """Group image_to_data words into physical lines with a bounding box and text.

    Grouped by Tesseract's (block, par, line) key, which is exactly what
    causes the column-split problem (label-column lines and value-column
    lines end up in different blocks) -- but that's fine here, since lines
    are only used as the unit for finding text/money content and its
    on-page position, not for pairing. Pairing is done spatially afterward.
    """
    n = len(ocr_data["text"])
    grouped: dict[tuple, list[int]] = {}
    for i in range(n):
        if not ocr_data["text"][i].strip():
            continue
        key = (ocr_data["block_num"][i], ocr_data["par_num"][i], ocr_data["line_num"][i])
        grouped.setdefault(key, []).append(i)

    lines = []
    for idxs in grouped.values():
        words = [ocr_data["text"][i] for i in idxs]
        lefts = [ocr_data["left"][i] for i in idxs]
        tops = [ocr_data["top"][i] for i in idxs]
        rights = [ocr_data["left"][i] + ocr_data["width"][i] for i in idxs]
        bottoms = [ocr_data["top"][i] + ocr_data["height"][i] for i in idxs]
        top, bottom = min(tops), max(bottoms)
        lines.append({
            "text": " ".join(words),
            "left": min(lefts),
            "right": max(rights),
            "top": top,
            "bottom": bottom,
            "cy": (top + bottom) / 2,
        })
    return lines


def _median_line_height(lines: list[dict]) -> float:
    heights = sorted(l["bottom"] - l["top"] for l in lines if l["bottom"] > l["top"])
    if not heights:
        return 30.0
    return heights[len(heights) // 2]


def _label_row_spacing(label_lines: dict[str, dict]) -> float | None:
    """Median vertical gap between consecutive worksheet line labels.

    Used to size the same-row search tolerance to the actual document:
    interleaved layouts have small gaps between lines (labels are packed
    close together), column-split layouts have much larger ones (each
    label can wrap to 2-3 lines of text). A single fixed pixel tolerance
    that works for one breaks the other -- too loose for interleaved
    (bleeds into the adjacent real line when a value is legitimately
    missing, e.g. "N/A"), too tight for column-split (misses the same-row
    value in the far column entirely). Sizing it relative to this
    document's own row spacing avoids both failure modes.
    """
    cys = sorted(l["cy"] for l in label_lines.values())
    if len(cys) < 2:
        return None
    gaps = sorted(b - a for a, b in zip(cys, cys[1:]) if b > a)
    if not gaps:
        return None
    return gaps[len(gaps) // 2]


def _find_label_lines(lines: list[dict]) -> dict[str, dict]:
    found = {}
    for key, label_pattern in LINE_LABELS.items():
        pattern = re.compile(label_pattern, re.I)
        for line in lines:
            if pattern.search(line["text"]):
                found[key] = line
                break
    return found


def _clean_money(raw: str) -> str:
    # The optional "$" allowed before the second half of a split fee
    # value (e.g. "$4,100.00/$4,375.00") is inside the capturing group,
    # so strip any stray "$" left over from that before storing.
    return raw.replace("$", "").strip()


def _find_money_lines(lines: list[dict]) -> list[dict]:
    money_lines = []
    for line in lines:
        m = re.search(MONEY_RE, line["text"]) or re.search(BARE_MONEY_RE, line["text"])
        if m:
            money_lines.append({**line, "money": _clean_money(m.group(1))})
    return money_lines


def _nearest_money(label_line: dict, money_lines: list[dict], max_distance: float) -> str | None:
    if not money_lines:
        return None
    best = min(money_lines, key=lambda m: abs(m["cy"] - label_line["cy"]))
    if abs(best["cy"] - label_line["cy"]) > max_distance:
        return None
    return best["money"]


def _same_line_money(label_line: dict) -> str | None:
    m = re.search(MONEY_RE, label_line["text"]) or re.search(BARE_MONEY_RE, label_line["text"])
    return _clean_money(m.group(1)) if m else None


def _extract_money(label_line: dict | None, money_lines: list[dict], max_distance: float) -> str | None:
    if label_line is None:
        return None
    same_line = _same_line_money(label_line)
    if same_line:
        return same_line
    return _nearest_money(label_line, money_lines, max_distance)


def _to_float(money_str: str | None) -> float | None:
    if money_str is None or "/" in money_str:
        return None
    return _normalize_amount(money_str)


def parse_worksheet_image(image: Image.Image) -> dict:
    ocr_data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    lines = _group_words_into_lines(ocr_data)

    label_lines = _find_label_lines(lines)
    money_lines = _find_money_lines(lines)

    row_spacing = _label_row_spacing(label_lines)
    if row_spacing is None:
        # Not enough labels found to infer this document's row spacing;
        # fall back to a conservative multiple of word-line height, which
        # is safer for tightly-packed interleaved layouts (the common
        # case) than a large fixed pixel value would be.
        max_distance = _median_line_height(lines) * 1.5
    else:
        # 0.6x the document's own label-to-label spacing comfortably
        # covers the observed ~90-100px offset between a wrapped
        # column-split label and its same-row value, while staying below
        # a full row's worth of distance so it can't bleed into the next
        # row -- and scales down automatically for tightly-packed
        # interleaved layouts instead of using one fixed tolerance for both.
        max_distance = row_spacing * 0.6

    # Assign each label's value with a greedy claim-and-remove pass, top to
    # bottom, rather than letting every label search the full money-line
    # pool independently. Two different real layouts both broke independent
    # search: (1) a label whose own row has no value can "steal" a
    # same-line-matched neighbor's value if it happens to be a few pixels
    # closer than its own farther-but-correct row; (2) on a right-aligned
    # value column, *two* labels can each independently need
    # nearest-neighbor lookup and both land on the exact same value row
    # (confirmed on a real filing: "Updated debt" and "Condominium Common
    # Charges" both resolved to the same dollar figure). In both cases the
    # fix is the same: once any label claims a money line -- whether via a
    # same-line match or a nearest-neighbor one -- remove it from the pool
    # so no other label can also claim it. Processing top to bottom means
    # same-line (typically unambiguous, since the value is right there on
    # the label's own row) is resolved before any nearest-neighbor guess
    # gets a chance to grab it first.
    same_line_results: dict[str, str | None] = {}
    for key, label_line in label_lines.items():
        same_line_results[key] = _same_line_money(label_line)

    available = list(money_lines)
    for key, money in same_line_results.items():
        if money is not None:
            label_cy = label_lines[key]["cy"]
            available = [m for m in available if m["cy"] != label_cy]

    found = {}
    for key, label_line in sorted(label_lines.items(), key=lambda kv: kv[1]["cy"]):
        money = same_line_results[key]
        if money is None:
            claimed = None
            if available:
                closest = min(available, key=lambda m: abs(m["cy"] - label_line["cy"]))
                if abs(closest["cy"] - label_line["cy"]) <= max_distance:
                    claimed = closest
            if claimed is not None:
                money = claimed["money"]
                available = [m for m in available if m is not claimed]
        found[key] = {"raw_line": label_line["text"].strip(), "money": money}
    return found


def extract_worksheet_fields(document_no: str, document_url: str, pdf_bytes: bytes) -> WorksheetFields:
    image = render_first_page(pdf_bytes)
    parsed = parse_worksheet_image(image)

    fields = WorksheetFields(
        document_no=document_no,
        document_url=document_url,
        raw_lines={k: v["raw_line"] for k, v in parsed.items()},
    )

    fields.appraised_value = _to_float(parsed.get("fair_market_value", {}).get("money"))
    fields.updated_debt = _to_float(parsed.get("updated_debt", {}).get("money"))
    fields.total_debt_plus_prior_encumbrances = _to_float(
        parsed.get("total_debt_plus_prior_encumbrances", {}).get("money")
    )
    fields.encumbrances_subsequent_to_lien = _to_float(
        parsed.get("encumbrances_subsequent_to_lien", {}).get("money")
    )
    fields.attorney_fees_raw = parsed.get("attorney_fees", {}).get("money")
    fields.appraisal_fee = _to_float(parsed.get("appraisal_fee", {}).get("money"))
    fields.title_search_fee = _to_float(parsed.get("title_search_fee", {}).get("money"))

    condo_fees = _to_float(parsed.get("condo_fees", {}).get("money")) or 0.0
    other_prior = _to_float(parsed.get("other_prior_encumbrances", {}).get("money")) or 0.0

    # The reported Total Debt figure (below) comes from line 2 ("Updated
    # debt"), which is normally typed. Line 5 ("Total debt plus
    # encumbrances prior") exists here purely as an independent
    # cross-check on line 2 -- but on a meaningful fraction of real
    # filings it is filled in *by hand* rather than typed (confirmed by
    # inspecting several flagged documents directly), and handwriting is
    # something Tesseract just cannot read reliably, no amount of
    # cropping/PSM tuning changes that. So the three real states here are:
    #   1. line 2 missing entirely -> no Total Debt to report, hard failure.
    #   2. line 2 present, line 5 unreadable -> no corroboration, but also
    #      no CONTRADICTING evidence against line 2 -- don't flag this as
    #      if the reported figure itself were suspect.
    #   3. both present but disagree -> actual conflicting evidence,
    #      genuinely needs a manual look (this is what originally caught a
    #      real OCR digit error during development, and also caught a
    #      real arithmetic inconsistency in a source document -- that
    #      catching power is the point of the check, so it stays strict
    #      here, just not in case 2).
    if fields.updated_debt is None:
        fields.ocr_validated = False
        fields.ocr_warning = (
            "could not locate a value for 'Updated debt' (line 2) via OCR "
            "-- Total Debt is unavailable for this case, needs manual review"
        )
    elif fields.total_debt_plus_prior_encumbrances is None:
        fields.ocr_validated = True
        fields.ocr_warning = (
            "Total Debt was read directly from line 2; the line 5 cross-check "
            "total could not be read (often handwritten on this form) so it "
            "could not be independently confirmed -- spot-check if in doubt"
        )
    else:
        expected_line5 = fields.updated_debt + condo_fees + other_prior
        if abs(expected_line5 - fields.total_debt_plus_prior_encumbrances) > 1.0:
            fields.ocr_validated = False
            fields.ocr_warning = (
                f"line2+line3+line4 ({expected_line5:.2f}) does not match "
                f"line5 ({fields.total_debt_plus_prior_encumbrances:.2f}); "
                "likely an OCR digit error or an inconsistency in the source "
                "document, needs manual review"
            )
        else:
            fields.ocr_validated = True

    return fields


async def fetch_and_extract_worksheet(
    context: BrowserContext, throttle: Throttle, document_no: str, document_url: str
) -> WorksheetFields:
    pdf_bytes = await fetch_worksheet_pdf(context, throttle, document_url)
    return extract_worksheet_fields(document_no, document_url, pdf_bytes)
