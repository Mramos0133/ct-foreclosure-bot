"""Re-read Order documents for cases whose key date never extracted.

WHY THIS EXISTS AS ITS OWN PASS. The connector list in
order_document.KEY_DATE_PATTERNS was widened on 2026-11-13 after reading
24 real Orders: the court writes "LAW DAY SET FOR Monday, November 16,
2026" and "the new Sale Date of March 6, 2027", neither of which the old
pattern could cross. A normal --update will not pick those up, because
update_run.recheck_matched_case() short-circuits on docket entry count
and never re-reads the Order of a case whose docket has not grown. Every
quiet case -- which is most of them -- would keep its blank Law Day
forever. Hence a pass that goes and reads the Order on purpose.

STRICTLY ADDITIVE. Only cases with NO key_date on record are touched, so
a stored date can never be overwritten by a newly-matchable earlier line
in the same document. A case the Order genuinely has no date for stays
blank, which is the honest state: six of the 24 sampled Orders rule on a
motion to open or send the owner to mediation and state no date at all.

Two requests per case (docket page, then the Order PDF), both throttled,
so this is the expensive kind of pass. It is resumable per case via the
law_day_backfilled table and takes a --budget so it exits cleanly inside
the caller's timeout instead of being killed mid-write -- same discipline
as scripts/run_leg.py.

  python3 scripts/backfill_law_day.py --dry-run --limit 20
  CT_BOT_CHROMIUM_PATH=/opt/pw-browsers/chromium \
    python3 scripts/backfill_law_day.py --budget 540
"""
import argparse
import asyncio
import dataclasses
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.case_analysis import analyze_docket
from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.docket import fetch_docket
from ct_foreclosure_bot.judgment import (
    current_law_day, extract_judgment_type, judgment_date_from_entry,
    last_material_event, title_vested_status,
)
from ct_foreclosure_bot.lead_ranking import days_until
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.order_document import (
    extract_key_date, fetch_document_text, find_order_entry, is_order_granted,
)
from ct_foreclosure_bot.throttle import Throttle

REPO = Path(__file__).resolve().parent.parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--dry-run", action="store_true",
                   help="read and report, write nothing to the checkpoint")
    p.add_argument("--limit", type=int, help="stop after this many cases")
    p.add_argument("--budget", type=float, default=540.0,
                   help="seconds of work before exiting cleanly; keep under the caller's timeout")
    p.add_argument("--min-delay", type=float, default=2.0,
                   help="seconds between requests; must stay >= 2.0")
    p.add_argument("--all", action="store_true",
                   help="include cases with no judgment on record (lower yield, ~7x the work)")
    return p.parse_args()


def targets(db: Path, include_all: bool) -> list[CaseResult]:
    """Cases with no key_date, highest-yield first.

    A case whose Order was read and granted but yielded no date is the
    population this fix was built for, so those run first; with --all,
    the cases with no judgment on record follow, where an Order often
    states no date at all and the yield is far lower.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()
    no_date = [r for r in rows if not r.key_date]
    granted = [r for r in no_date if r.judgment_granted]
    rest = [r for r in no_date if not r.judgment_granted]
    return granted + rest if include_all else granted


async def read_one(context, throttle, r: CaseResult, today: date):
    """Re-read one case's Order. Returns (updated_result, note)."""
    docket = await fetch_docket(context, throttle, r.docket_no)
    analysis = analyze_docket(docket)
    if not analysis.matched_motion_entries:
        return None, "no matched motion"
    anchor = analysis.matched_motion_entries[-1]
    order = find_order_entry(docket.entries, anchor)
    if order is None:
        return None, "no order entry"
    text = await fetch_document_text(context, throttle, order.document_url)

    key_date, label = extract_key_date(text)
    granted = is_order_granted(text)
    judgment_type = extract_judgment_type(text, label, r.motion_types_found)
    iso = key_date.isoformat() if key_date else None
    event, event_date = last_material_event(docket.entries)

    updated = dataclasses.replace(
        r,
        key_date=iso,
        key_date_label=label,
        days_to_key_date=days_until(iso, today),
        judgment_granted=granted,
        judgment_type=judgment_type,
        judgment_date=judgment_date_from_entry(order),
        current_law_day=current_law_day(iso, judgment_type),
        last_material_event=event,
        last_material_event_date=event_date,
        title_vested_status=title_vested_status(judgment_type, iso, today, event_date),
    )
    if key_date:
        note = f"{label} {iso}"
    else:
        note = "order states no date"
    if granted != bool(r.judgment_granted):
        note += f"  [judgment_granted {bool(r.judgment_granted)} -> {granted}]"
    return updated, note


async def run(args) -> int:
    db = REPO / args.checkpoint_db
    today = date.today()
    cases = targets(db, args.all)
    cp = Checkpoint(str(db))
    try:
        pending = [r for r in cases if not cp.is_law_day_backfilled(r.docket_no)]
        print(f"cases with no key_date in scope : {len(cases)}")
        print(f"already backfilled              : {len(cases) - len(pending)}")
        print(f"to read now                     : {len(pending)}"
              f"{f' (limited to {args.limit})' if args.limit else ''}\n")
        if args.limit:
            pending = pending[:args.limit]
        if not pending:
            print("nothing to do -- backfill complete")
            return 0

        started = time.monotonic()
        dated = blank = failed = flag_flips = 0
        throttle = Throttle(min_delay=args.min_delay, max_delay=args.min_delay + 1.0)
        async with async_playwright() as p:
            browser = await launch_browser(p, headless=True)
            context = await new_context(browser)
            try:
                for i, r in enumerate(pending, 1):
                    if time.monotonic() - started > args.budget:
                        print(f"\nbudget of {args.budget:.0f}s reached -- exiting cleanly")
                        break
                    try:
                        updated, note = await read_one(context, throttle, r, today)
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        print(f"  {i:>4}/{len(pending)} {r.docket_no:<22} FAILED {exc!r}")
                        continue
                    if updated is None:
                        blank += 1
                        print(f"  {i:>4}/{len(pending)} {r.docket_no:<22} {r.town:<14} -- {note}")
                        if not args.dry_run:
                            cp.mark_law_day_backfilled(r.docket_no)
                        continue
                    if updated.key_date:
                        dated += 1
                    else:
                        blank += 1
                    if bool(updated.judgment_granted) != bool(r.judgment_granted):
                        flag_flips += 1
                    print(f"  {i:>4}/{len(pending)} {r.docket_no:<22} {r.town:<14} {note}")
                    if not args.dry_run:
                        cp.save_case_result(updated)
                        cp.mark_law_day_backfilled(r.docket_no)
            finally:
                await context.close()
                await browser.close()

        elapsed = time.monotonic() - started
        print(f"\nread {dated + blank + failed} orders in {elapsed/60:.1f} min")
        print(f"  key date recovered            : {dated}")
        print(f"  order genuinely states none   : {blank}")
        print(f"  judgment_granted corrected    : {flag_flips}")
        print(f"  fetch/OCR failed (retryable)  : {failed}")
        # Recount against the live checkpoint rather than subtracting the
        # marker total from the starting scope: a recovered date removes
        # that case from scope, so the subtraction goes negative and
        # reports "0 still to read" while retryable failures are in fact
        # still outstanding.
        remaining = sum(1 for r in targets(db, args.all)
                        if not cp.is_law_day_backfilled(r.docket_no))
        print(f"  still to read (incl. retries) : {remaining}")
        if args.dry_run:
            print("\ndry run -- nothing written")
        return 0 if remaining <= 0 else 2
    finally:
        cp.close()


def main() -> int:
    args = parse_args()
    if args.min_delay < 2.0:
        raise SystemExit("--min-delay must stay at or above 2.0 seconds")
    return asyncio.run(run(args))


sys.exit(main())
