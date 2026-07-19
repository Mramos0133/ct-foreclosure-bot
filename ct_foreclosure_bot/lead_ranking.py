"""Classifies a matched case into a lead-priority bucket.

Buckets and the reasoning behind each (as specified by the user):

  HOT  -- judgment entered (strict or by sale), non-appearing defendant,
          and (for by-sale cases) not yet publicly listed on the auction
          site. Cleanest, most time-sensitive lead: the legal outcome is
          locked in, there's no attorney contesting it, and it's not yet
          public-facing to every other investor watching the auction site.
  WARM -- a bankruptcy stay was filed and the judgment has since been
          reopened. Real distress signal, but messier: another stay is
          possible, and the owner has shown they're actively trying to
          solve the problem (may already be talking to a bankruptcy
          attorney or loss-mitigation rep). Chapter 7 (liquidation) vs.
          Chapter 13 (repayment plan, may still be trying to keep the
          house) changes the read on this case, so it's flagged.
  COLD -- the sale/law day has been extended multiple times. Deprioritized
          by default (heavily shopped by every other investor watching the
          auction site), except for a sub-segment worth a second look:
          3+ extensions with no bankruptcy filed and no appearance --
          stalling without an apparent real workout in place.

Bucket priority when a case could arguably fit more than one: WARM is
checked first (bankruptcy fundamentally changes a case's trajectory, per
the user's own reasoning above, so it takes precedence over a technically
non-appearing/granted case), then COLD (multiple continuances mean it's
already been shopped, regardless of appearance status), then HOT, else
UNCLASSIFIED for a matched case that doesn't cleanly fit any of the three
(most commonly: the judgment motion hasn't actually been granted yet, or
a case with none of the distinguishing signals above).
"""

from dataclasses import dataclass
from datetime import date

from .models import DocketEntry, DocketInfo
from .case_analysis import DocketAnalysis
from .motions import find_target_motions, is_granted, is_extend_sale_date_or_law_day
from .order_document import find_order_entry


@dataclass
class RankingInfo:
    lead_bucket: str
    judgment_granted: bool
    non_appearing: bool
    on_auction_site: bool
    key_date: date | None
    key_date_label: str | None
    days_to_key_date: int | None
    bankruptcy_chapter: str | None
    continuance_count: int
    warm_cold_subflag: bool
    order_entry_for_key_date: DocketEntry | None  # so the caller knows which doc to OCR
    order_entry_for_bankruptcy: DocketEntry | None


def count_continuances(entries: list[DocketEntry]) -> int:
    return sum(
        1 for e in entries
        if is_extend_sale_date_or_law_day(e.description) and is_granted(e.description)
    )


def _matched_motion_is_granted(entries: list[DocketEntry]) -> bool:
    return any(
        find_target_motions(e.description) and is_granted(e.description)
        for e in entries
    )


def classify_case(
    docket: DocketInfo,
    analysis: DocketAnalysis,
    on_auction_site: bool,
    today: date,
) -> RankingInfo:
    """Determine the lead bucket and supporting metrics for one case.

    Does not itself fetch/OCR any documents -- the caller is expected to
    resolve key_date (from the Order doc) and bankruptcy_chapter (from the
    bankruptcy-related motion doc) separately using the returned order
    entries, then pass them back in via `finalize_key_date` /
    `finalize_bankruptcy_chapter` below. Kept split this way so the
    (network-costly) document fetches only happen when this function has
    already determined they're worth doing.
    """
    judgment_granted = _matched_motion_is_granted(docket.entries)
    non_appearing = analysis.default_failure_to_appear
    continuance_count = count_continuances(docket.entries)

    # Which order entry would tell us the key date, and which bankruptcy-
    # related entry (if any) would tell us the chapter -- resolved here so
    # the caller can decide whether it's worth fetching them.
    order_entry_for_key_date = None
    if analysis.matched_motion_entries:
        anchor = analysis.matched_motion_entries[-1]
        order_entry_for_key_date = find_order_entry(docket.entries, anchor)

    order_entry_for_bankruptcy = None
    if analysis.bankruptcy_stay_reopened:
        # The reopening motion is the last entry in the bankruptcy
        # supporting text pairing; re-derive it the same way
        # case_analysis.py does, and prefer its order (the *new* law day
        # after the stay) over the original judgment's -- confirmed on a
        # real filing that the reopened order carries an updated date.
        for e in docket.entries:
            if "RESET LAW DAYS" in e.description.upper() or "OPEN JUDGMENT" in e.description.upper():
                candidate_order = find_order_entry(docket.entries, e)
                if candidate_order is not None:
                    order_entry_for_key_date = candidate_order
                    order_entry_for_bankruptcy = e
                    break

    ranking = RankingInfo(
        lead_bucket="UNCLASSIFIED",  # placeholder; decide_bucket() sets the real value below
        judgment_granted=judgment_granted,
        non_appearing=non_appearing,
        on_auction_site=on_auction_site,
        key_date=None,
        key_date_label=None,
        days_to_key_date=None,
        bankruptcy_chapter=None,
        continuance_count=continuance_count,
        warm_cold_subflag=False,
        order_entry_for_key_date=order_entry_for_key_date,
        order_entry_for_bankruptcy=order_entry_for_bankruptcy,
    )
    decide_bucket(ranking, analysis.bankruptcy_stay_reopened)
    return ranking


def decide_bucket(ranking: RankingInfo, bankruptcy_stay_reopened: bool) -> None:
    """(Re)derive lead_bucket and warm_cold_subflag from the ranking's
    current field values. Called once with the cheap docket-text-only
    signals, and again after finalize_judgment_granted()/finalize_key_date()
    update those fields with the more authoritative Order-document read --
    a docket entry reading "RESULT: Order 2/17/2026 ..." (as opposed to
    "RESULT: Granted ...") gives no reliable signal by itself; only the
    Order document's own text says unambiguously whether it was granted
    or denied.
    """
    # A key date that has already passed means the law day/sale already
    # happened -- confirmed on a real case where the extracted Sale Date
    # was correct but the docket showed "RETURN OF SALE - WITH PROCEEDS"
    # shortly after it, i.e. the opportunity window is already closed.
    # Only excludes on a *known* past date, not a missing one -- a case
    # where the date simply couldn't be extracted shouldn't be penalized
    # for that.
    key_date_in_past = ranking.days_to_key_date is not None and ranking.days_to_key_date < 0

    if bankruptcy_stay_reopened:
        ranking.lead_bucket = "WARM"
    elif ranking.continuance_count >= 2:
        ranking.lead_bucket = "COLD"
    elif (
        ranking.judgment_granted
        and ranking.non_appearing
        and not ranking.on_auction_site
        and not key_date_in_past
    ):
        ranking.lead_bucket = "HOT"
    else:
        ranking.lead_bucket = "UNCLASSIFIED"

    ranking.warm_cold_subflag = (
        ranking.lead_bucket == "COLD"
        and ranking.continuance_count >= 3
        and not bankruptcy_stay_reopened
        and ranking.non_appearing
    )


def finalize_key_date(ranking: RankingInfo, key_date: date | None, label: str | None, today: date) -> None:
    ranking.key_date = key_date
    ranking.key_date_label = label
    ranking.days_to_key_date = (key_date - today).days if key_date else None


def finalize_judgment_granted(ranking: RankingInfo, granted_from_order: bool) -> None:
    # The Order document's own text is authoritative when available --
    # trust it outright rather than only OR-ing it with the weaker
    # docket-entry-text guess, since that guess is known to under-detect
    # (misses the "RESULT: Order <date> <judge>" phrasing variant that
    # carries no explicit grant/deny word at the docket-entry level).
    ranking.judgment_granted = granted_from_order


def finalize_bankruptcy_chapter(ranking: RankingInfo, chapter: str | None) -> None:
    ranking.bankruptcy_chapter = chapter
