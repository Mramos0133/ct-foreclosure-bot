"""Re-derive lead_bucket for every stored case from data already on
record -- no network, no document OCR.

Exists for the case where only the RULES changed, not the dockets. The
--update flow reaches every case too (see update_run.reclassify_from_docket)
but costs one docket fetch each, which is hours for the full checkpoint.
When a rule depends solely on fields already stored -- as the
assistance-elapsed HOT window does, since assistance_state and
assistance_elapsed_date are persisted -- nothing needs re-fetching and
the whole checkpoint reclassifies in seconds.

ADDITIVE by design: it only ever promotes a case that the new
assistance-elapsed rule now qualifies. It does not re-derive the other
rules, because not every input they depend on is persisted -- deriving
from a partially-stored record silently demotes cases that are correctly
classified. Anything needing a full re-derivation goes through
--update, which has the actual docket in hand.

Deliberately does NOT rebuild case_summary: that needs the docket entries,
which are not stored. The summary already carries the underlying facts
(assistance elapsed date, complaint date, probate); only the bucket label
is stale, and that is what this fixes.

  python3 scripts/reclassify_offline.py --checkpoint-db statewide_checkpoint.sqlite3
  python3 scripts/reclassify_offline.py --dry-run
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.lead_ranking import (
    equity_bucket_override, is_assistance_elapsed_hot, short_sale_ratio,
)
from ct_foreclosure_bot.models import CaseResult


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--dry-run", action="store_true", help="Report the movement without writing.")
    return p.parse_args()


def _d(iso):
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def reclassify(r: CaseResult, today: date) -> str:
    """Return the bucket this case should carry. Only the new
    assistance-elapsed rule is evaluated; every other bucket is left as
    stored -- see the module docstring on why this is additive.
    """
    if r.assistance_state != "elapsed":
        return r.lead_bucket
    if not is_assistance_elapsed_hot(_d(r.assistance_elapsed_date), today):
        return r.lead_bucket

    r.assistance_elapsed_hot = True
    # Equity still outranks every distress signal.
    forced = equity_bucket_override(
        short_sale_ratio(r.total_debt, r.appraised_value, r.encumbrances_subsequent_to_lien)
    )
    if forced:
        return forced
    # Never demote: a case already WARM/COLD/SHORT_SALE earned that from a
    # rule this pass is not re-evaluating.
    return "HOT" if r.lead_bucket == "UNCLASSIFIED" else r.lead_bucket


def main():
    args = parse_args()
    today = date.today()
    con = sqlite3.connect(args.checkpoint_db)
    rows = con.execute("SELECT docket_no, data FROM case_results").fetchall()

    moves, after = {}, {}
    updates = []
    for docket_no, data in rows:
        r = CaseResult(**json.loads(data))
        before = r.lead_bucket
        new_bucket = reclassify(r, today)
        after[new_bucket] = after.get(new_bucket, 0) + 1
        if new_bucket != before:
            moves[f"{before} -> {new_bucket}"] = moves.get(f"{before} -> {new_bucket}", 0) + 1
        r.lead_bucket = new_bucket
        updates.append((new_bucket, json.dumps(r.__dict__), docket_no))

    if not args.dry_run:
        con.executemany(
            "UPDATE case_results SET lead_bucket = ?, data = ? WHERE docket_no = ?", updates
        )
        con.commit()
    con.close()

    print(("DRY RUN -- " if args.dry_run else "") + f"reclassified {len(rows)} cases")
    print("movement:")
    for k, v in sorted(moves.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5}  {k}")
    print("resulting buckets:")
    for b, n in sorted(after.items(), key=lambda kv: -kv[1]):
        print(f"  {b:22} {n}")


if __name__ == "__main__":
    main()
