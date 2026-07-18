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
from datetime import datetime, timezone

from playwright.async_api import Page, BrowserContext

from .case_analysis import analyze_docket
from .checkpoint import Checkpoint
from .docket import fetch_docket
from .models import CaseResult
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
) -> tuple[CaseResult | None, object | None]:
    """Fetch + analyze one docket. Returns (CaseResult or None, WorksheetFields or None).

    Returns (None, None) for a case that doesn't match either target motion
    -- that is not an error, just a non-match, and the caller still
    checkpoints it so it is not re-fetched on resume.
    """
    docket = await fetch_docket(context, throttle, docket_no)
    analysis = analyze_docket(docket)

    if not analysis.motion_types_found:
        return None, None

    worksheet = None
    encumbrances_display = ""
    total_debt = None
    attorney_fees_display = None
    worksheet_url = analysis.worksheet_entry.document_url if analysis.worksheet_entry else None

    if analysis.worksheet_entry is not None:
        worksheet = await fetch_and_extract_worksheet(
            context, throttle, analysis.worksheet_entry.entry_no, analysis.worksheet_entry.document_url
        )
        total_debt = worksheet.updated_debt
        attorney_fees_display = worksheet.attorney_fees_raw
        if worksheet.encumbrances_subsequent_to_lien is not None:
            # The worksheet only ever gives a lump-sum total for this line --
            # no per-holder itemization is present on the source document.
            encumbrances_display = (
                f"${worksheet.encumbrances_subsequent_to_lien:,.2f} total "
                "(source document does not itemize by holder)"
            )
    else:
        encumbrances_display = "no Foreclosure Worksheet found in docket"

    result = CaseResult(
        town=town,
        docket_no=docket.docket_no,
        case_caption=docket.case_caption,
        motion_types_found=analysis.motion_types_found,
        total_debt=total_debt,
        encumbrances_subsequent_itemized=encumbrances_display,
        attorney_fees=attorney_fees_display,
        default_failure_to_appear=analysis.default_failure_to_appear,
        bankruptcy_stay_reopened=analysis.bankruptcy_stay_reopened,
        bankruptcy_supporting_text=analysis.bankruptcy_supporting_text,
        case_detail_url=docket.case_detail_url,
        worksheet_doc_url=worksheet_url,
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
    case_status: str = "All Cases",
    property_type: str = "All Properties",
) -> None:
    matched_count = 0

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
                    result, worksheet = await process_case(context, throttle, town, row.docket_no)
                except Exception as exc:  # noqa: BLE001 - one bad case must not kill the run
                    log.exception("failed processing docket %s", row.docket_no)
                    checkpoint.mark_docket_processed(row.docket_no, town, matched=False, status="error", error=str(exc))
                    review_writer.write(datetime.now(timezone.utc).isoformat(), town, row.docket_no, str(exc))
                    continue

                if result is not None:
                    result_writer.write(result, worksheet)
                    matched_count += 1
                    log.info("MATCH  %s  %s  motions=%s", town, row.docket_no, result.motion_types_found)
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
