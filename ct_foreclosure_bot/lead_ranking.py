"""Classifies a matched case into a lead-priority bucket.

Buckets and the reasoning behind each (as specified by the user):

  HOT  -- judgment entered (strict or by sale), non-appearing defendant,
          and (for by-sale cases) not yet publicly listed on the auction
          site. Cleanest, most time-sensitive lead: the legal outcome is
          locked in, there's no attorney contesting it, and it's not yet
          public-facing to every other investor watching the auction site.

          ALSO HOT (per explicit request): bankruptcy was filed, and
          *after* that a motion to open judgment / reset Law Days / any
          other motion that restarts the foreclosure or auction process
          was filed -- but only when the bankruptcy filing itself is
          "fresh enough to matter, but not so fresh the case hasn't had
          time to actually restart": 2-12 months before today. Outside
          that window (bankruptcy filed <2 months ago, i.e. still likely
          mid-stay, or >12 months ago, i.e. stale) it falls through to
          WARM instead, same as before this rule existed.

          ALSO HOT (per explicit request, independent of the bankruptcy
          rule above): the owner entered an EMAP or loan-modification
          program, and *after* that the docket shows the most recent
          motion of that type is a denial / motion to open judgment /
          motion for judgment of foreclosure / or any other motion
          signaling the program failed or lapsed and the foreclosure or
          auction is moving again -- same idea as the bankruptcy rule,
          just a different kind of stay. Only counts when the program was
          entered 1-12 months before today. NOTE: the EMAP/loan-mod entry
          detection is a best-effort pattern match (unlike the bankruptcy
          patterns, which were confirmed against real dockets) -- see
          motions.py.

          ALSO HOT (per explicit request): a bank/lender filed the
          foreclosure complaint recently -- within the last
          RECENT_COMPLAINT_HOT_MAX_MONTHS (6) months. This is the
          earliest-stage lead type: no judgment motion needs to exist
          yet (it's the only rule that can match a case with no target
          motion at all -- see pipeline.process_case), the owner has
          just been served, and no other investor watching judgments or
          the auction site has seen it. The complaint document is OCR'd
          for the principal balance owed and the date since which P&I
          has gone unpaid (see complaint_document.py); both go on the
          spreadsheet row.
  WARM -- a bankruptcy stay was filed and the judgment has since been
          reopened, but the bankruptcy filing date is outside the HOT
          2-12-month window above (or couldn't be parsed). Real distress
          signal, but messier: another stay is possible, and the owner
          has shown they're actively trying to solve the problem (may
          already be talking to a bankruptcy attorney or loss-mitigation
          rep). Chapter 7 (liquidation) vs. Chapter 13 (repayment plan,
          may still be trying to keep the house) changes the read on this
          case, so it's flagged.
  COLD -- the sale/law day has been extended multiple times. Deprioritized
          by default (heavily shopped by every other investor watching the
          auction site), except for a sub-segment worth a second look:
          3+ extensions with no bankruptcy filed and no appearance --
          stalling without an apparent real workout in place.

Bucket priority when a case could arguably fit more than one: the two
"stay-then-failed" HOT rules above (bankruptcy-then-reopened and
EMAP/loan-mod-then-failed, each within their own month window) are
checked first and are independent of each other -- either one alone is
enough, and both can be true at once. Then WARM for bankruptcy-then-
reopened cases outside the HOT window, then COLD (multiple continuances
mean it's already been shopped, regardless of appearance status), then
the original judgment/non-appearing HOT rule, else UNCLASSIFIED for a
matched case that doesn't cleanly fit any of the three (most commonly:
the judgment motion hasn't actually been granted yet, or a case with none
of the distinguishing signals above).
"""

from dataclasses import dataclass
from datetime import date

from .models import DocketEntry, DocketInfo
from .case_analysis import DocketAnalysis
from .motions import find_target_motions, is_granted, is_extend_sale_date_or_law_day
from .order_document import find_order_entry

# Bankruptcy-then-reopened cases only count toward HOT when the bankruptcy
# was filed within this many months of today -- see module docstring.
BANKRUPTCY_REOPEN_HOT_MIN_MONTHS = 2
BANKRUPTCY_REOPEN_HOT_MAX_MONTHS = 12

# EMAP/loan-mod-then-failed cases only count toward HOT when the program
# was entered within this many months of today -- see module docstring.
ASSISTANCE_PROGRAM_HOT_MIN_MONTHS = 1
ASSISTANCE_PROGRAM_HOT_MAX_MONTHS = 12

# A bank/lender-filed complaint counts as "recent" (and therefore HOT --
# see module docstring) when filed within this many months of today. Six
# months keeps the definition of "recent" generous enough to cover the
# pre-judgment window of a typical CT foreclosure (service, appearance,
# mediation referral all happen inside it) while excluding cases old
# enough that the complaint itself is no longer news to the owner.
RECENT_COMPLAINT_HOT_MAX_MONTHS = 6


def months_between(earlier: date, later: date) -> int:
    """Whole calendar months elapsed from `earlier` to `later` (>= 0 when
    `later` is on/after `earlier`). Day-of-month aware: a filing on the
    27th doesn't count as "1 month ago" until the 27th of the next month,
    not just the 1st.
    """
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)


def is_bankruptcy_reopen_hot(bankruptcy_filed_date: date | None, today: date) -> bool:
    if bankruptcy_filed_date is None or bankruptcy_filed_date > today:
        return False
    months = months_between(bankruptcy_filed_date, today)
    return BANKRUPTCY_REOPEN_HOT_MIN_MONTHS <= months <= BANKRUPTCY_REOPEN_HOT_MAX_MONTHS


def is_assistance_program_hot(entered_date: date | None, today: date) -> bool:
    if entered_date is None or entered_date > today:
        return False
    months = months_between(entered_date, today)
    return ASSISTANCE_PROGRAM_HOT_MIN_MONTHS <= months <= ASSISTANCE_PROGRAM_HOT_MAX_MONTHS


def is_recent_complaint_hot(complaint_filed_date: date | None, lender_plaintiff: bool, today: date) -> bool:
    if not lender_plaintiff or complaint_filed_date is None or complaint_filed_date > today:
        return False
    return months_between(complaint_filed_date, today) <= RECENT_COMPLAINT_HOT_MAX_MONTHS


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
    bankruptcy_filed_date: date | None = None
    bankruptcy_reopen_hot: bool = False  # bankruptcy-then-reopened AND within the 2-12 month HOT window
    assistance_program_entered_date: date | None = None
    assistance_program_label: str | None = None  # "EMAP" | "Loan Modification"
    assistance_program_failure_present: bool = False  # a qualifying failure motion was found after the program entry (timing not yet checked)
    assistance_program_hot: bool = False  # program-then-failed AND within the 1-12 month HOT window
    complaint_filed_date: date | None = None
    lender_plaintiff: bool = False
    recent_complaint_hot: bool = False  # lender-filed complaint within the 6-month HOT window


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
        bankruptcy_filed_date=analysis.bankruptcy_filed_date,
        assistance_program_entered_date=analysis.assistance_program_entered_date,
        assistance_program_label=analysis.assistance_program_label,
        assistance_program_failure_present=analysis.assistance_program_failure_entry is not None,
        complaint_filed_date=analysis.complaint_filed_date,
        lender_plaintiff=analysis.lender_plaintiff,
    )
    decide_bucket(ranking, analysis.bankruptcy_stay_reopened, today)
    return ranking


def decide_bucket(ranking: RankingInfo, bankruptcy_stay_reopened: bool, today: date) -> None:
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

    ranking.bankruptcy_reopen_hot = bankruptcy_stay_reopened and is_bankruptcy_reopen_hot(
        ranking.bankruptcy_filed_date, today
    )
    ranking.assistance_program_hot = ranking.assistance_program_failure_present and is_assistance_program_hot(
        ranking.assistance_program_entered_date, today
    )
    ranking.recent_complaint_hot = is_recent_complaint_hot(
        ranking.complaint_filed_date, ranking.lender_plaintiff, today
    )

    if ranking.bankruptcy_reopen_hot or ranking.assistance_program_hot or ranking.recent_complaint_hot:
        ranking.lead_bucket = "HOT"
    elif bankruptcy_stay_reopened:
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
