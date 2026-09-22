"""Backfill complaint principal / P&I-unpaid-since on cases already scraped.

The unpaid-since patterns were fixed in d20f9ff after being confirmed
against real pleadings, but the fix only reaches cases scraped AFTER it.
The 457 complaints already OCR'd under the old patterns still carry the
values those patterns produced -- 35 dates out of 457. Re-reading the
complaint PDFs applies the corrected patterns to them.

Marks every docket it touches in `complaint_backfilled`, whether or not a
date came out. That table is the point: roughly 40% of complaints state no
default date at all (they plead "in default for nonpayment of monthly
installments" and stop), so a still-blank cell cannot distinguish "not yet
attempted" from "attempted, genuinely absent". Without the marker the job
would re-fetch those PDFs on every pass forever.

Bounded and resumable like run_leg.py, for the same reason: the container
suspends about five minutes after a turn ends, so work has to happen in
the foreground in chunks that commit as they go. Exit 0 when every case is
done, 2 when more remain.

  python3 scripts/backfill_complaints.py --budget 540
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from checkpoint_autosave import commit_snapshot

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.complaint_document import (
    fetch_complaint_text, extract_principal_amount, extract_unpaid_since,
)
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.throttle import Throttle

REPO = Path(__file__).resolve().parent.parent
DB_REL = "statewide_checkpoint.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaint_backfilled (
    docket_no TEXT PRIMARY KEY,
    backfilled_at TEXT NOT NULL
);
"""


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default=DB_REL)
    p.add_argument("--budget", type=int, default=540,
                   help="Wall-clock seconds. Keep UNDER the caller's timeout so the "
                        "run stops cleanly and commits instead of being killed.")
    return p.parse_args()


def todo(con) -> list[dict]:
    done = {r[0] for r in con.execute("SELECT docket_no FROM complaint_backfilled")}
    out = []
    for (data,) in con.execute("SELECT data FROM case_results"):
        r = json.loads(data)
        if not (r.get("recent_complaint_hot") and r.get("complaint_doc_url")):
            continue
        if r["docket_no"] in done:
            continue
        out.append(r)
    return out


async def main() -> int:
    args = parse_args()
    started = time.time()

    con = sqlite3.connect(REPO / args.checkpoint_db)
    con.executescript(SCHEMA)
    con.commit()
    pending = todo(con)
    total_scope = con.execute(
        "SELECT COUNT(*) FROM complaint_backfilled").fetchone()[0] + len(pending)
    con.close()

    print(f"{len(pending)} of {total_scope} complaints left to backfill", flush=True)
    if not pending:
        print("COMPLETE", flush=True)
        return 0

    checkpoint = Checkpoint(str(REPO / args.checkpoint_db))
    con = sqlite3.connect(REPO / args.checkpoint_db)
    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    gained_date = gained_principal = attempted = failed = 0

    try:
        async with async_playwright() as p:
            browser = await launch_browser(p, headless=True)
            try:
                context = await new_context(browser)
                for r in pending:
                    if time.time() - started > args.budget:
                        print("  budget reached -- stopping cleanly", flush=True)
                        break
                    docket = r["docket_no"]
                    try:
                        text = await fetch_complaint_text(context, throttle, r["complaint_doc_url"])
                    except Exception as exc:  # noqa: BLE001
                        # Do NOT mark it done: a transport failure should be
                        # retried on the next pass, unlike a clean read that
                        # simply found no date.
                        failed += 1
                        print(f"  {docket}: fetch failed ({type(exc).__name__}) -- will retry", flush=True)
                        continue

                    attempted += 1
                    new_date = extract_unpaid_since(text)
                    new_principal = extract_principal_amount(text)

                    result = CaseResult(**r)
                    if new_date and not result.complaint_unpaid_since:
                        gained_date += 1
                    if new_principal is not None and result.complaint_principal_amount is None:
                        gained_principal += 1
                    # Only ever fill or correct from a fresh read; never blank
                    # a value that is already on the sheet.
                    if new_date:
                        result.complaint_unpaid_since = new_date
                    if new_principal is not None:
                        result.complaint_principal_amount = new_principal
                    checkpoint.save_case_result(result)

                    con.execute(
                        "INSERT OR REPLACE INTO complaint_backfilled (docket_no, backfilled_at) "
                        "VALUES (?, datetime('now'))", (docket,))
                    con.commit()

                    if attempted % 20 == 0:
                        print(f"  {attempted} read, +{gained_date} dates, "
                              f"+{gained_principal} principals", flush=True)
            finally:
                await browser.close()
    finally:
        checkpoint.close()
        remaining = len(todo(con))
        con.close()

    print(f"  read {attempted}, +{gained_date} new dates, +{gained_principal} new principals, "
          f"{failed} fetch failures", flush=True)
    try:
        saved = commit_snapshot(DB_REL, f"complaint backfill: {total_scope - remaining}/{total_scope}")
        print(f"  checkpoint: {'pushed' if saved else 'no change'}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  checkpoint push FAILED: {exc}", flush=True)

    print(f"  {remaining} still to do", flush=True)
    if remaining == 0:
        print("COMPLETE", flush=True)
        return 0
    return 2


sys.exit(asyncio.run(main()))
