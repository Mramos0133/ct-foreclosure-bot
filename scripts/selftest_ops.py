"""Regression guard for the operational scripts.

Every stall in the statewide run so far came from an ops script, not the
scraper: a sqlite blob read as utf-8, and an autosaver that matched its
own command line with pgrep -f and waited on itself forever. Both were
silent -- the run simply stopped. These checks make that class of failure
loud and immediate instead.

Fast and side-effect-free: no network, no git writes, no checkpoint
mutation. Safe to run before kicking off any long job.

  python3 scripts/selftest_ops.py
"""
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' -- ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("ops self-test")

    # 1. Every script must at least import/compile.
    for f in sorted((REPO / "scripts").glob("*.py")):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(f)], capture_output=True, text=True)
        check(f"compiles: {f.name}", r.returncode == 0, r.stderr.strip()[:120])

    # 2. --once must terminate. It previously hung forever because a
    #    sentinel --watch-process value matched the process's own argv.
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "checkpoint_autosave.py"), "--once", "--db",
         "statewide_checkpoint.sqlite3"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    check("checkpoint_autosave --once terminates", r.returncode == 0, r.stderr.strip()[:160])

    # 3. No sentinel --watch-process anywhere: pgrep -f sees this process's
    #    own command line, so any sentinel deadlocks.
    bad = []
    for f in (REPO / "scripts").glob("*.py"):
        if f.name == Path(__file__).name:
            continue  # this file names the sentinels in order to detect them
        text = f.read_text()
        for sentinel in ('"__none__"', "'__none__'", "__no_such", "__nonexistent"):
            if sentinel in text and "pgrep" not in text.split(sentinel)[0][-200:]:
                if "--watch-process" in text:
                    bad.append(f"{f.name}:{sentinel}")
    check("no self-matching --watch-process sentinel", not bad, ", ".join(bad))

    # 4. The checkpoint blob must be read as binary, never text-decoded.
    sup = (REPO / "scripts" / "supervise_update.py").read_text()
    text_mode_blob = 'sh("git", "show"' in sup and "statewide_checkpoint" in sup
    check("supervisor reads checkpoint blob as binary", not text_mode_blob,
          "sh() text-decodes; use subprocess.run(capture_output=True) without text=True")

    # 5. The snapshot path must produce a readable database.
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from checkpoint_autosave import snapshot
        snap = snapshot(REPO / "statewide_checkpoint.sqlite3")
        con = sqlite3.connect(snap)
        n = con.execute("SELECT COUNT(*) FROM case_results").fetchone()[0]
        con.close()
        snap.unlink(missing_ok=True)
        check("sqlite snapshot is readable", n > 0, f"{n} cases")
    except Exception as exc:  # noqa: BLE001
        check("sqlite snapshot is readable", False, str(exc)[:160])

    # 6. The checkpoint must carry the resumability tables; without them a
    #    long run restarts from zero on every interruption.
    try:
        con = sqlite3.connect(f"file:{REPO / 'statewide_checkpoint.sqlite3'}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        need = {"recheck_progress", "completed_towns", "processed_dockets", "case_results"}
        check("checkpoint has resumability tables", need <= tables, str(sorted(need - tables)))
    except Exception as exc:  # noqa: BLE001
        check("checkpoint has resumability tables", False, str(exc)[:160])

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall checks passed")
    return 1 if FAILURES else 0


sys.exit(main())
