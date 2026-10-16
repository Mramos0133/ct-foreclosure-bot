"""Export the master workbook, diffed against a baseline checkpoint commit.

Green rows = dockets absent from the baseline (genuinely new leads).
Yellow rows = dockets present in the baseline whose classification changed
in a way that matters for outreach (update_run.MEANINGFUL_FIELDS -- bucket
moves, a new judgment, a probate finding, the assistance clock closing,
and so on). Cosmetic churn like a recomputed days_to_key_date is
deliberately excluded, so yellow always means "worth another look".

Routes through export_to_xlsx() so the sheet matches the master format
exactly rather than drifting into a bespoke layout.

days_to_key_date is recomputed against today before writing, the same way
city_pull.py does it. The stored value is whatever the clock said when the
case was last scraped, so on 2026-10-16 the sheet showed twelve HOT cases
as "within 30 days" whose sale had in fact already happened -- one of them
reading "1 day" on a sale from July -- while six genuine next-day sales
read as two to nine weeks out. Only the column is corrected here; the
checkpoint keeps the scrape-time value, and a recomputed day count is not
a "meaningful" change, so this never turns a row yellow on its own.

  python3 scripts/export_final.py --baseline daf3587 --output statewide_leads.xlsx
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.excel_export import export_to_xlsx
from ct_foreclosure_bot.lead_ranking import days_until
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.update_run import _meaningfully_different

REPO = Path(__file__).resolve().parent.parent
DB_REL = "statewide_checkpoint.sqlite3"


def load_baseline(rev: str) -> dict[str, CaseResult]:
    raw = subprocess.run(["git", "show", f"{rev}:{DB_REL}"], cwd=REPO, capture_output=True)
    if raw.returncode != 0 or not raw.stdout:
        raise SystemExit(f"cannot read {DB_REL} at {rev}")
    tmp = Path(tempfile.mkstemp(suffix=".sqlite3")[1])
    tmp.write_bytes(raw.stdout)
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        out = {}
        for (data,) in con.execute("SELECT data FROM case_results"):
            d = json.loads(data)
            out[d["docket_no"]] = CaseResult(**d)
        con.close()
        return out
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True, help="git rev holding the previous checkpoint")
    p.add_argument("--output", default="statewide_leads.xlsx")
    p.add_argument("--live-auction", default=None,
                   help="JSON {docket_no: 'YYYY-MM-DD'} of sale dates re-read from the "
                        "auction site; fills in dates the Order document never carried")
    args = p.parse_args()

    base = load_baseline(args.baseline)
    cp = Checkpoint(str(REPO / DB_REL))
    try:
        current = cp.all_case_results()
    finally:
        cp.close()

    new_dockets, updated_dockets = set(), set()
    for r in current:
        old = base.get(r.docket_no)
        if old is None:
            new_dockets.add(r.docket_no)
        elif _meaningfully_different(old, r):
            updated_dockets.add(r.docket_no)

    # Everything below runs AFTER the diff, so a refreshed sale date or a
    # recomputed day count can never be what turns a row yellow.
    today = date.today()

    filled = 0
    if args.live_auction:
        live = json.loads(Path(args.live_auction).read_text())
        for r in current:
            fresh = live.get(r.docket_no)
            if fresh and fresh != r.key_date:
                r.key_date = fresh
                r.key_date_label = "Sale Date"
                r.on_auction_site = True
                filled += 1

    corrected = past = 0
    for r in current:
        fresh_days = days_until(r.key_date, today)
        if fresh_days != r.days_to_key_date:
            corrected += 1
        r.days_to_key_date = fresh_days
        if fresh_days is not None and fresh_days < 0:
            past += 1

    counts = export_to_xlsx(current, str(REPO / args.output),
                            new_dockets=new_dockets, updated_dockets=updated_dockets)

    print(f"baseline {args.baseline}: {len(base)} cases")
    print(f"current:  {len(current)} cases")
    print(f"NEW (green):     {len(new_dockets)}")
    print(f"UPDATED (yellow): {len(updated_dockets)}")
    print(f"buckets: {counts}")
    if args.live_auction:
        print(f"sale dates refreshed from auction site: {filled}")
    print(f"days_to_key_date corrected against {today}: {corrected}")
    print(f"  ...of which the date has already passed: {past}")

    # Bucket split of just the new leads -- that is the part a caller acts on first.
    by_bucket: dict[str, int] = {}
    for r in current:
        if r.docket_no in new_dockets:
            by_bucket[r.lead_bucket or "UNCLASSIFIED"] = by_bucket.get(r.lead_bucket or "UNCLASSIFIED", 0) + 1
    print(f"new-lead buckets: {by_bucket}")
    return 0


sys.exit(main())
