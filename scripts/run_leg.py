"""Run ONE bounded leg of the statewide update in the FOREGROUND.

Why this exists, and why it is not just supervise_update.py again:

This container suspends roughly five minutes after the agent's turn ends.
Anything started with `setsid nohup ... &` is killed with it. Measured
twice on the same day: a supervisor launched at 21:21 stopped at 21:27,
one launched at 21:52 stopped at 21:59, and the run then sat dead for
1h53m. Background supervision cannot work here -- the supervisor is just
one more process that dies.

What DOES keep the container alive is an in-flight foreground tool call.
So the unit of work is a blocking leg with a wall-clock budget slightly
under the caller's timeout: the agent (or an hourly Routine) calls this
repeatedly, and every call is a self-contained increment that leaves the
checkpoint committed and pushed. Killing it at any moment costs at most
the current case, because resumability lives in the checkpoint tables
(completed_towns, processed_dockets, recheck_progress), not in memory.

Exit codes let the caller decide whether to keep going:
  0 -- the whole update is finished (169 towns walked, every case rechecked)
  2 -- progress made or attempted, more work remains; call again
  1 -- something failed that the caller should look at

  python3 scripts/run_leg.py --budget 540
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkpoint_autosave import commit_snapshot

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "statewide_checkpoint.sqlite3"
DB_REL = "statewide_checkpoint.sqlite3"
BRANCH = "claude/ct-foreclosure-scraper-3dmm7j"
TOTAL_TOWNS = 169


def counts(db_path) -> tuple[int, int, int]:
    """(towns, cases, rechecked) -- (-1, -1, -1) if unreadable."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return (
                con.execute("SELECT COUNT(*) FROM completed_towns").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM case_results").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM recheck_progress").fetchone()[0],
            )
        finally:
            con.close()
    except sqlite3.Error:
        return -1, -1, -1


def reconcile() -> tuple[int, int, int]:
    """Keep whichever of local/remote checkpoint is further along.

    After a container revert the remote is ahead; after an ordinary crash
    the local file is. Always resetting would throw away the tail of the
    local run, so compare before choosing.
    """
    subprocess.run(["git", "fetch", "origin", BRANCH], cwd=REPO, capture_output=True)
    lt, lc, ln = counts(DB)

    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    # Binary blob: must not be text-decoded, a sqlite file is not utf-8.
    raw = subprocess.run(["git", "show", f"origin/{BRANCH}:{DB_REL}"],
                         cwd=REPO, capture_output=True)
    Path(tmp).write_bytes(raw.stdout)
    rt, rc, rn = counts(tmp)
    Path(tmp).unlink(missing_ok=True)

    print(f"  local={lt}t/{lc}c/{ln}r   remote={rt}t/{rc}c/{rn}r", flush=True)
    # Rank by total work done, so neither phase can be silently rolled back.
    if (rn, rt, rc) > (ln, lt, lc):
        print("  remote is ahead (container reverted) -> resetting to remote", flush=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{BRANCH}"], cwd=REPO, capture_output=True)
        return counts(DB)
    if lt < 0:
        subprocess.run(["git", "reset", "--hard", f"origin/{BRANCH}"], cwd=REPO, capture_output=True)
        return counts(DB)
    return lt, lc, ln


def save(note: str) -> None:
    try:
        saved = commit_snapshot(DB_REL, note)
        print(f"  checkpoint: {'pushed' if saved else 'no change'}", flush=True)
    except Exception as exc:  # noqa: BLE001 -- a failed push must not lose the leg's work
        print(f"  checkpoint push FAILED: {exc}", flush=True)
    else:
        if saved:
            _sync_worktree()


def _sync_worktree() -> None:
    """Leave the working tree clean after committing.

    commit_snapshot() stages a backup-API *snapshot* blob via git plumbing
    rather than the live file, so the two are byte-different (page layout,
    freelist) even when every row matches. The tracked file therefore reads
    as permanently modified, which buries any real uncommitted change in
    noise. The scraper has already exited by the time this runs, so there is
    no writer to race -- but verify row counts match before overwriting, and
    leave the file alone if they do not, since the live file would then hold
    work the commit does not.
    """
    try:
        raw = subprocess.run(["git", "show", f"HEAD:{DB_REL}"], cwd=REPO, capture_output=True)
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        Path(tmp).write_bytes(raw.stdout)
        same = counts(tmp) == counts(DB)
        Path(tmp).unlink(missing_ok=True)
        if same:
            subprocess.run(["git", "checkout", "--", DB_REL], cwd=REPO, capture_output=True)
        else:
            print("  note: live checkpoint differs from commit -- leaving worktree as is", flush=True)
    except Exception as exc:  # noqa: BLE001 -- cosmetic; never fail a leg over it
        print(f"  worktree sync skipped: {exc}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--budget", type=int, default=540,
                   help="Wall-clock seconds for the scraping leg. Keep this UNDER the "
                        "caller's own timeout so the leg exits cleanly and commits its "
                        "work instead of being killed mid-write.")
    args = p.parse_args()

    started = time.time()
    towns, cases, rechecked = reconcile()
    if towns >= TOTAL_TOWNS and cases > 0 and rechecked >= cases:
        print(f"COMPLETE: {towns}/{TOTAL_TOWNS} towns, {rechecked}/{cases} rechecked", flush=True)
        return 0

    cmd = [sys.executable, str(REPO / "scripts" / "resume_update.py")]
    if towns >= TOTAL_TOWNS:
        cmd.append("--skip-phase1")
    else:
        print(f"  phase 1 incomplete ({towns}/{TOTAL_TOWNS} towns) -- discovery first", flush=True)

    # Skip the xlsx export on every leg: it rewrites the whole workbook and
    # only the final leg's copy matters.
    cmd += ["--export-xlsx", ""]

    env = dict(os.environ, CT_BOT_CHROMIUM_PATH="/opt/pw-browsers/chromium")
    (REPO / "runlogs").mkdir(exist_ok=True)
    log_path = REPO / "runlogs" / "leg_current.log"
    with open(log_path, "w") as lg:
        try:
            subprocess.run(cmd, cwd=REPO, env=env, stdout=lg,
                           stderr=subprocess.STDOUT, timeout=args.budget)
            print("  leg exited on its own", flush=True)
        except subprocess.TimeoutExpired:
            print(f"  leg hit its {args.budget}s budget -- stopping cleanly", flush=True)

    t2, c2, n2 = counts(DB)
    save(f"{c2} cases, {t2}/{TOTAL_TOWNS} towns, {n2} rechecked")
    elapsed = int(time.time() - started)
    print(f"  {towns}t/{cases}c/{rechecked}r -> {t2}t/{c2}c/{n2}r  in {elapsed}s", flush=True)

    if t2 >= TOTAL_TOWNS and c2 > 0 and n2 >= c2:
        print("COMPLETE", flush=True)
        return 0
    if (t2, c2, n2) == (towns, cases, rechecked):
        print("  WARNING: no progress this leg", flush=True)
        print(f"  last log lines:\n{_tail(log_path)}", flush=True)
    return 2


def _tail(path: Path, n: int = 12) -> str:
    try:
        return "\n".join("    " + l for l in path.read_text().splitlines()[-n:])
    except OSError:
        return "    (no log)"


sys.exit(main())
