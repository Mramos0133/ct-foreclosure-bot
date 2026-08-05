"""Turns a parsed DocketInfo into case-level flags and motion matches.

Worksheet selection: a case can have more than one Foreclosure Worksheet
filing (e.g. one before a bankruptcy stay, another after the case was
reset post-bankruptcy -- observed directly in HHD-CV-18-6098713-S). The
worksheet actually relied on for a given judgment motion is the one filed
closest to (and normally shortly after) that motion, so this picks the
worksheet entry with the earliest file date on/after the matched motion's
file date; if none exists after the motion, it falls back to the most
recently filed worksheet in the docket.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

import re

from .models import DocketEntry, DocketInfo
from .motions import (
    find_target_motions,
    is_failure_to_appear_default,
    is_foreclosure_worksheet,
    is_bankruptcy_mention,
    is_reopen_after_bankruptcy,
    is_return_of_service,
    is_appearance_entry,
    is_substitute_party_motion,
    is_assistance_program_entry,
    is_program_failure_motion,
    program_entry_label,
    is_complaint_entry,
    is_lender_plaintiff,
    is_assistance_started,
    is_assistance_ended,
)
from .probate import (
    is_probate_case,
    probate_signal,
    extract_estate_of,
    extract_heirs,
)


@dataclass
class DocketAnalysis:
    motion_types_found: list[str] = field(default_factory=list)
    matched_motion_entries: list[DocketEntry] = field(default_factory=list)
    default_failure_to_appear: bool = False
    bankruptcy_stay_reopened: bool = False
    bankruptcy_supporting_text: str = ""
    bankruptcy_filed_date: date | None = None
    reopen_motion_entry: DocketEntry | None = None  # first motion filed *after* the bankruptcy that re-opens judgment/resets Law Days -- see motions.REOPEN_AFTER_BANKRUPTCY_PATTERNS
    assistance_program_entered_date: date | None = None
    assistance_program_label: str | None = None  # "EMAP" | "Loan Modification"
    assistance_program_failure_entry: DocketEntry | None = None  # most recent motion filed *after* the program entry signaling it failed/expired -- see motions.is_program_failure_motion
    assistance_program_supporting_text: str = ""
    # Loan-assistance clock, driving the complaint-stage HOT/WARM split
    # (see lead_ranking.py). "elapsed" only when every avenue found has
    # closed; one still-open avenue leaves the whole case "open".
    assistance_state: str = "none"  # "none" | "open" | "elapsed"
    assistance_started_entries: list[DocketEntry] = field(default_factory=list)
    assistance_ended_entries: list[DocketEntry] = field(default_factory=list)
    assistance_elapsed_date: date | None = None  # date the last open avenue closed
    # Probate / deceased-owner detection (see probate.py)
    probate_case: bool = False
    probate_signal: str = ""
    estate_of: str = ""
    heirs: list[str] = field(default_factory=list)
    complaint_entry: DocketEntry | None = None  # earliest COMPLAINT docket entry -- see motions.is_complaint_entry
    complaint_filed_date: date | None = None  # date of that entry; falls back to the earliest docket entry's date (case start) when no COMPLAINT entry is labeled
    lender_plaintiff: bool = False  # plaintiff caption reads like a bank/lender -- see motions.is_lender_plaintiff
    worksheet_entry: DocketEntry | None = None
    owner_name: str = ""
    return_of_service_doc_url: str | None = None
    appearance_doc_urls: list[str] = field(default_factory=list)
    substitute_party_doc_url: str | None = None


def _parse_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y")
    except ValueError:
        return None


def _pick_worksheet(entries: list[DocketEntry], matched_entries: list[DocketEntry]) -> DocketEntry | None:
    worksheets = [e for e in entries if is_foreclosure_worksheet(e.description) and e.document_url]
    if not worksheets:
        return None
    if not matched_entries:
        # no anchor motion date to compare against -- use the latest worksheet filed
        return max(worksheets, key=lambda e: _parse_date(e.file_date) or datetime.min)

    motion_date = max((_parse_date(e.file_date) for e in matched_entries if _parse_date(e.file_date)), default=None)
    if motion_date is None:
        return max(worksheets, key=lambda e: _parse_date(e.file_date) or datetime.min)

    on_or_after = [
        e for e in worksheets
        if (_parse_date(e.file_date) or datetime.min) >= motion_date
    ]
    if on_or_after:
        return min(on_or_after, key=lambda e: _parse_date(e.file_date) or datetime.max)
    return max(worksheets, key=lambda e: _parse_date(e.file_date) or datetime.min)


_CAPTION_SPLIT = re.compile(r"\bvs?\.\s+", re.I)
_TRAILING_ET_AL = re.compile(r"\s*,?\s*ET\s*AL\.?\s*$", re.I)


def parse_defendant_name(case_caption: str) -> str:
    """Best-effort defendant/owner name from the case caption's "X v. Y"
    text -- this is parsed from the court's own case-detail HTML, not OCR'd
    from a document, so it's a reliable (if occasionally court-truncated)
    source. Returns "" if the caption doesn't split cleanly.
    """
    parts = _CAPTION_SPLIT.split(case_caption, maxsplit=1)
    if len(parts) < 2:
        return ""
    defendant = parts[1].strip()
    defendant = _TRAILING_ET_AL.sub("", defendant).strip()
    return defendant


def analyze_docket(docket: DocketInfo) -> DocketAnalysis:
    analysis = DocketAnalysis()
    entries = docket.entries

    motion_labels: set[str] = set()
    for entry in entries:
        labels = find_target_motions(entry.description)
        if labels:
            motion_labels.update(labels)
            analysis.matched_motion_entries.append(entry)
        if is_failure_to_appear_default(entry.description):
            analysis.default_failure_to_appear = True

    analysis.motion_types_found = sorted(motion_labels)

    bankruptcy_idx = [i for i, e in enumerate(entries) if is_bankruptcy_mention(e.description)]
    if bankruptcy_idx:
        first_bk = bankruptcy_idx[0]
        parsed_bk_date = _parse_date(entries[first_bk].file_date)
        analysis.bankruptcy_filed_date = parsed_bk_date.date() if parsed_bk_date else None
        reopen_entries = [
            e for e in entries[first_bk + 1:] if is_reopen_after_bankruptcy(e.description)
        ]
        if reopen_entries:
            analysis.bankruptcy_stay_reopened = True
            analysis.reopen_motion_entry = reopen_entries[0]  # earliest motion filed after the bankruptcy that reopens/restarts the case
            supporting = [entries[first_bk]] + reopen_entries
            analysis.bankruptcy_supporting_text = " | ".join(
                f"[{e.entry_no or e.file_date}] {e.file_date}: {e.description}" for e in supporting
            )

    program_idx = [i for i, e in enumerate(entries) if is_assistance_program_entry(e.description)]
    if program_idx:
        first_prog = program_idx[0]
        parsed_prog_date = _parse_date(entries[first_prog].file_date)
        analysis.assistance_program_entered_date = parsed_prog_date.date() if parsed_prog_date else None
        analysis.assistance_program_label = program_entry_label(entries[first_prog].description)
        failure_entries = [
            e for e in entries[first_prog + 1:] if is_program_failure_motion(e.description)
        ]
        if failure_entries:
            # "Most recent" per explicit request (as opposed to the
            # bankruptcy-reopen rule above, which anchors on the
            # *earliest* reopening motion) -- latest by parsed file date,
            # falling back to last-in-docket-order when a date can't be
            # parsed.
            analysis.assistance_program_failure_entry = max(
                failure_entries, key=lambda e: _parse_date(e.file_date) or datetime.min
            )
            supporting = [entries[first_prog], analysis.assistance_program_failure_entry]
            analysis.assistance_program_supporting_text = " | ".join(
                f"[{e.entry_no or e.file_date}] {e.file_date}: {e.description}" for e in supporting
            )

    # Loan-assistance clock. Counts mediation and EMAP alike ("either
    # signal, whichever appears"), and only reports "elapsed" once the
    # last avenue has closed -- an avenue opened *after* the most recent
    # closure means the owner is back inside a live assistance window.
    analysis.assistance_started_entries = [e for e in entries if is_assistance_started(e.description)]
    analysis.assistance_ended_entries = [e for e in entries if is_assistance_ended(e.description)]
    if analysis.assistance_ended_entries or analysis.assistance_started_entries:
        last_start = max(
            (_parse_date(e.file_date) for e in analysis.assistance_started_entries if _parse_date(e.file_date)),
            default=None,
        )
        last_end = max(
            (_parse_date(e.file_date) for e in analysis.assistance_ended_entries if _parse_date(e.file_date)),
            default=None,
        )
        if last_end is not None and (last_start is None or last_end >= last_start):
            analysis.assistance_state = "elapsed"
            analysis.assistance_elapsed_date = last_end.date()
        else:
            analysis.assistance_state = "open"

    analysis.probate_case = is_probate_case(docket.case_caption, entries)
    if analysis.probate_case:
        analysis.probate_signal = probate_signal(docket.case_caption, entries)
        analysis.estate_of = extract_estate_of(docket.case_caption)
        analysis.heirs = extract_heirs(docket.case_caption, entries)

    analysis.lender_plaintiff = is_lender_plaintiff(docket.case_caption)
    complaint_entries = [e for e in entries if is_complaint_entry(e.description)]
    if complaint_entries:
        analysis.complaint_entry = min(
            complaint_entries, key=lambda e: _parse_date(e.file_date) or datetime.max
        )
        parsed_complaint = _parse_date(analysis.complaint_entry.file_date)
        analysis.complaint_filed_date = parsed_complaint.date() if parsed_complaint else None
    elif entries:
        # No labeled COMPLAINT entry -- use the earliest docket entry's
        # date as the case-start proxy (still what "when was this filed"
        # means for recency), but leave complaint_entry None so nothing
        # tries to OCR a document that isn't actually the complaint.
        earliest = min((_parse_date(e.file_date) for e in entries if _parse_date(e.file_date)), default=None)
        analysis.complaint_filed_date = earliest.date() if earliest else None

    analysis.worksheet_entry = _pick_worksheet(entries, analysis.matched_motion_entries)

    analysis.owner_name = parse_defendant_name(docket.case_caption)

    return_of_service_entries = [
        e for e in entries if is_return_of_service(e.description) and e.document_url
    ]
    if return_of_service_entries:
        analysis.return_of_service_doc_url = max(
            return_of_service_entries, key=lambda e: _parse_date(e.file_date) or datetime.min
        ).document_url

    appearance_entries = [
        e for e in entries if is_appearance_entry(e.description) and e.document_url
    ]
    analysis.appearance_doc_urls = [
        e.document_url
        for e in sorted(appearance_entries, key=lambda e: _parse_date(e.file_date) or datetime.min)
    ]

    substitute_party_entries = [
        e for e in entries if is_substitute_party_motion(e.description) and e.document_url
    ]
    if substitute_party_entries:
        analysis.substitute_party_doc_url = max(
            substitute_party_entries, key=lambda e: _parse_date(e.file_date) or datetime.min
        ).document_url

    return analysis
