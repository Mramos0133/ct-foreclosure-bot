"""Begin a NEW statewide update pass: clear progress markers, then commit.

Why the commit is not optional: run_leg.py reconciles the local checkpoint
against the remote before every leg and keeps whichever is further along,
which is what makes a container revert survivable. A freshly reset local
checkpoint looks exactly like a reverted one -- 0 towns, 0 rechecked --
so an uncommitted reset gets "restored" from the remote on the very next
leg and the new pass never starts. Committing the reset makes it the
agreed state of truth on both sides.

Clears only the progress bookkeeping. case_results is deliberately KEPT:
phase 1 re-walks every town to find new filings, and phase 2 rechecks the
cases already on record -- both need the existing rows. processed_dockets
is kept too, since per-docket dedup is what makes the re-walk cheap.

Record the commit this prints as the export baseline: it is the state the
new run's green (new) / yellow (updated) highlighting is measured against.

  python3 scripts/start_fresh_update.py
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkpoint_autosave import commit_snapshot

REPO = Path(__file__).resolve().parent.parent
DB_REL = "statewide_checkpoint.sqlite3"


def counts(con) -> tuple[int, int, int]:
    return (
        con.execute("SELECT COUNT(*) FROM completed_towns").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM case_results").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM recheck_progress").fetchone()[0],
    )


def main() -> int:
    baseline = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=REPO, capture_output=True, text=True).stdout.strip()

    con = sqlite3.connect(REPO / DB_REL)
    try:
        print(f"before: {counts(con)}  (towns, cases, rechecked)")
        con.execute("DELETE FROM completed_towns")
        con.execute("DELETE FROM recheck_progress")
        con.commit()
        after = counts(con)
        print(f"after:  {after}")
    finally:
        con.close()

    if after[0] or after[2]:
        print("reset did not take effect", file=sys.stderr)
        return 1

    try:
        commit_snapshot(DB_REL, f"start fresh update pass (baseline {baseline})")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to commit the reset: {exc}", file=sys.stderr)
        print("Do not start legs -- the next reconcile would undo this reset.", file=sys.stderr)
        return 1

    print(f"\nEXPORT BASELINE: {baseline}")
    print(f"  python3 scripts/export_final.py --baseline {baseline} --output statewide_leads.xlsx")
    print("Now drive the pass with repeated: python3 scripts/run_leg.py --budget 540")
    return 0


sys.exit(main())
