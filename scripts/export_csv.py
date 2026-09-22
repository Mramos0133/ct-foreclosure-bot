"""Export the lead sheets as CSV, one file per bucket.

A fallback for when the .xlsx will not come down: CSV opens in Excel
everywhere and is a fraction of the size.

CSV cannot carry cell fills, so the green(new)/yellow(updated) highlighting
that the workbook uses to prioritise calls would simply vanish. Instead the
same diff is written into two leading columns, Status and Status Detail, and
the rows are ordered new first, then updated, then unchanged -- so the
information survives the format change rather than being silently dropped.

Columns are taken from excel_export.COLUMNS so the CSV and the workbook can
never drift apart.

  python3 scripts/export_csv.py --baseline 79ca819 --outdir csv_out
"""
import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.excel_export import COLUMNS, SHEET_ORDER
from ct_foreclosure_bot.models import CaseResult
from ct_foreclosure_bot.update_run import _meaningfully_different

REPO = Path(__file__).resolve().parent.parent
DB_REL = "statewide_checkpoint.sqlite3"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True, help="git rev holding the previous checkpoint")
    p.add_argument("--outdir", default="csv_out")
    return p.parse_args()


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
    args = parse_args()
    base = load_baseline(args.baseline)

    con = sqlite3.connect(f"file:{REPO / DB_REL}?mode=ro", uri=True)
    current = [CaseResult(**json.loads(d)) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    outdir = REPO / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("*.csv"):
        f.unlink()

    def status(r):
        old = base.get(r.docket_no)
        if old is None:
            return "NEW", "New lead since baseline"
        if _meaningfully_different(old, r):
            bits = []
            if old.lead_bucket != r.lead_bucket:
                bits.append(f"bucket {old.lead_bucket} -> {r.lead_bucket}")
            if not old.complaint_unpaid_since and r.complaint_unpaid_since:
                bits.append("P&I unpaid-since added")
            if not old.judgment_granted and r.judgment_granted:
                bits.append("judgment granted")
            if not old.on_auction_site and r.on_auction_site:
                bits.append("posted for sale")
            return "UPDATED", "; ".join(bits) or "new motion / detail change"
        return "", ""

    # NEW first, then UPDATED, then the rest -- the ordering replaces the
    # colour cue the workbook uses to put actionable rows on top.
    rank = {"NEW": 0, "UPDATED": 1, "": 2}
    header = ["Status", "Status Detail"] + [name for name, _ in COLUMNS]
    totals = {}

    for bucket in SHEET_ORDER:
        rows = [r for r in current if (r.lead_bucket if r.lead_bucket in SHEET_ORDER
                                       else "UNCLASSIFIED") == bucket]
        decorated = []
        for r in rows:
            st, detail = status(r)
            decorated.append((rank[st], st, detail, r))
        decorated.sort(key=lambda t: (t[0], t[3].town or "", t[3].street_address or ""))

        path = outdir / f"{bucket}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for _, st, detail, r in decorated:
                w.writerow([st, detail] + [getter(r) for _, getter in COLUMNS])
        n_new = sum(1 for d in decorated if d[1] == "NEW")
        n_upd = sum(1 for d in decorated if d[1] == "UPDATED")
        totals[bucket] = (len(rows), n_new, n_upd)
        print(f"  {path.name}: {len(rows)} rows ({n_new} new, {n_upd} updated)")

    print(f"\ntotal rows: {sum(v[0] for v in totals.values())}")
    print("utf-8-sig encoding so Excel opens accented names correctly")
    return 0


sys.exit(main())
