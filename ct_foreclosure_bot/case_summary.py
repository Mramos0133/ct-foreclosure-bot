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

from .lead_ranking import months_between as _months_between
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


RECENT_ACTIONS_LIMIT = 3


def build_case_summary(
    entries: list[DocketEntry],
    key_date: date | None,
    key_date_label: str | None,
    bankruptcy_chapter: str | None,
    continuance_reasons: dict[str, str],
    recent_limit: int = RECENT_ACTIONS_LIMIT,
    bankruptcy_filed_date: date | None = None,
    reopen_motion_entry: DocketEntry | None = None,
    bankruptcy_reopen_hot: bool = False,
    assistance_program_entered_date: date | None = None,
    assistance_program_label: str | None = None,
    assistance_program_failure_entry: DocketEntry | None = None,
    assistance_program_hot: bool = False,
    complaint_filed_date: date | None = None,
    lender_plaintiff: bool = False,
    complaint_principal_amount: float | None = None,
    complaint_unpaid_since: str | None = None,
    recent_complaint_hot: bool = False,
    assistance_state: str = "none",
    assistance_elapsed_date: date | None = None,
    probate_case: bool = False,
    probate_signal: str = "",
    estate_of: str = "",
    heirs: list[str] | None = None,
    today: date | None = None,
) -> str:
    """Return a "\\n"-joined bullet list: current status facts (auction
    date, bankruptcy chapter) first, then just the `recent_limit` most
    recent case actions, newest first -- not the full case history. The
    point of this column is "what's happened lately," so a case open
    since 2022 shouldn't dump years of docket entries into one cell.

    `continuance_reasons` maps entry_no -> reason label for continuance
    entries the caller already OCR'd (see pipeline.py); entries not in
    the dict just get a plain "granted, no reason found in motion text"
    bullet rather than a fabricated one.

    `bankruptcy_filed_date`/`reopen_motion_entry`/`bankruptcy_reopen_hot`
    and their EMAP/loan-mod counterparts (see lead_ranking.py) each get
    their own always-visible status line, same as key_date/
    bankruptcy_chapter below -- the triggering filing can easily be older
    than the `recent_limit` cutoff below by the time this matters (the
    whole point of these HOT rules is a several-month-old filing), so it
    can't rely on the recent-bullets loop to surface it.
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

    # Most recent action first, entries with no parseable date sink to the
    # bottom (least useful, not "recent" by definition), then cut down to
    # just the last `recent_limit`.
    bullets.sort(key=lambda b: b[0] or datetime.min, reverse=True)
    recent = bullets[:recent_limit]

    # Current status facts always shown, regardless of how old the entry
    # that set them was -- these answer "what's the auction date" / "are
    # they in bankruptcy right now," which matter even if that entry
    # itself didn't make the recent-actions cut.
    lines = []
    # Probate first: it changes WHO gets contacted, so it has to be the
    # first thing read before any call or door knock.
    if probate_case:
        who = f" -- deceased owner: {estate_of}" if estate_of else ""
        lines.append(f"- PROBATE/ESTATE CASE{who} ({probate_signal})" if probate_signal
                     else f"- PROBATE/ESTATE CASE{who}")
        if heirs:
            lines.append(f"- Contact: {'; '.join(heirs)}")
        else:
            lines.append("- Heirs not named in the caption -- check the complaint/probate filing for who holds authority to sell")
    if assistance_state == "open":
        lines.append("- Loan assistance IN PROGRESS (mediation/EMAP still open) -- WARM: owner is working a rescue, not ready to sell")
    elif assistance_state == "elapsed":
        when = f" {assistance_elapsed_date.strftime('%m/%d/%Y')}" if assistance_elapsed_date else ""
        lines.append(f"- Loan assistance ELAPSED{when} (mediation/EMAP closed) -- options exhausted")
    if recent_complaint_hot and complaint_filed_date:
        months_ago = _months_between(complaint_filed_date, today or date.today())
        when = f"{months_ago} months ago" if months_ago != 1 else "1 month ago"
        lines.append(
            f"- Complaint filed by lender {complaint_filed_date.strftime('%m/%d/%Y')} ({when})"
            " -- HOT: recent foreclosure complaint"
        )
        detail_bits = []
        if complaint_principal_amount is not None:
            detail_bits.append(f"principal owed ${complaint_principal_amount:,.2f}")
        if complaint_unpaid_since:
            detail_bits.append(f"P&I unpaid since {complaint_unpaid_since}")
        if detail_bits:
            lines.append(f"- Per complaint: {'; '.join(detail_bits)}")
    if key_date and key_date_label:
        lines.append(f"- {key_date_label} currently scheduled for {key_date.strftime('%m/%d/%Y')}")
    if bankruptcy_chapter:
        lines.append(f"- Bankruptcy Chapter {bankruptcy_chapter}")
    if bankruptcy_filed_date:
        months_ago = _months_between(bankruptcy_filed_date, today or date.today())
        lines.append(f"- Bankruptcy filed {bankruptcy_filed_date.strftime('%m/%d/%Y')} ({months_ago} months ago)")
    if reopen_motion_entry:
        hot_note = " -- HOT: restarts foreclosure/auction process" if bankruptcy_reopen_hot else ""
        lines.append(
            f"- {reopen_motion_entry.file_date.strip()}: {reopen_motion_entry.description.strip()}{hot_note}"
        )
    if assistance_program_entered_date and assistance_program_label:
        months_ago = _months_between(assistance_program_entered_date, today or date.today())
        lines.append(
            f"- Entered {assistance_program_label} {assistance_program_entered_date.strftime('%m/%d/%Y')} ({months_ago} months ago)"
        )
    if assistance_program_failure_entry:
        hot_note = (
            " -- HOT: program failed/expired, foreclosure continuing" if assistance_program_hot else ""
        )
        lines.append(
            f"- {assistance_program_failure_entry.file_date.strip()}: "
            f"{assistance_program_failure_entry.description.strip()}{hot_note}"
        )
    for _, display_date, text in recent:
        lines.append(f"- {display_date}: {text}")
    return "\n".join(lines)
