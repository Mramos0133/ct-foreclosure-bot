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
from datetime import datetime

from .models import DocketEntry, DocketInfo
from .motions import (
    find_target_motions,
    is_failure_to_appear_default,
    is_foreclosure_worksheet,
    is_bankruptcy_mention,
    is_reopen_after_bankruptcy,
)


@dataclass
class DocketAnalysis:
    motion_types_found: list[str] = field(default_factory=list)
    matched_motion_entries: list[DocketEntry] = field(default_factory=list)
    default_failure_to_appear: bool = False
    bankruptcy_stay_reopened: bool = False
    bankruptcy_supporting_text: str = ""
    worksheet_entry: DocketEntry | None = None


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
        reopen_entries = [
            e for e in entries[first_bk + 1:] if is_reopen_after_bankruptcy(e.description)
        ]
        if reopen_entries:
            analysis.bankruptcy_stay_reopened = True
            supporting = [entries[first_bk]] + reopen_entries
            analysis.bankruptcy_supporting_text = " | ".join(
                f"[{e.entry_no or e.file_date}] {e.file_date}: {e.description}" for e in supporting
            )

    analysis.worksheet_entry = _pick_worksheet(entries, analysis.matched_motion_entries)
    return analysis
