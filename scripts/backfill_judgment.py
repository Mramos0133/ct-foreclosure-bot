"""Fill the judgment columns on cases already on record, without network.

Three of the five columns added on 2026-11-13 are derivable from fields
the checkpoint already holds, so existing cases do not have to wait for a
re-scrape to show them:

  Judgment Type     from key_date_label -- a Law Day is only ever set by
                    a strict foreclosure, a Sale Date only by a sale.
  Current Law Day   from key_date on strict foreclosures.
  Title Vested      from those two plus today's date.

The other two CANNOT be derived offline and are deliberately left empty
rather than guessed:

  Judgment Date         is the file date of the Order docket entry, and
                        raw docket entries are not persisted.
  Last Material Event   needs the docket entries for the same reason.

Both fill in on the next full update, which reads every docket anyway.
Leaving them blank is the honest state: an empty cell reads as "not known
yet", a wrong date reads as fact.

  python3 scripts/backfill_judgment.py --dry-run
  python3 scripts/backfill_judgment.py
"""
import argparse
import re
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.judgment import (
    current_law_day, extract_judgment_type, title_vested_status,
)
from ct_foreclosure_bot.models import CaseResult

REPO = Path(__file__).resolve().parent.parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    today = date.today()

    con = sqlite3.connect(f"file:{REPO / args.checkpoint_db}?mode=ro", uri=True)
    records = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    changed = []
    types, titles = Counter(), Counter()
    for r in records:
        jt = r.judgment_type or extract_judgment_type(
            None, r.key_date_label, r.motion_types_found)
        law_day = current_law_day(r.key_date, jt)
        title = title_vested_status(jt, r.key_date, today, r.last_material_event_date)
        if (jt, law_day, title) != (r.judgment_type, r.current_law_day, r.title_vested_status):
            changed.append((r, jt, law_day, title))
        types[jt or "(unknown)"] += 1
        # Group by the shape of the answer, not the answer: every status
        # embeds its own date, so counting the raw strings prints one row
        # per distinct sale date instead of a usable distribution.
        titles[re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", title)] += 1

    print(f"cases on record:            {len(records)}")
    print(f"rows the backfill changes:  {len(changed)}")
    print("\njudgment type:")
    for k, n in types.most_common():
        print(f"   {k:<22} {n:>5}")
    print("\ntitle vested status:")
    for k, n in titles.most_common():
        print(f"   {k:<46} {n:>5}")

    blank_jd = sum(1 for r in records if not r.judgment_date)
    blank_ev = sum(1 for r in records if not r.last_material_event)
    print(f"\nstill blank, pending the next docket read:")
    print(f"   Judgment Date               {blank_jd:>5}")
    print(f"   Last Material Court Event   {blank_ev:>5}")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    cp = Checkpoint(str(REPO / args.checkpoint_db))
    try:
        for r, jt, law_day, title in changed:
            r.judgment_type = jt
            r.current_law_day = law_day
            r.title_vested_status = title
            cp.save_case_result(r)
    finally:
        cp.close()
    print(f"\nwrote {len(changed)} records")
    return 0


sys.exit(main())
