"""Drive phase 2 to completion across crashes and container reverts.

Each leg of the recheck dies for one of two reasons in this environment:
a transient browser/network failure, or a container revert that resets
the working tree to an older commit. Both look the same from outside --
the process is gone and work stops until someone restarts it. This loops
until every case is rechecked, so the run finishes without needing a
human to notice each failure.

The revert case needs care: after one, the local checkpoint is stale and
must be restored from the remote. But after a plain crash the LOCAL file
is the more advanced one, and blindly resetting would throw away up to
one autosave interval of work. So before each leg this compares the
local and remote recheck counts and keeps whichever is further along.

  python3 scripts/supervise_update.py --max-legs 20
"""
import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "statewide_checkpoint.sqlite3"
BRANCH = "claude/ct-foreclosure-scraper-3dmm7j"
TOTAL_TOWNS = 169


def sh(*args, check=False, **kw):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=check, **kw)


def towns_done(db_path) -> int:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM completed_towns").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return -1


def counts(db_path) -> tuple[int, int]:
    """(rechecked, total) -- (-1, -1) if the DB predates recheck_progress."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM recheck_progress").fetchone()[0]
            t = con.execute("SELECT COUNT(*) FROM case_results").fetchone()[0]
            return n, t
        finally:
            con.close()
    except sqlite3.Error:
        return -1, -1


def ensure_best_checkpoint() -> tuple[int, int]:
    """Keep whichever of local/remote is further along -- see module docstring."""
    sh("git", "fetch", "origin", BRANCH)
    local_n, local_t = counts(DB)

    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    # Binary blob -- must be read without text decoding (sh() decodes as
    # utf-8 and a sqlite file is not valid utf-8).
    raw = subprocess.run(["git", "show", f"origin/{BRANCH}:statewide_checkpoint.sqlite3"],
                         cwd=REPO, capture_output=True)
    Path(tmp).write_bytes(raw.stdout)
    remote_n, remote_t = counts(tmp)
    Path(tmp).unlink(missing_ok=True)

    print(f"  local={local_n}/{local_t}  remote={remote_n}/{remote_t}", flush=True)
    if remote_n > local_n:
        print("  remote is ahead (container reverted) -> resetting to remote", flush=True)
        sh("git", "reset", "--hard", f"origin/{BRANCH}")
        return counts(DB)
    if local_n < 0:
        sh("git", "reset", "--hard", f"origin/{BRANCH}")
        return counts(DB)
    return local_n, local_t


def commit_progress(note: str) -> None:
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "checkpoint_autosave.py"),
                        "--once"],
                       cwd=REPO, capture_output=True, text=True, timeout=180)
    print("  " + (r.stdout.strip().splitlines() or ["autosave: no output"])[-1], flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-legs", type=int, default=25)
    p.add_argument("--leg-timeout", type=int, default=5400, help="Seconds before a stuck leg is killed.")
    args = p.parse_args()

    env = dict(os.environ, CT_BOT_CHROMIUM_PATH="/opt/pw-browsers/chromium")
    for leg in range(1, args.max_legs + 1):
        print(f"=== leg {leg} ===", flush=True)
        n, t = ensure_best_checkpoint()
        if t > 0 and n >= t and towns_done(DB) >= TOTAL_TOWNS:
            print(f"COMPLETE: {towns_done(DB)}/{TOTAL_TOWNS} towns, {n}/{t} rechecked", flush=True)
            return 0

        autosave = subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "checkpoint_autosave.py"),
             "--interval", "240", "--watch-process", "resume_update"],
            cwd=REPO, stdout=open(REPO / "runlogs" / f"autosave_leg{leg}.log", "w"),
            stderr=subprocess.STDOUT,
        )
        # Discovery first: only skip phase 1 once every town has been
        # walked. A fresh run needs it; a resumed one usually does not.
        cmd = [sys.executable, str(REPO / "scripts" / "resume_update.py")]
        done = towns_done(DB)
        if done >= TOTAL_TOWNS:
            cmd.append("--skip-phase1")
        else:
            print(f"  phase 1 incomplete ({done}/{TOTAL_TOWNS} towns) -- running discovery too", flush=True)
        with open(REPO / "runlogs" / f"leg{leg}.log", "w") as lg:
            try:
                subprocess.run(cmd, cwd=REPO, env=env,
                               stdout=lg, stderr=subprocess.STDOUT, timeout=args.leg_timeout)
            except subprocess.TimeoutExpired:
                print("  leg timed out -- moving on", flush=True)
        try:
            autosave.send_signal(signal.SIGTERM)
            autosave.wait(timeout=20)
        except Exception:  # noqa: BLE001
            autosave.kill()

        commit_progress(f"leg {leg}")
        n2, t2 = counts(DB)
        print(f"  after leg {leg}: {n2}/{t2}", flush=True)
        if t2 > 0 and n2 >= t2 and towns_done(DB) >= TOTAL_TOWNS:
            print("COMPLETE", flush=True)
            return 0
        if n2 <= n:
            print("  no progress this leg -- backing off 30s", flush=True)
            time.sleep(30)

    print("max legs reached without completing", flush=True)
    return 1


sys.exit(main())
