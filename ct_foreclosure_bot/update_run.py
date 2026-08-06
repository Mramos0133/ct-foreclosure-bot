"""Incremental "Update files" run: find genuinely new leads, and detect
real changes to leads already in the checkpoint, without paying the cost
of a full cold re-scrape every time this is invoked.

Two phases:

  1. Discover new dockets. This is just a normal run_pipeline() pass --
     cheap, because per-docket dedup (processed_dockets) skips every
     docket already seen, so the only real cost is walking each town's
     search-result pages plus processing whatever's genuinely new.
     completed_towns is cleared first so every town actually gets
     re-searched (it exists to make a *single* run resumable, not to
     permanently mark a town as never-worth-checking-again).

  2. Recheck already-matched cases for updates. process_case() redoes
     full Worksheet/Order-document OCR every time it runs, which is fine
     for a case we've never seen but far too expensive to blindly repeat
     for every one of potentially thousands of already-matched cases on
     every single "Update files" invocation. Instead this does a cheap
     check first -- just re-fetch the docket page and compare its entry
     count to what was recorded at last check (CaseResult.docket_entry_
     count) -- and only pays for the full reprocess when that count has
     actually grown, i.e. something genuinely happened on that case.
"""

import dataclasses
import logging
from datetime import date

from playwright.async_api import BrowserContext, Page

from .case_analysis import analyze_docket
from .case_summary import build_case_summary
from .checkpoint import Checkpoint
from .docket import fetch_docket
from .lead_ranking import (
    RankingInfo, decide_bucket, equity_bucket_override, short_sale_ratio,
)
from .models import CaseResult
from .pipeline import process_case, run_pipeline
from .throttle import Throttle

log = logging.getLogger("ct_foreclosure_bot")

# Fields compared to decide whether a recheck counts as a meaningful
# update (-> yellow highlight) rather than just noise. Deliberately
# excludes days_to_key_date (recomputed from today's date every run, so
# it always "changes" even when nothing about the case did) and purely
# structural fields (docket_entry_count itself, URLs that don't change
# once a document exists).
MEANINGFUL_FIELDS = (
    "lead_bucket", "judgment_granted", "non_appearing", "on_auction_site",
    "key_date", "key_date_label", "bankruptcy_chapter", "continuance_count",
    "warm_cold_subflag", "total_debt", "appraised_value",
    "encumbrances_subsequent_to_lien", "case_summary", "owner_name",
    "focus_contact", "new_decision_maker_source_url",
    "default_failure_to_appear", "bankruptcy_stay_reopened",
    "bankruptcy_filed_date", "bankruptcy_reopen_hot",
    "assistance_program_label", "assistance_program_entered_date", "assistance_program_hot",
    "recent_complaint_hot", "complaint_principal_amount", "complaint_unpaid_since",
    "recent_complaint_warm", "assistance_state", "assistance_elapsed_date",
    "probate_case", "estate_of", "heirs", "assistance_elapsed_hot",
)


def _fields_differ(old_value, new_value) -> bool:
    # A legacy quirk: cases saved before the "On Auction Site always
    # Y/N, never N/A" fix earlier this session stored on_auction_site as
    # None for strict-foreclosure cases. None and False mean the same
    # thing there (not on the auction site) and shouldn't read as a real
    # change -- normalize via bool() whenever the current field is a
    # bool, so an old None doesn't false-positive against a new False.
    if isinstance(new_value, bool):
        return bool(old_value) != bool(new_value)
    return old_value != new_value


def _meaningfully_different(old: CaseResult, new: CaseResult) -> bool:
    return any(_fields_differ(getattr(old, f), getattr(new, f)) for f in MEANINGFUL_FIELDS)


def reclassify_from_docket(old_result: CaseResult, docket, analysis, today) -> CaseResult:
    """Re-derive every classification field that needs only the docket
    page plus figures already on record -- no worksheet/order/complaint
    refetch.

    This is what lets a RULE change reach every stored case at zero extra
    request cost. recheck_matched_case() already fetches the docket page
    for its staleness check, so the docket is in hand either way; the
    expensive half of a full reprocess is document OCR, and none of the
    probate / assistance-clock / equity rules need it. Debt and appraised
    value come off the stored record, so the equity override re-evaluates
    correctly without re-reading the worksheet.

    Deliberately does NOT touch key_date, judgment_granted, or the
    financial figures: those come from Order/worksheet OCR, and a case
    whose docket has not grown cannot have changed them.
    """
    ranking = RankingInfo(
        lead_bucket=old_result.lead_bucket,
        judgment_granted=old_result.judgment_granted,
        non_appearing=old_result.non_appearing,
        on_auction_site=old_result.on_auction_site,
        key_date=None,
        key_date_label=old_result.key_date_label,
        days_to_key_date=old_result.days_to_key_date,
        bankruptcy_chapter=old_result.bankruptcy_chapter,
        continuance_count=old_result.continuance_count,
        warm_cold_subflag=old_result.warm_cold_subflag,
        order_entry_for_key_date=None,
        order_entry_for_bankruptcy=None,
        bankruptcy_filed_date=analysis.bankruptcy_filed_date,
        assistance_program_entered_date=analysis.assistance_program_entered_date,
        assistance_program_label=analysis.assistance_program_label,
        assistance_program_failure_present=analysis.assistance_program_failure_entry is not None,
        complaint_filed_date=analysis.complaint_filed_date,
        lender_plaintiff=analysis.lender_plaintiff,
        assistance_state=analysis.assistance_state,
        assistance_elapsed_date=analysis.assistance_elapsed_date,
        probate_case=analysis.probate_case,
    )
    decide_bucket(ranking, analysis.bankruptcy_stay_reopened, today)

    forced = equity_bucket_override(
        short_sale_ratio(
            old_result.total_debt, old_result.appraised_value,
            old_result.encumbrances_subsequent_to_lien,
        )
    )
    if forced:
        ranking.lead_bucket = forced

    case_summary = build_case_summary(
        docket.entries,
        date.fromisoformat(old_result.key_date) if old_result.key_date else None,
        old_result.key_date_label, old_result.bankruptcy_chapter, {},
        bankruptcy_filed_date=analysis.bankruptcy_filed_date,
        reopen_motion_entry=analysis.reopen_motion_entry,
        bankruptcy_reopen_hot=ranking.bankruptcy_reopen_hot,
        assistance_program_entered_date=analysis.assistance_program_entered_date,
        assistance_program_label=analysis.assistance_program_label,
        assistance_program_failure_entry=analysis.assistance_program_failure_entry,
        assistance_program_hot=ranking.assistance_program_hot,
        complaint_filed_date=analysis.complaint_filed_date,
        lender_plaintiff=analysis.lender_plaintiff,
        complaint_principal_amount=old_result.complaint_principal_amount,
        complaint_unpaid_since=old_result.complaint_unpaid_since,
        recent_complaint_hot=ranking.recent_complaint_hot,
        assistance_state=analysis.assistance_state,
        assistance_elapsed_date=analysis.assistance_elapsed_date,
        probate_case=analysis.probate_case,
        probate_signal=analysis.probate_signal,
        estate_of=analysis.estate_of,
        heirs=analysis.heirs,
        today=today,
    )

    return dataclasses.replace(
        old_result,
        lead_bucket=ranking.lead_bucket,
        warm_cold_subflag=ranking.warm_cold_subflag,
        recent_complaint_hot=ranking.recent_complaint_hot,
        recent_complaint_warm=ranking.recent_complaint_warm,
        assistance_program_hot=ranking.assistance_program_hot,
        assistance_program_label=analysis.assistance_program_label,
        # Persist the INPUT alongside the derived flag. Storing only the
        # flag leaves the record internally inconsistent, so anything that
        # later re-derives the bucket from stored fields (offline
        # reclassification) recomputes it from a stale date and silently
        # demotes the case.
        assistance_program_entered_date=(
            analysis.assistance_program_entered_date.isoformat()
            if analysis.assistance_program_entered_date else None
        ),
        assistance_state=analysis.assistance_state,
        assistance_elapsed_date=(
            analysis.assistance_elapsed_date.isoformat()
            if analysis.assistance_elapsed_date else None
        ),
        probate_case=analysis.probate_case,
        probate_signal=analysis.probate_signal,
        estate_of=analysis.estate_of,
        heirs="; ".join(analysis.heirs),
        complaint_filed_date=(
            analysis.complaint_filed_date.isoformat()
            if analysis.complaint_filed_date else None
        ),
        lender_plaintiff=analysis.lender_plaintiff,
        case_summary=case_summary,
        docket_entry_count=len(docket.entries),
    )


async def recheck_matched_case(
    context: BrowserContext, throttle: Throttle, old_result: CaseResult, auction_listing: dict,
) -> tuple[CaseResult, bool]:
    """Cheap-check one already-matched case; only pays for a full
    reprocess (worksheet/order-doc/continuance OCR) when the docket's
    entry count has grown since last check. Returns (result, changed) --
    `result` is `old_result` unchanged if nothing new was found.

    When nothing new was filed, the already-fetched docket still goes
    through reclassify_from_docket() so rule changes reach every stored
    case without spending a single extra request.
    """
    docket = await fetch_docket(context, throttle, old_result.docket_no)
    if len(docket.entries) <= old_result.docket_entry_count:
        analysis = analyze_docket(docket)
        refreshed = reclassify_from_docket(old_result, docket, analysis, date.today())
        return refreshed, _meaningfully_different(old_result, refreshed)

    new_result, _worksheet = await process_case(
        context, throttle, old_result.town, old_result.docket_no, auction_listing,
        street_address=old_result.street_address, zip_code=old_result.zip_code,
    )
    if new_result is None:
        # Implausible (a matched case's target-motion text doesn't get
        # retracted from the public record), but don't lose the old
        # result over it if it somehow happens.
        log.warning("recheck for %s no longer matches a target motion -- keeping prior result", old_result.docket_no)
        return old_result, False

    return new_result, _meaningfully_different(old_result, new_result)


async def run_update(
    page: Page,
    context: BrowserContext,
    throttle: Throttle,
    towns: list[str],
    checkpoint: Checkpoint,
    result_writer,
    review_writer,
    case_status: str = "Active Cases",
    property_type: str = "All Properties",
) -> dict:
    """Run the full "Update files" flow. Returns a stats dict with
    new_dockets / updated_dockets (sets of docket_no) for the caller to
    pass into the highlighted export, plus counts.
    """
    from .auction_site import fetch_statewide_auction_listing

    before_results = {r.docket_no: r for r in checkpoint.all_case_results()}
    log.info("update: %d already-matched cases on record before this run", len(before_results))

    log.info("fetching statewide pending-foreclosure-sale listing (for HOT/auction-site cross-check)...")
    auction_listing = await fetch_statewide_auction_listing(context, throttle)
    log.info("auction listing: %d pending sales found statewide", len(auction_listing))

    # Phase 1: discover new dockets. Re-walk every town's search results
    # (cheap -- per-docket dedup does the real work of skipping already-
    # seen dockets), so clear the town-level bookkeeping first.
    checkpoint.clear_completed_towns()
    await run_pipeline(
        page=page, context=context, throttle=throttle, towns=towns, checkpoint=checkpoint,
        result_writer=result_writer, review_writer=review_writer,
        case_status=case_status, property_type=property_type, auction_listing=auction_listing,
    )

    after_docket_nos = {r.docket_no for r in checkpoint.all_case_results()}
    new_dockets = after_docket_nos - set(before_results.keys())
    log.info("phase 1 done: %d genuinely new matched cases found", len(new_dockets))

    # Phase 2: recheck cases that were already matched before this run for
    # real changes (new continuance, judgment granted, bucket change, etc).
    updated_dockets: set[str] = set()
    checked = 0
    for docket_no, old_result in before_results.items():
        checked += 1
        try:
            new_result, changed = await recheck_matched_case(context, throttle, old_result, auction_listing)
        except Exception:  # noqa: BLE001 -- one bad recheck shouldn't sink the whole update
            log.exception("failed rechecking %s", docket_no)
            continue

        if new_result is not old_result:
            checkpoint.save_case_result(new_result)
        if changed:
            updated_dockets.add(docket_no)
            log.info(
                "UPDATED  %s  %s  bucket %s -> %s",
                old_result.town, docket_no, old_result.lead_bucket, new_result.lead_bucket,
            )
        if checked % 100 == 0:
            log.info("phase 2 progress: %d/%d already-matched cases rechecked", checked, len(before_results))

    log.info(
        "update summary: %d new, %d updated, %d unchanged (of %d already-matched cases rechecked)",
        len(new_dockets), len(updated_dockets), len(before_results) - len(updated_dockets), len(before_results),
    )

    return {
        "new_dockets": new_dockets,
        "updated_dockets": updated_dockets,
        "before_count": len(before_results),
        "after_count": len(after_docket_nos),
    }
