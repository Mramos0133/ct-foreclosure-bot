"""Apply the 2026-09-27 rule fixes to cases already on record.

Two changes landed that only reach cases scraped afterwards:

  1. Judgment + non-appearing is now checked BEFORE the continuance
     demotion (lead_ranking.decide_bucket).
  2. An "Updated debt" reading below MIN_PLAUSIBLE_DEBT is treated as
     unknown rather than as a real figure (worksheet._plausible_debt).

ADDITIVE, for the same reason reclassify_offline.py is: not every input
decide_bucket depends on is persisted on a CaseResult, so a blind
re-derivation silently demotes cases that are correctly classified. A
faithful replay attempted during development disagreed with 108 of 2565
stored buckets using the OLD rules, which is the measure of how wrong
that approach is. So this evaluates only what the stored fields settle
conclusively:

  - Promotion: a COLD case is moved to HOT when judgment_granted,
    non_appearing, not on_auction_site and the key date has not passed --
    every one of those is persisted -- AND the equity override does not
    independently force COLD. That is exactly the reordering, applied to
    the subset where the stored record is sufficient to decide it.

  - Debt: an implausible total_debt is cleared to None, because the
    figure is false and it is on the spreadsheet. Where that figure was
    the sole reason for the case's bucket, the bucket is LEFT ALONE and
    reported, since re-deriving the underlying bucket needs the docket.
    The next --update pass fixes those with the docket in hand.

  python3 scripts/apply_rule_fixes.py --dry-run
  python3 scripts/apply_rule_fixes.py
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.lead_ranking import equity_bucket_override, short_sale_ratio
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.worksheet import MIN_PLAUSIBLE_DEBT, _plausible_debt

REPO = Path(__file__).resolve().parent.parent


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def equity_forces(r: CaseResult, debt) -> str | None:
    return equity_bucket_override(
        short_sale_ratio(debt, r.appraised_value, r.encumbrances_subsequent_to_lien)
    )


def meets_judgment_hot(r: CaseResult) -> bool:
    """The original HOT rule, evaluated purely from persisted fields."""
    key_past = r.days_to_key_date is not None and r.days_to_key_date < 0
    return bool(
        r.judgment_granted and r.non_appearing and not r.on_auction_site and not key_past
    )


def main() -> int:
    args = parse_args()
    con = sqlite3.connect(f"file:{REPO / args.checkpoint_db}?mode=ro", uri=True)
    records = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    promoted, debt_cleared, bucket_now_stale = [], [], []

    for r in records:
        bad_debt = r.total_debt is not None and _plausible_debt(r.total_debt) is None
        if bad_debt:
            # Was this figure the only thing putting the case in its bucket?
            was_forced = equity_forces(r, r.total_debt)
            now_forced = equity_forces(r, None)
            debt_cleared.append((r, r.total_debt))
            if was_forced == r.lead_bucket and now_forced is None:
                bucket_now_stale.append(r)

        effective_debt = _plausible_debt(r.total_debt)
        if (
            r.lead_bucket == "COLD"
            and meets_judgment_hot(r)
            and (r.continuance_count or 0) >= 2
            and equity_forces(r, effective_debt) is None
        ):
            promoted.append(r)

    print(f"promotions COLD -> HOT (reordering):           {len(promoted)}")
    print(f"implausible debt figures cleared (< {MIN_PLAUSIBLE_DEBT:.0f}):  {len(debt_cleared)}")
    print(f"  ...whose bucket rested on that figure:      {len(bucket_now_stale)}")
    if bucket_now_stale:
        print("     (left as-is; needs the docket to re-derive, next --update will)")
        for r in bucket_now_stale:
            print(f"       {r.docket_no}  {r.town} {r.street_address}  bucket={r.lead_bucket}")

    if promoted:
        print("\n  promoted (top 10 by appraised value):")
        for r in sorted(promoted, key=lambda x: -(x.appraised_value or 0))[:10]:
            debt = _plausible_debt(r.total_debt)
            ratio = short_sale_ratio(debt, r.appraised_value, r.encumbrances_subsequent_to_lien)
            rs = f"{ratio*100:.0f}%" if ratio is not None else "debt unknown"
            print(f"    {r.town:<15} {(r.street_address or '')[:30]:<30} "
                  f"appraised ${r.appraised_value or 0:>10,.0f}  equity ratio {rs:<12} "
                  f"cont={r.continuance_count}")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    cp = Checkpoint(str(REPO / args.checkpoint_db))
    try:
        for r, _old in debt_cleared:
            r.total_debt = None
            cp.save_case_result(r)
        for r in promoted:
            r.lead_bucket = "HOT"
            cp.save_case_result(r)
    finally:
        cp.close()
    print(f"\nwrote {len(promoted)} promotions and {len(debt_cleared)} cleared debt figures")
    return 0


sys.exit(main())
