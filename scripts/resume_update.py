"""Resume an interrupted statewide --update without redoing finished work.

run_update() clears completed_towns before phase 1 so every town gets
re-searched for new filings. That is right for a fresh invocation and
wrong for a resume: after a mid-run interruption it would throw away the
towns already walked. This runs the same two phases while leaving
completed_towns intact, so phase 1 picks up at the first unfinished town.

Phase 2 is unchanged -- recheck_matched_case() is reused as-is, so
already-matched cases still get the cheap staleness check plus the
SOP-rule refresh via reclassify_from_docket().

  python3 scripts/resume_update.py --checkpoint-db statewide_checkpoint.sqlite3 \
      --output statewide_results.csv --review-log statewide_review.csv \
      --export-xlsx statewide_leads.xlsx
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.auction_site import fetch_statewide_auction_listing
from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.excel_export import export_to_xlsx
from ct_foreclosure_bot.output import ResultWriter, ReviewWriter
from ct_foreclosure_bot.pipeline import run_pipeline
from ct_foreclosure_bot.throttle import Throttle
from ct_foreclosure_bot.towns import CT_TOWNS
from ct_foreclosure_bot.update_run import recheck_matched_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("ct_foreclosure_bot")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--output", default="statewide_results.csv")
    p.add_argument("--review-log", default="statewide_review.csv")
    p.add_argument("--export-xlsx", default="statewide_leads.xlsx")
    p.add_argument("--skip-phase1", action="store_true",
                   help="Phase 1 already finished; go straight to the recheck.")
    return p.parse_args()


async def main():
    args = parse_args()
    checkpoint = Checkpoint(args.checkpoint_db)
    result_writer = ResultWriter(args.output)
    review_writer = ReviewWriter(args.review_log)
    throttle = Throttle(min_delay=2.0, max_delay=3.0)

    before = {r.docket_no: r for r in checkpoint.all_case_results()}
    done_towns = {t for t in CT_TOWNS if checkpoint.is_town_completed(t)}
    log.info("resume: %d cases on record, %d/%d towns already walked",
             len(before), len(done_towns), len(CT_TOWNS))

    new_dockets, updated_dockets = set(), set()
    try:
        async with async_playwright() as p:
            browser = await launch_browser(p, headless=True)
            try:
                context = await new_context(browser)
                log.info("fetching statewide auction listing...")
                auction_listing = await fetch_statewide_auction_listing(context, throttle)
                log.info("auction listing: %d pending sales", len(auction_listing))

                if not args.skip_phase1:
                    page = await context.new_page()
                    # NOTE: completed_towns deliberately NOT cleared.
                    await run_pipeline(
                        page=page, context=context, throttle=throttle, towns=list(CT_TOWNS),
                        checkpoint=checkpoint, result_writer=result_writer,
                        review_writer=review_writer, auction_listing=auction_listing,
                    )
                    await page.close()
                    new_dockets = {r.docket_no for r in checkpoint.all_case_results()} - set(before)
                    log.info("phase 1 done: %d genuinely new matched cases", len(new_dockets))

                checked = 0
                for docket_no, old_result in before.items():
                    checked += 1
                    try:
                        new_result, changed = await recheck_matched_case(
                            context, throttle, old_result, auction_listing
                        )
                    except Exception:  # noqa: BLE001 -- one bad recheck must not sink the run
                        log.exception("failed rechecking %s", docket_no)
                        continue
                    if new_result is not old_result:
                        checkpoint.save_case_result(new_result)
                    if changed:
                        updated_dockets.add(docket_no)
                        log.info("UPDATED  %s  %s  bucket %s -> %s", old_result.town,
                                 docket_no, old_result.lead_bucket, new_result.lead_bucket)
                    if checked % 100 == 0:
                        log.info("phase 2 progress: %d/%d rechecked", checked, len(before))
            finally:
                await browser.close()
    finally:
        result_writer.close()
        review_writer.close()
        if args.export_xlsx:
            counts = export_to_xlsx(
                checkpoint.all_case_results(), args.export_xlsx,
                new_dockets=new_dockets, updated_dockets=updated_dockets,
            )
            log.info("exported %s: %s  (new=%d updated=%d)", args.export_xlsx, counts,
                     len(new_dockets), len(updated_dockets))
        checkpoint.close()


asyncio.run(main())
