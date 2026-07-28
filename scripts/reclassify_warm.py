"""One-time pass: fully reprocess every existing WARM lead in a checkpoint
so the new EMAP/loan-mod-then-failed HOT rule (and any bankruptcy-window
drift from time passing) gets applied to cases that were classified before
that rule existed.

`recheck_matched_case` (update_run.py) short-circuits when a docket's
entry count hasn't grown -- a fine cost-saver for "did anything happen,"
but useless here since what changed is the classification *logic*, not
the docket. So this bypasses that and calls process_case() directly for
every WARM docket_no, same as update_run.py's own full-reprocess fallback
path, and re-exports the workbook when done. Resumable: re-run the same
command and it picks up where it left off via --checkpoint-db state
(cases whose bucket is no longer WARM are skipped next time).

Usage: python3 scripts/reclassify_warm.py <checkpoint_db> <export_xlsx> [--headed]
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/home/user/ct-foreclosure-bot")

from playwright.async_api import async_playwright

from ct_foreclosure_bot.auction_site import fetch_statewide_auction_listing
from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.excel_export import export_to_xlsx
from ct_foreclosure_bot.pipeline import process_case
from ct_foreclosure_bot.throttle import Throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("reclassify_warm")


async def main(checkpoint_db: str, export_xlsx: str, headed: bool) -> None:
    checkpoint = Checkpoint(checkpoint_db)
    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    try:
        warm = [r for r in checkpoint.all_case_results() if r.lead_bucket == "WARM"]
        log.info("%d WARM cases to reclassify", len(warm))

        async with async_playwright() as p:
            browser = await launch_browser(p, headless=not headed)
            try:
                context = await new_context(browser)
                log.info("fetching statewide auction listing...")
                auction_listing = await fetch_statewide_auction_listing(context, throttle)
                log.info("auction listing: %d pending sales", len(auction_listing))

                promoted, unchanged, errors = 0, 0, 0
                for i, old_result in enumerate(warm, 1):
                    try:
                        new_result, _worksheet = await process_case(
                            context, throttle, old_result.town, old_result.docket_no, auction_listing,
                            street_address=old_result.street_address, zip_code=old_result.zip_code,
                        )
                    except Exception:
                        log.exception("failed reclassifying %s", old_result.docket_no)
                        errors += 1
                        continue

                    if new_result is None:
                        log.warning("%s no longer matches a target motion -- keeping WARM", old_result.docket_no)
                        continue

                    checkpoint.save_case_result(new_result)
                    if new_result.lead_bucket != "WARM":
                        promoted += 1
                        log.info(
                            "RECLASSIFIED  %s  %s  WARM -> %s  (bankruptcy_hot=%s assistance_hot=%s)",
                            old_result.town, old_result.docket_no, new_result.lead_bucket,
                            new_result.bankruptcy_reopen_hot, new_result.assistance_program_hot,
                        )
                    else:
                        unchanged += 1

                    if i % 10 == 0:
                        log.info("progress: %d/%d done (%d promoted, %d unchanged, %d errors)",
                                  i, len(warm), promoted, unchanged, errors)
            finally:
                await browser.close()

        log.info("done: %d promoted out of WARM, %d still WARM, %d errors", promoted, unchanged, errors)
    finally:
        checkpoint.close()

    counts = export_to_xlsx(Checkpoint(checkpoint_db).all_case_results(), export_xlsx)
    log.info(
        "exported %s: HOT=%d WARM=%d COLD=%d SHORT_SALE=%d UNCLASSIFIED=%d",
        export_xlsx, counts["HOT"], counts["WARM"], counts["COLD"],
        counts["POTENTIAL_SHORT_SALE"], counts["UNCLASSIFIED"],
    )


if __name__ == "__main__":
    checkpoint_db = sys.argv[1]
    export_xlsx = sys.argv[2]
    headed = "--headed" in sys.argv
    asyncio.run(main(checkpoint_db, export_xlsx, headed))
