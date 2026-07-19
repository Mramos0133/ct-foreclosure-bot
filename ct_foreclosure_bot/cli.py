"""CLI entrypoint.

Usage examples:
    python -m ct_foreclosure_bot --town Hartford --limit 5
    python -m ct_foreclosure_bot --output results.csv
    python -m ct_foreclosure_bot --town "New Haven" --headed

Statewide runs can be split into batches against one shared
--checkpoint-db, each batch resuming where the last left off:
    python -m ct_foreclosure_bot --checkpoint-db statewide.sqlite3 --town-start 0 --town-count 15
    python -m ct_foreclosure_bot --checkpoint-db statewide.sqlite3 --town-start 15 --town-count 15
    ...

Interrupt with Ctrl-C at any time -- already-processed dockets and
completed towns are checkpointed in the SQLite file (--checkpoint-db) and
will be skipped automatically on the next run with the same --checkpoint-db
path. Matched cases are written to --output as they are found, not
buffered to the end.
"""

import argparse
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

from .browser import launch_browser, new_context
from .checkpoint import Checkpoint
from .excel_export import export_to_xlsx
from .output import ResultWriter, ReviewWriter
from .pipeline import run_pipeline
from .search import open_search_form  # noqa: F401 (kept for potential future warmup use)
from .throttle import Throttle
from .towns import CT_TOWNS


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ct_foreclosure_bot",
        description=(
            "Search CT Judicial Branch civil case records for foreclosure "
            "cases with a target judgment motion, and extract Foreclosure "
            "Worksheet figures. Respects the site's robots.txt disallow by "
            "running strictly sequentially with a conservative delay "
            "between every request; treat rate limiting as non-negotiable, "
            "not a tunable to lower for speed."
        ),
    )
    p.add_argument("--town", help="Process a single CT town instead of all 169 (e.g. 'Hartford'). Useful for testing.")
    p.add_argument("--town-start", type=int, default=None, help="0-based index into the alphabetical 169-town list to start at -- for running the statewide list in batches against a shared --checkpoint-db. Mutually exclusive with --town.")
    p.add_argument("--town-count", type=int, default=None, help="Number of towns to process starting at --town-start (default: all remaining). Ignored without --town-start.")
    p.add_argument("--limit", type=int, default=None, help="Stop after this many MATCHED cases (across all towns processed this run). Useful for a quick test batch.")
    p.add_argument("--case-status", default="Active Cases", choices=["All Cases", "Active Cases", "Disposed Cases"], help="Defaults to Active Cases only -- pass 'All Cases' or 'Disposed Cases' to widen it.")
    p.add_argument("--property-type", default="All Properties", choices=["All Properties", "Commercial Properties", "Residential Properties"])
    p.add_argument("--output", default="ct_foreclosure_results.csv", help="Output CSV path (appended to; safe to resume into the same file).")
    p.add_argument("--review-log", default="needs_manual_review.csv", help="CSV of dockets that errored during processing.")
    p.add_argument("--checkpoint-db", default="ct_foreclosure_checkpoint.sqlite3", help="SQLite checkpoint file; reuse the same path to resume a run.")
    p.add_argument("--export-xlsx", default="ct_foreclosure_leads.xlsx", help="Ranked, multi-sheet (HOT/WARM/COLD/POTENTIAL_SHORT_SALE/UNCLASSIFIED) Excel workbook, (re)written at the end of every run from the checkpoint's full case data. Pass '' to skip.")
    p.add_argument("--export-only", action="store_true", help="Skip crawling entirely; just (re)build --export-xlsx from the existing --checkpoint-db. Useful to refresh the ranked workbook without hitting the site again.")
    p.add_argument("--headed", action="store_true", help="Run the browser headed (debugging only).")
    p.add_argument("--proxy", default=None, help="Explicit proxy server (defaults to HTTPS_PROXY env var if set).")
    p.add_argument("--disable-tls12-workaround", action="store_true", help="Disable forcing Chromium to TLS 1.2 max. Only useful if you are NOT behind a TLS-intercepting proxy that mishandles TLS 1.3, and you need TLS 1.3.")
    p.add_argument("--min-delay", type=float, default=2.0, help="Minimum seconds between requests to the site. Do not set below 2.0.")
    p.add_argument("--max-delay", type=float, default=3.0, help="Maximum seconds between requests to the site.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def _main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.min_delay < 2.0:
        raise SystemExit("--min-delay must be >= 2.0 seconds; this site's robots.txt disallows automated access entirely, so conservative pacing is a hard requirement, not a default to lower.")

    if args.export_only:
        checkpoint = Checkpoint(args.checkpoint_db)
        try:
            if args.export_xlsx:
                counts = export_to_xlsx(checkpoint.all_case_results(), args.export_xlsx)
                logging.getLogger("ct_foreclosure_bot").info(
                    "exported %s: HOT=%d WARM=%d COLD=%d SHORT_SALE=%d UNCLASSIFIED=%d",
                    args.export_xlsx, counts["HOT"], counts["WARM"], counts["COLD"],
                    counts["POTENTIAL_SHORT_SALE"], counts["UNCLASSIFIED"],
                )
        finally:
            checkpoint.close()
        return

    if args.town:
        towns = [args.town]
    elif args.town_start is not None:
        if not (0 <= args.town_start < len(CT_TOWNS)):
            raise SystemExit(f"--town-start must be in [0, {len(CT_TOWNS) - 1}]")
        end = args.town_start + args.town_count if args.town_count is not None else len(CT_TOWNS)
        towns = CT_TOWNS[args.town_start:end]
    else:
        towns = list(CT_TOWNS)

    checkpoint = Checkpoint(args.checkpoint_db)
    result_writer = ResultWriter(args.output)
    review_writer = ReviewWriter(args.review_log)
    throttle = Throttle(min_delay=args.min_delay, max_delay=args.max_delay)

    try:
        async with async_playwright() as p:
            browser = await launch_browser(
                p,
                headless=not args.headed,
                proxy_server=args.proxy,
                disable_tls12_workaround=args.disable_tls12_workaround,
            )
            try:
                context = await new_context(browser)
                page = await context.new_page()
                await run_pipeline(
                    page=page,
                    context=context,
                    throttle=throttle,
                    towns=towns,
                    checkpoint=checkpoint,
                    result_writer=result_writer,
                    review_writer=review_writer,
                    limit=args.limit,
                    case_status=args.case_status,
                    property_type=args.property_type,
                )
            finally:
                await browser.close()
    finally:
        result_writer.close()
        review_writer.close()
        if args.export_xlsx:
            counts = export_to_xlsx(checkpoint.all_case_results(), args.export_xlsx)
            logging.getLogger("ct_foreclosure_bot").info(
                "exported %s: HOT=%d WARM=%d COLD=%d SHORT_SALE=%d UNCLASSIFIED=%d",
                args.export_xlsx, counts["HOT"], counts["WARM"], counts["COLD"],
                counts["POTENTIAL_SHORT_SALE"], counts["UNCLASSIFIED"],
            )
        checkpoint.close()


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted -- progress is checkpointed; re-run the same command to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
