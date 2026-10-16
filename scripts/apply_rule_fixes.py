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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.lead_ranking import (
    equity_bucket_override, is_assistance_elapsed_hot, is_bankruptcy_reopen_hot,
    short_sale_ratio,
)
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.worksheet import MIN_PLAUSIBLE_DEBT, _plausible_debt

REPO = Path(__file__).resolve().parent.parent


def _d(iso):
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def correct_hot_flags(r: CaseResult, today: date) -> dict:
    """Recompute the two HOT flags reclassify_from_docket forgot to persist.

    It rebuilt lead_bucket from a fresh decide_bucket but left
    assistance_elapsed_hot and bankruptcy_reopen_hot at whatever was
    stored, so a case could sit in HOT *because* its assistance window had
    just closed while the column reporting that read N. Both inputs are on
    the record, so the flags recompute exactly rather than being guessed.
    """
    want_elapsed = bool(
        r.assistance_state == "elapsed"
        and is_assistance_elapsed_hot(_d(r.assistance_elapsed_date), today)
    )
    want_bk = bool(
        r.bankruptcy_stay_reopened
        and is_bankruptcy_reopen_hot(_d(r.bankruptcy_filed_date), today)
    )
    out = {}
    if bool(r.assistance_elapsed_hot) != want_elapsed:
        out["assistance_elapsed_hot"] = want_elapsed
    if bool(r.bankruptcy_reopen_hot) != want_bk:
        out["bankruptcy_reopen_hot"] = want_bk
    return out


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def equity_forces(r: CaseResult, debt) -> str | None:
    return equity_bucket_override(
        short_sale_ratio(debt, r.appraised_value, r.encumbrances_subsequent_to_lien)
    )


def sale_already_held(r: CaseResult, today: date) -> bool:
    """The property's scheduled sale date is in the past, by today's clock.

    Reads key_date rather than the stored days_to_key_date, which is
    frozen at scrape time and so reports a long-past sale as days away.
    """
    d = _d(r.key_date)
    return d is not None and d < today


def stale_hot_after_sale(r: CaseResult, today: date) -> bool:
    """A HOT case that can only have been HOT by rule E, whose sale is over.

    Rule E is the one HOT rule with no persisted flag of its own, so a
    case sitting in HOT with none of the A-D flags set was put there by E
    and nothing else. E requires the sale not to have happened yet. When
    the auction date passes, the case stays HOT until the next docket read
    notices -- which on 2026-10-16 left thirteen properties on the HOT
    sheet whose auctions were up to 62 days gone.

    Every input here is persisted, so this is decidable without the
    docket, which is what keeps it inside this script's additive
    discipline: it never guesses, it only applies E's own expiry.
    """
    return bool(
        r.lead_bucket == "HOT"
        and not r.assistance_elapsed_hot
        and not r.bankruptcy_reopen_hot
        and not r.recent_complaint_hot
        and not r.assistance_program_hot
        and sale_already_held(r, today)
    )


def bucket_after_sale(r: CaseResult, debt) -> str:
    """Where decide_bucket's tail sends a case once rule E stops matching.

    Mirrors the real order: the equity override outranks everything, then
    the continuance demotion, then UNCLASSIFIED.
    """
    forced = equity_forces(r, debt)
    if forced:
        return forced
    return "COLD" if (r.continuance_count or 0) >= 2 else "UNCLASSIFIED"


def meets_judgment_hot(r: CaseResult) -> bool:
    """HOT rule E, evaluated purely from persisted fields.

    Kept in step with decide_bucket, which dropped the "not on the auction
    site" condition on 2026-10-16 -- a posted sale shortens the window
    rather than closing it, and only a sale that already happened ends it.
    """
    key_past = r.days_to_key_date is not None and r.days_to_key_date < 0
    return bool(r.judgment_granted and r.non_appearing and not key_past)


def main() -> int:
    args = parse_args()
    con = sqlite3.connect(f"file:{REPO / args.checkpoint_db}?mode=ro", uri=True)
    records = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    promoted, debt_cleared, bucket_now_stale = [], [], []
    flag_fixes, expired = [], []
    today = date.today()

    for r in records:
        fixes = correct_hot_flags(r, today)
        if fixes:
            flag_fixes.append((r, fixes))

        if stale_hot_after_sale(r, today):
            expired.append((r, bucket_after_sale(r, _plausible_debt(r.total_debt))))

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

    ef = sum(1 for _r, f in flag_fixes if "assistance_elapsed_hot" in f)
    bf = sum(1 for _r, f in flag_fixes if "bankruptcy_reopen_hot" in f)
    print(f"stale HOT flags corrected:                    {len(flag_fixes)}"
          f"  (assistance_elapsed {ef}, bankruptcy_reopen {bf})")
    print(f"promotions COLD -> HOT (reordering):           {len(promoted)}")
    print(f"HOT cases whose sale has already been held:   {len(expired)}")
    if expired:
        for r, dest in sorted(expired, key=lambda x: x[0].key_date or ""):
            print(f"       sale {r.key_date}  {r.town:<15} "
                  f"{(r.street_address or '')[:30]:<30} HOT -> {dest}")
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
        for r, fixes in flag_fixes:
            for k, v in fixes.items():
                setattr(r, k, v)
            cp.save_case_result(r)
        for r in promoted:
            r.lead_bucket = "HOT"
            cp.save_case_result(r)
        for r, dest in expired:
            r.lead_bucket = dest
            cp.save_case_result(r)
    finally:
        cp.close()
    print(f"\nwrote {len(promoted)} promotions, {len(debt_cleared)} cleared debt figures, "
          f"{len(flag_fixes)} flag corrections, {len(expired)} post-sale demotions")
    return 0


sys.exit(main())
