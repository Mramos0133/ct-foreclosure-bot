"""Periodically commit+push the checkpoint while a long run is in flight.

This container has repeatedly reverted to an older commit mid-run, which
discards everything uncommitted -- on the last attempt that cost a full
statewide update. The checkpoint is tracked in git, so committing it
often turns a rollback from "hours of work lost" into "resume from a few
minutes ago".

Commits a CONSISTENT snapshot without touching the live file: sqlite's
backup API copies the database safely while the run is mid-write, then
git plumbing (hash-object + update-index --cacheinfo) stages that blob
directly against the tracked path. Committing the live file instead
risks capturing a torn mid-transaction state.

  python3 scripts/checkpoint_autosave.py --interval 600
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def sh(*args, check=True):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=check).stdout.strip()


def snapshot(db_path: Path) -> Path:
    """Consistent copy of the live DB via sqlite's own backup API."""
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return Path(tmp)


def commit_snapshot(db_rel: str, note: str) -> bool:
    snap = snapshot(REPO / db_rel)
    try:
        blob = sh("git", "hash-object", "-w", str(snap))
        head_blob = sh("git", "rev-parse", f"HEAD:{db_rel}", check=False)
        if blob == head_blob:
            return False  # nothing changed since last autosave
        sh("git", "update-index", "--cacheinfo", f"100644,{blob},{db_rel}")
        sh("git", "commit", "-m", f"Checkpoint autosave: {note}")
        for attempt in range(4):
            try:
                sh("git", "push", "origin", "HEAD")
                return True
            except subprocess.CalledProcessError:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        return True
    finally:
        snap.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--interval", type=int, default=600, help="Seconds between autosaves.")
    p.add_argument("--watch-process", default="ct_foreclosure_bot",
                   help="Stop once no process matches this pattern.")
    p.add_argument("--once", action="store_true",
                   help="Commit a single snapshot and exit. Use this instead of a "
                        "sentinel --watch-process value: pgrep -f matches this "
                        "process's own command line, so any sentinel makes it wait "
                        "on itself forever.")
    args = p.parse_args()

    if args.once:
        try:
            saved = commit_snapshot(args.db, "one-shot")
            print(f"{'saved' if saved else 'no change'}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"autosave FAILED: {exc}", flush=True)
        return

    print(f"autosaving {args.db} every {args.interval}s while '{args.watch_process}' runs", flush=True)
    idle = 0
    while True:
        time.sleep(args.interval)
        running = subprocess.run(["pgrep", "-f", args.watch_process],
                                 capture_output=True).returncode == 0
        try:
            con = sqlite3.connect(f"file:{REPO / args.db}?mode=ro", uri=True)
            n = con.execute("SELECT COUNT(*) FROM case_results").fetchone()[0]
            towns = con.execute("SELECT COUNT(*) FROM completed_towns").fetchone()[0]
            con.close()
            note = f"{n} cases, {towns}/169 towns"
        except Exception as exc:  # noqa: BLE001 -- never let a bad read kill the autosaver
            note = f"unreadable ({exc})"
        try:
            saved = commit_snapshot(args.db, note)
            print(f"[{time.strftime('%H:%M:%S')}] {'saved' if saved else 'no change'} -- {note}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] autosave FAILED: {exc}", flush=True)

        if not running:
            idle += 1
            if idle >= 2:
                print("run finished; final autosave done, exiting", flush=True)
                return
        else:
            idle = 0


if __name__ == "__main__":
    main()
