"""Orchestrates the full run: town iteration -> search -> docket -> worksheet.

Every docket is wrapped in its own try/except so one bad/malformed case
(a broken PDF, an OCR crash, an unexpected page shape) is logged to the
review CSV and skipped, rather than aborting the whole run. Every docket,
matched or not, is recorded in the checkpoint immediately after handling
it, so a resumed run never re-spends a request on it. Towns are only
marked complete once every result page has been walked without hitting
--limit, so an interrupted or limited run resumes mid-town correctly.
"""

import logging
import sys
from datetime import date, datetime, timezone

from playwright.async_api import Page, BrowserContext

from .auction_site import normalize_docket
from .case_analysis import analyze_docket
from .case_summary import build_case_summary, extract_continuance_reason
from .checkpoint import Checkpoint
from .docket import fetch_docket
from .lead_ranking import (
    classify_case, decide_bucket, finalize_key_date,
    finalize_judgment_granted, finalize_bankruptcy_chapter,
)
from .models import CaseResult
from .motions import is_extend_sale_date_or_law_day, is_granted
from .order_document import (
    fetch_document_text, extract_key_date, extract_bankruptcy_chapter, is_order_granted,
)
from .output import ResultWriter, ReviewWriter
from .search import iter_town_cases
from .throttle import Throttle
from .worksheet import fetch_and_extract_worksheet

log = logging.getLogger("ct_foreclosure_bot")


def _fmt_motion_type(labels: list[str]) -> str:
    return "; ".join(labels)


async def process_case(
    context: BrowserContext,
    throttle: Throttle,
    town: str,
    docket_no: str,
    auction_listing: dict[str, date],
    street_address: str = "",
    zip_code: str = "",
) -> tuple[CaseResult | None, object | None]:
    """Fetch + analyze one docket. Returns (CaseResult or None, WorksheetFields or None).

    Returns (None, None) for a case that doesn't match either target motion
    -- that is not an error, just a non-match, and the caller still
    checkpoints it so it is not re-fetched on resume.

    `auction_listing` is the statewide pending-sale listing fetched once
    per run (see auction_site.py) -- passed in rather than fetched here so
    it's only ever pulled once, not once per case.

    `street_address`/`zip_code` come straight from the property search
    results row (CaseListing) -- no extra fetch needed, they're already
    part of the initial search grid.
    """
    docket = await fetch_docket(context, throttle, docket_no)
    analysis = analyze_docket(docket)

    if not analysis.motion_types_found:
        return None, None

    worksheet = None
    encumbrances_display = ""
    encumbrances_numeric = None
    total_debt = None
    appraised_value = None
    attorney_fees_display = None
    worksheet_url = analysis.worksheet_entry.document_url if analysis.worksheet_entry else None

    if analysis.worksheet_entry is not None:
        worksheet = await fetch_and_extract_worksheet(
            context, throttle, analysis.worksheet_entry.entry_no, analysis.worksheet_entry.document_url
        )
        total_debt = worksheet.updated_debt
        appraised_value = worksheet.appraised_value
        attorney_fees_display = worksheet.attorney_fees_raw
        encumbrances_numeric = worksheet.encumbrances_subsequent_to_lien
        if worksheet.encumbrances_subsequent_to_lien is not None:
            # The worksheet only ever gives a lump-sum total for this line --
            # no per-holder itemization is present on the source document.
            encumbrances_display = (
                f"${worksheet.encumbrances_subsequent_to_lien:,.2f} total "
                "(source document does not itemize by holder)"
            )
    else:
        encumbrances_display = "no Foreclosure Worksheet found in docket"

    # Always a definite Yes/No, never N/A: a strict foreclosure case can
    # never appear on the pending-sale listing (no auction happens for
    # strict foreclosure -- the property transfers directly, no sale
    # event), but for the purpose this column serves -- is the case
    # currently visible to other investors watching the public auction
    # list -- "No" is the correct, meaningful answer for those cases too,
    # not an inapplicable one.
    normalized = normalize_docket(docket.docket_no)
    on_auction_site = normalized in auction_listing

    today = date.today()
    ranking = classify_case(docket, analysis, on_auction_site, today)

    # Only fetch/OCR the Order document (and, for WARM cases, the
    # bankruptcy motion) when there's actually a date/chapter to find --
    # these are additional network requests per case, so skip them when
    # classify_case() already determined there's no relevant order entry.
    if ranking.order_entry_for_key_date is not None:
        try:
            order_text = await fetch_document_text(
                context, throttle, ranking.order_entry_for_key_date.document_url
            )
            key_date, label = extract_key_date(order_text)
            finalize_key_date(ranking, key_date, label, today)
            # The Order document's own text is the authoritative source for
            # whether the motion was actually granted -- the docket entry's
            # own RESULT text is not reliable for this (confirmed on a real
            # filing: "RESULT: Order 2/17/2026 HON ..." carries no
            # grant/deny word at all, unlike the more common "RESULT:
            # Granted ..." phrasing), so this overrides classify_case()'s
            # preliminary guess and the bucket is re-decided below.
            finalize_judgment_granted(ranking, is_order_granted(order_text))
            if ranking.order_entry_for_bankruptcy is None and analysis.bankruptcy_stay_reopened:
                chapter = extract_bankruptcy_chapter(order_text)
                if chapter:
                    finalize_bankruptcy_chapter(ranking, chapter)
        except Exception:  # noqa: BLE001 -- a failed order-doc fetch shouldn't sink the whole case
            log.exception("failed fetching order document for %s", docket_no)

    if ranking.bankruptcy_chapter is None and ranking.order_entry_for_bankruptcy is not None:
        try:
            bk_text = await fetch_document_text(
                context, throttle, ranking.order_entry_for_bankruptcy.document_url
            )
            finalize_bankruptcy_chapter(ranking, extract_bankruptcy_chapter(bk_text))
        except Exception:  # noqa: BLE001
            log.exception("failed fetching bankruptcy document for %s", docket_no)

    decide_bucket(ranking, analysis.bankruptcy_stay_reopened)

    # Short-sale override: total debt plus subsequent-lien encumbrances
    # exceeding 85% of appraised value means there's little/no equity
    # cushion left for a standard purchase -- moved to its own tab instead
    # of wherever it would have otherwise landed (HOT/WARM/COLD/
    # UNCLASSIFIED), per explicit request. Only evaluated when both figures
    # are actually known; a missing appraised value (or one of zero) can't
    # support a ratio.
    if total_debt is not None and appraised_value is not None and appraised_value > 0:
        short_sale_ratio = (total_debt + (encumbrances_numeric or 0)) / appraised_value
        if short_sale_ratio > 0.85:
            ranking.lead_bucket = "POTENTIAL_SHORT_SALE"

    # Only OCR a continuance motion's own text when it was actually
    # granted -- a denied one has no "why" worth surfacing, and this is
    # an extra fetch+OCR per continuance, so it's skipped for cases that
    # don't need it.
    continuance_reasons: dict[str, str] = {}
    for e in docket.entries:
        if is_extend_sale_date_or_law_day(e.description) and is_granted(e.description) and e.document_url:
            try:
                motion_text = await fetch_document_text(context, throttle, e.document_url)
                reason = extract_continuance_reason(motion_text)
                if reason:
                    continuance_reasons[e.entry_no] = reason
            except Exception:  # noqa: BLE001 -- a failed fetch just means no reason for this bullet
                log.exception("failed fetching continuance motion for %s entry %s", docket_no, e.entry_no)

    case_summary = build_case_summary(
        docket.entries, ranking.key_date, ranking.key_date_label,
        ranking.bankruptcy_chapter, continuance_reasons,
    )

    result = CaseResult(
        town=town,
        street_address=street_address,
        zip_code=zip_code,
        docket_no=docket.docket_no,
        case_caption=docket.case_caption,
        motion_types_found=analysis.motion_types_found,
        total_debt=total_debt,
        appraised_value=appraised_value,
        encumbrances_subsequent_itemized=encumbrances_display,
        encumbrances_subsequent_to_lien=encumbrances_numeric,
        attorney_fees=attorney_fees_display,
        owner_name=analysis.owner_name,
        owner_address_source_urls="; ".join(filter(
            None, [analysis.return_of_service_doc_url, *analysis.appearance_doc_urls]
        )),
        new_decision_maker_source_url=analysis.substitute_party_doc_url or "",
        focus_contact="New Decision Maker" if analysis.substitute_party_doc_url else "Owner",
        default_failure_to_appear=analysis.default_failure_to_appear,
        bankruptcy_stay_reopened=analysis.bankruptcy_stay_reopened,
        bankruptcy_supporting_text=analysis.bankruptcy_supporting_text,
        case_detail_url=docket.case_detail_url,
        worksheet_doc_url=worksheet_url,
        lead_bucket=ranking.lead_bucket,
        judgment_granted=ranking.judgment_granted,
        non_appearing=ranking.non_appearing,
        on_auction_site=ranking.on_auction_site,
        key_date=ranking.key_date.isoformat() if ranking.key_date else None,
        key_date_label=ranking.key_date_label,
        days_to_key_date=ranking.days_to_key_date,
        bankruptcy_chapter=ranking.bankruptcy_chapter,
        continuance_count=ranking.continuance_count,
        warm_cold_subflag=ranking.warm_cold_subflag,
        case_summary=case_summary,
    )
    return result, worksheet


async def run_pipeline(
    page: Page,
    context: BrowserContext,
    throttle: Throttle,
    towns: list[str],
    checkpoint: Checkpoint,
    result_writer: ResultWriter,
    review_writer: ReviewWriter,
    limit: int | None = None,
    case_status: str = "Active Cases",
    property_type: str = "All Properties",
    auction_listing: dict | None = None,
) -> None:
    matched_count = 0

    if auction_listing is None:
        from .auction_site import fetch_statewide_auction_listing
        log.info("fetching statewide pending-foreclosure-sale listing (for HOT/auction-site cross-check)...")
        auction_listing = await fetch_statewide_auction_listing(context, throttle)
        log.info("auction listing: %d pending sales found statewide", len(auction_listing))

    for town in towns:
        if checkpoint.is_town_completed(town):
            log.info("skipping already-completed town: %s", town)
            continue

        log.info("searching town: %s", town)
        town_finished_cleanly = True
        try:
            async for row in iter_town_cases(
                page, throttle, town, property_type=property_type, case_status=case_status
            ):
                if limit is not None and matched_count >= limit:
                    town_finished_cleanly = False
                    break

                if checkpoint.is_docket_processed(row.docket_no):
                    continue

                try:
                    result, worksheet = await process_case(
                        context, throttle, town, row.docket_no, auction_listing,
                        street_address=row.street_address, zip_code=row.zip_code,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad case must not kill the run
                    log.exception("failed processing docket %s", row.docket_no)
                    checkpoint.mark_docket_processed(row.docket_no, town, matched=False, status="error", error=str(exc))
                    review_writer.write(datetime.now(timezone.utc).isoformat(), town, row.docket_no, str(exc))
                    continue

                if result is not None:
                    result_writer.write(result, worksheet)
                    checkpoint.save_case_result(result)
                    matched_count += 1
                    log.info(
                        "MATCH  %s  %s  bucket=%s  motions=%s",
                        town, row.docket_no, result.lead_bucket, result.motion_types_found,
                    )
                    if worksheet is not None and not worksheet.ocr_validated:
                        # Not an exception -- the case processed fine, but the
                        # worksheet OCR extraction is low-confidence (e.g. a
                        # cross-field mismatch, or a value that couldn't be
                        # located at all). Route it to the same review queue
                        # as hard failures, since it needs the same kind of
                        # human look before the numbers are trusted.
                        review_writer.write(
                            datetime.now(timezone.utc).isoformat(), town, row.docket_no,
                            f"OCR not validated: {worksheet.ocr_warning}",
                        )

                checkpoint.mark_docket_processed(row.docket_no, town, matched=result is not None, status="ok")

        except Exception as exc:  # noqa: BLE001 - search/pagination failure for this town
            log.exception("search failed for town %s", town)
            town_finished_cleanly = False

        if town_finished_cleanly:
            checkpoint.mark_town_completed(town)
            log.info("town completed: %s", town)

        if limit is not None and matched_count >= limit:
            log.info("reached --limit of %d matched cases, stopping", limit)
            break

    stats = checkpoint.stats()
    log.info(
        "run summary: %d dockets processed, %d matched, %d errors, %d towns completed",
        stats["dockets_processed"], stats["matched"], stats["errors"], stats["towns_completed"],
    )
