"""Builds a short, dated bullet-point summary of a case's recent activity.

Goal: let the caller get oriented on a case -- what's happened, when, and
(for a continuance) why -- without reading the full docket before calling
the homeowner. Most of this is free: the event type, date, and grant/deny
outcome all come from docket entry text already fetched and classified
elsewhere in the pipeline (motions.py / case_analysis.py / lead_ranking.py),
no extra network cost.

The one exception is *why* a sale-date/Law Day extension was granted --
that reasoning only exists in the motion's own text (e.g. "Defendant is
currently completing a loan modification review with the servicer"), not
in the one-line docket entry description, so it requires OCR'ing the
motion document. That extraction is keyword-based, not a fixed-template
form like the Foreclosure Worksheet -- treat a matched reason as a
best-effort hint worth a glance at the source document, not a guaranteed
account of what happened.
"""

import re
from datetime import date, datetime

from .models import DocketEntry
from .motions import (
    find_target_motions,
    is_bankruptcy_mention,
    is_extend_sale_date_or_law_day,
    is_failure_to_appear_default,
    is_granted,
)

DENIED_PATTERN = re.compile(r"\bDENIED\b", re.I)

# Keyword -> short human-readable reason label. Matched in order, first
# hit wins. Deliberately short/generic labels (not raw quotes from the
# motion) so the summary column stays scannable -- the source document
# link elsewhere on the row is there for the full text.
REASON_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("loan modification review", re.compile(r"loan\s+modif", re.I)),
    ("forbearance", re.compile(r"forbearance", re.I)),
    ("short sale in progress", re.compile(r"short\s+sale", re.I)),
    ("refinance in progress", re.compile(r"refinanc", re.I)),
    ("mediation", re.compile(r"mediation", re.I)),
    ("COVID-19 hardship", re.compile(r"covid", re.I)),
    ("financial hardship", re.compile(r"hardship", re.I)),
]


def extract_continuance_reason(motion_text: str) -> str | None:
    for label, pattern in REASON_KEYWORDS:
        if pattern.search(motion_text):
            return label
    return None


def _parse_entry_date(file_date: str) -> datetime | None:
    try:
        return datetime.strptime(file_date.strip(), "%m/%d/%Y")
    except ValueError:
        return None


def build_case_summary(
    entries: list[DocketEntry],
    key_date: date | None,
    key_date_label: str | None,
    bankruptcy_chapter: str | None,
    continuance_reasons: dict[str, str],
) -> str:
    """Return a "\\n"-joined, chronologically-sorted list of "- M/D/YYYY: ..." bullets.

    `continuance_reasons` maps entry_no -> reason label for continuance
    entries the caller already OCR'd (see pipeline.py); entries not in
    the dict just get a plain "granted, no reason found in motion text"
    bullet rather than a fabricated one.
    """
    bullets: list[tuple[datetime | None, str, str]] = []  # (sort_key, display_date, text)

    for e in entries:
        parsed = _parse_entry_date(e.file_date)
        display_date = e.file_date.strip()

        # A single entry can be both a target motion (e.g. "Motion to Open
        # Judgment") *and* an extend-sale-date/Law Day motion -- confirmed
        # on real dockets, where the common combined filing is literally
        # titled "MOTION TO OPEN JUDGMENT AND EXTEND THE SALE DATE". Build
        # one bullet per entry covering whichever of those apply, rather
        # than one bullet per matched pattern, so that single filing
        # doesn't show up twice with the same date.
        motions = find_target_motions(e.description)
        is_continuance = is_extend_sale_date_or_law_day(e.description)
        if motions or is_continuance:
            label = "; ".join(motions) if motions else "Motion to extend sale date/Law Day"
            if is_continuance and motions:
                label += " (extend sale date/Law Day)"
            if is_granted(e.description):
                text = f"{label} -- GRANTED"
                if is_continuance:
                    reason = continuance_reasons.get(e.entry_no)
                    text += f" (reason: {reason})" if reason else " (reason not found in motion text)"
                bullets.append((parsed, display_date, text))
            elif DENIED_PATTERN.search(e.description):
                bullets.append((parsed, display_date, f"{label} -- denied"))

        if is_bankruptcy_mention(e.description):
            bullets.append((parsed, display_date, "Bankruptcy filed / automatic stay in effect"))

        if is_failure_to_appear_default(e.description):
            bullets.append((parsed, display_date, "Defendant defaulted for failure to appear"))

    if key_date and key_date_label:
        bullets.append((
            datetime.combine(key_date, datetime.min.time()),
            key_date.strftime("%m/%d/%Y"),
            f"{key_date_label} currently scheduled for {key_date.strftime('%m/%d/%Y')}",
        ))

    if bankruptcy_chapter:
        bullets.append((None, "", f"Bankruptcy Chapter {bankruptcy_chapter}"))

    bullets.sort(key=lambda b: (b[0] is None, b[0] or datetime.min))

    lines = []
    for _, display_date, text in bullets:
        lines.append(f"- {display_date}: {text}" if display_date else f"- {text}")
    return "\n".join(lines)
