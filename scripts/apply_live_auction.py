"""Write the auction site's current pending-sale facts into the checkpoint.

scripts/fetch_live_sales.py reads the listing; this persists it. Two fields,
both of which the checkpoint can only ever hold a scrape-time guess for:

  - key_date / key_date_label: a stored key_date came from the Order
    document when the case was last scraped, so it is blank for any case
    posted for sale afterwards and wrong for any sale that moved.
  - on_auction_site: whether the property is publicly posted for sale.

Deliberately does NOT touch lead_bucket. on_auction_site is an input to
HOT rule E ("judgment entered but not yet publicly listed"), so correcting
it changes what that rule would decide -- but a case that just got posted
for sale is a more urgent lead, not a dead one, and silently demoting it
out of HOT would hide it. The next full --update re-derives buckets from
the docket with these corrected inputs in hand; until then the buckets
stand and the corrected Key Date column carries the urgency.

  python3 scripts/fetch_live_sales.py --output /tmp/live_sales.json
  python3 scripts/apply_live_auction.py --live-auction /tmp/live_sales.json --dry-run
  python3 scripts/apply_live_auction.py --live-auction /tmp/live_sales.json
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.checkpoint import Checkpoint
from ct_foreclosure_bot.lead_ranking import days_until, hot_rule_for
from ct_foreclosure_bot.models import CaseResult

REPO = Path(__file__).resolve().parent.parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--live-auction", required=True,
                   help="JSON {docket_no: 'YYYY-MM-DD'} from fetch_live_sales.py")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    today = date.today()
    live = json.loads(Path(args.live_auction).read_text())

    con = sqlite3.connect(f"file:{REPO / args.checkpoint_db}?mode=ro", uri=True)
    records = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    filled, moved, flagged, touched = [], [], [], []
    for r in records:
        sale = live.get(r.docket_no)
        if not sale:
            continue
        changed = False
        if not r.key_date:
            filled.append((r, sale))
            changed = True
        elif r.key_date != sale:
            moved.append((r, r.key_date, sale))
            changed = True
        if not r.on_auction_site:
            flagged.append(r)
            changed = True
        if changed:
            touched.append((r, sale))

    rule_e = [r for r, _s in touched if hot_rule_for(r) == "E"]

    print(f"cases on record:                         {len(records)}")
    print(f"matched against the live listing:        {len(live)}")
    print(f"  sale date was blank, now filled:       {len(filled)}")
    print(f"  sale date was wrong, now corrected:    {len(moved)}")
    print(f"  on_auction_site was False, now True:   {len(flagged)}")
    print(f"\nHOT rule E cases among these:            {len(rule_e)}")
    print("  (rule E means 'judgment entered, NOT yet publicly listed' -- these")
    print("   ARE listed, so the rule fired on a stale input. Buckets left alone;")
    print("   the next --update re-derives them with the corrected flag.)")

    imminent = sorted(
        [(days_until(s, today), r) for r, s in touched if days_until(s, today) is not None],
        key=lambda x: (x[0], x[1].town),
    )
    soon = [x for x in imminent if 0 <= x[0] <= 7]
    if soon:
        print(f"\nposted sales within 7 days among the corrections: {len(soon)}")
        for d, r in soon[:15]:
            print(f"   {d:>2}d  {r.town:<15} {(r.street_address or '')[:32]:<32} {r.lead_bucket}")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    cp = Checkpoint(str(REPO / args.checkpoint_db))
    try:
        for r, sale in touched:
            r.key_date = sale
            r.key_date_label = "Sale Date"
            r.on_auction_site = True
            r.days_to_key_date = days_until(sale, today)
            cp.save_case_result(r)
    finally:
        cp.close()
    print(f"\nwrote {len(touched)} corrected records")
    return 0


sys.exit(main())
