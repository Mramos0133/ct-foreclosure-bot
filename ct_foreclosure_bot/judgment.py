"""Judgment shape, redemption deadline, title status, and last court event.

Five columns requested on 2026-11-13, to be carried on every pull from
then on. All five come from material the pipeline already fetches -- the
Order document it OCRs for the key date, and the docket entries it has
already classified -- so none of this costs an extra network request.

  Judgment Type     strict foreclosure vs foreclosure by sale, which
                    decides whether the deadline being tracked is a Law
                    Day or an auction.
  Judgment Date     when judgment entered, so a case that has sat since
                    is visible as stalled.
  Current Law Day   the controlling redemption deadline on a strict
                    foreclosure.
  Title Vested      whether title has likely already passed, which
                    removes the homeowner as an acquisition prospect.
  Last Material     the most recent docket event that actually moves the
  Court Event       case, with its date.

TITLE VESTED IS AN INFERENCE FROM DOCKET DATES, NOT A TITLE SEARCH.
In a Connecticut strict foreclosure, title vests absolutely in the
plaintiff once the Law Days run without redemption. This module reads
"the Law Day on record has passed" and reports that -- it cannot see a
redemption that happened, a Law Day reset by a motion the docket has not
yet shown, or a title defect. It is deliberately hedged where the record
is ambiguous: a case with material activity recorded AFTER its Law Day is
reported as probably reset rather than vested, because that is exactly
the shape of a case whose judgment was opened. Treat the column as
triage, and confirm on the land records before writing an owner off.
"""

import re
from datetime import date, datetime

from .models import DocketEntry
from .motions import (
    find_target_motions,
    is_assistance_ended,
    is_assistance_started,
    is_bankruptcy_mention,
    is_complaint_entry,
    is_extend_sale_date_or_law_day,
    is_failure_to_appear_default,
    is_granted,
    is_reopen_after_bankruptcy,
)

# The Order's own words, when it states the judgment form outright. Read
# before falling back to the date label, since a judgment can be entered
# by sale and later converted, and the explicit text is the stronger
# signal when both are present.
JUDGMENT_TYPE_PATTERNS = [
    ("Strict Foreclosure", re.compile(r"STRICT\s+FORECLOSURE", re.I)),
    ("Foreclosure by Sale", re.compile(r"FORECLOSURE\s+BY\s+SALE|JUDGMENT\s+OF\s+SALE", re.I)),
]

# A Law Day is only ever set by a strict foreclosure and a Sale Date only
# by a foreclosure by sale, so the date label the Order carried is a
# reliable fallback when the judgment form is not spelled out.
LABEL_TO_TYPE = {"Law Day": "Strict Foreclosure", "Sale Date": "Foreclosure by Sale"}

# Ordered: the first predicate that matches an entry names it. Ordering
# matters only for entries that satisfy more than one, and puts the more
# specific event first -- a "MOTION TO OPEN JUDGMENT" after a bankruptcy
# should read as the reopening, not as the bankruptcy.
MATERIAL_EVENTS: list[tuple[str, object]] = [
    ("Judgment reopened after bankruptcy", is_reopen_after_bankruptcy),
    ("Sale date / Law Day extended", is_extend_sale_date_or_law_day),
    ("Bankruptcy", is_bankruptcy_mention),
    ("Loan assistance ended", is_assistance_ended),
    ("Loan assistance started", is_assistance_started),
    ("Default for failure to appear", is_failure_to_appear_default),
    ("Complaint filed", is_complaint_entry),
]


def _parse(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


# The motion that was moved on, as find_target_motions() labels it. A
# third source because the first two miss most of the book: statewide,
# 1898 cases carry a strict-foreclosure motion while only 11 ever yielded
# a "Law Day" date label, since the Law Day line is often not extracted
# from the Order even when the judgment plainly is a strict foreclosure.
# Without this the column would report Connecticut as overwhelmingly
# foreclosure-by-sale, which is backwards.
MOTION_TO_TYPE = {
    "Motion for Judgment of Strict Foreclosure": "Strict Foreclosure",
    "Motion for Judgment of Foreclosure by Sale": "Foreclosure by Sale",
}


def extract_judgment_type(
    order_text: str | None,
    key_date_label: str | None,
    motion_types: list[str] | None = None,
) -> str | None:
    """"Strict Foreclosure" | "Foreclosure by Sale" | None.

    Three sources, strongest first:

      1. the Order's own wording, when it states the judgment form;
      2. the date label the Order carried -- a Law Day is only ever set
         by a strict foreclosure and a Sale Date only by a sale, so this
         names the judgment that is actually operative;
      3. the motion moved on, which covers the many cases where no date
         was extracted from the Order at all.

    A case carrying BOTH motion types has usually been converted from one
    form to the other, and with no date label there is nothing to say
    which is now operative -- so that returns None rather than picking
    one. An empty cell reads as "not established"; a wrong one reads as
    fact and would send someone to the wrong deadline.
    """
    if order_text:
        for label, pattern in JUDGMENT_TYPE_PATTERNS:
            if pattern.search(order_text):
                return label
    from_label = LABEL_TO_TYPE.get(key_date_label or "")
    if from_label:
        return from_label
    from_motions = {MOTION_TO_TYPE[m] for m in (motion_types or []) if m in MOTION_TO_TYPE}
    return from_motions.pop() if len(from_motions) == 1 else None


def judgment_date_from_entry(order_entry: DocketEntry | None) -> str | None:
    """ISO date judgment entered: the file date of the Order entry itself.

    The Order document's text states the Law Day but not reliably its own
    entry date, so this reads the docket entry that carries the Order
    rather than the PDF.
    """
    if order_entry is None:
        return None
    d = _parse(order_entry.file_date)
    return d.isoformat() if d else None


def current_law_day(key_date: str | None, judgment_type: str | None) -> str | None:
    """The controlling redemption deadline, on strict foreclosures only.

    A Sale Date is not a Law Day and does not belong in this column; the
    sale is already carried by Key Date.
    """
    if judgment_type != "Strict Foreclosure":
        return None
    d = _parse(key_date)
    return d.isoformat() if d else None


def classify_material_event(entry: DocketEntry) -> str | None:
    """A short label for an entry that moves the case, else None."""
    text = entry.description or ""
    for label, predicate in MATERIAL_EVENTS:
        if predicate(text):
            return label
    if find_target_motions(text):
        return "Judgment granted" if is_granted(text) else "Judgment motion filed"
    return None


def last_material_event(entries: list[DocketEntry]) -> tuple[str, str | None]:
    """The most recent material event and its ISO date, ("", None) if none.

    Scans by file date rather than entry number: entry numbers are not
    reliably chronological on these dockets (a ".20" correcting an
    earlier ".10" is filed later but sorts earlier), and the question
    being asked is "what happened most recently".
    """
    best: tuple[date, str] | None = None
    for e in entries:
        label = classify_material_event(e)
        if not label:
            continue
        d = _parse(e.file_date)
        if d is None:
            continue
        if best is None or d > best[0]:
            best = (d, label)
    if best is None:
        return "", None
    return best[1], best[0].isoformat()


def title_vested_status(
    judgment_type: str | None,
    key_date: str | None,
    today: date,
    last_event_date: str | None = None,
) -> str:
    """Whether title has likely passed -- see the module docstring's caveat.

    `last_event_date` guards the one case that would otherwise read as a
    confident false positive: a Law Day in the past with docket activity
    after it is far more likely a judgment that was opened and reset than
    a title that quietly vested, so it is reported as such.
    """
    kd = _parse(key_date)
    if judgment_type is None or kd is None:
        return "Unknown - no judgment date on record"

    after = _parse(last_event_date)
    activity_after = after is not None and after > kd

    if judgment_type == "Strict Foreclosure":
        if kd >= today:
            return f"Redemption period running - Law Day {kd.isoformat()}"
        if activity_after:
            return f"Law Day {kd.isoformat()} passed, but docket activity since - likely reset"
        return f"Likely vested - Law Day {kd.isoformat()} passed without recorded redemption"

    # Foreclosure by sale: title does not pass on the sale date itself.
    # The sale must be held and then confirmed by the court, so a past
    # sale date means the auction has happened, not that title is gone.
    if kd >= today:
        return f"Owner still in title - sale set for {kd.isoformat()}"
    if activity_after:
        return f"Sale date {kd.isoformat()} passed, docket activity since - check confirmation"
    return f"Sale date {kd.isoformat()} passed - title passes on confirmation, verify"
