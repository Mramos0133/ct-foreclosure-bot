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
import re
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


def warn(name: str, ok: bool, detail: str = "") -> None:
    """Report a condition without failing the run.

    For environment quirks that mislead the next person but do not stop
    the work: the imports above already prove the packages are present
    for THIS interpreter, so a `pip` pointing elsewhere is a trap to
    document, not a reason to refuse to scrape.
    """
    print(f"  {'PASS' if ok else 'WARN'}  {name}{(' -- ' + detail) if detail and not ok else ''}")


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

    # 6. Runtime dependencies. A rebuilt container comes back WITHOUT the
    #    pip packages or the tesseract binary, and the failure is silent
    #    from outside: the supervisor just logs legs that make no progress.
    import importlib, shutil
    # Import the submodule that actually pulls in the compiled extension,
    # not just the top-level package. A stale Pillow built for another
    # Python version satisfies `import PIL` and then raises on
    # `from PIL import Image`, so checking the package alone reported a
    # confident PASS for a Pillow that could not decode anything
    # (2026-10-26). Same shape of risk for pymupdf and playwright.
    for mod, probe in (
        ("playwright", "playwright.sync_api"),
        ("bs4", "bs4"),
        ("openpyxl", "openpyxl.workbook"),
        ("pymupdf", "pymupdf"),
        ("pytesseract", "pytesseract"),
        ("PIL", "PIL.Image"),
    ):
        try:
            importlib.import_module(probe)
            check(f"python dep: {mod}", True)
        except Exception as exc:
            check(f"python dep: {mod}", False,
                  f"{type(exc).__name__}: {exc} -- python3 -m pip install -r requirements.txt "
                  "(bare `pip` may target a different interpreter)")

    # The interpreter running this must be the one the packages landed in.
    # In a rebuilt container `pip` is 3.13's while `python3` is 3.11, so a
    # clean-looking install leaves every import failing.
    import sys as _sys
    pip_py = shutil.which("pip")
    if pip_py:
        import subprocess as _sp
        out = _sp.run([pip_py, "--version"], capture_output=True, text=True).stdout
        m = re.search(r"\(python (\d+\.\d+)\)", out)
        ours = f"{_sys.version_info.major}.{_sys.version_info.minor}"
        warn("bare `pip` targets this interpreter",
             m is None or m.group(1) == ours,
             f"pip is for python {m.group(1) if m else '?'} but this is {ours} -- "
             "always use `python3 -m pip`, a bare `pip install` will appear to "
             "succeed and install nothing this interpreter can import")
    check("tesseract binary on PATH", shutil.which("tesseract") is not None,
          "apt-get install -y tesseract-ocr -- OCR silently fails without it")
    check("chromium present", Path("/opt/pw-browsers/chromium").exists(),
          "set CT_BOT_CHROMIUM_PATH or reinstall browsers")

    # 7. The checkpoint must carry the resumability tables; without them a
    #    long run restarts from zero on every interruption.
    try:
        con = sqlite3.connect(f"file:{REPO / 'statewide_checkpoint.sqlite3'}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        need = {"recheck_progress", "completed_towns", "processed_dockets", "case_results"}
        check("checkpoint has resumability tables", need <= tables, str(sorted(need - tables)))
    except Exception as exc:  # noqa: BLE001
        check("checkpoint has resumability tables", False, str(exc)[:160])

    # 8. The complaint extraction patterns. These are pure regex over fixed
    #    fixtures -- no network, fast -- and they silently degrade rather
    #    than crash when wrong, which is exactly how the old P&I-unpaid-since
    #    patterns sat at an 8% hit rate unnoticed.
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "selftest_complaint_patterns.py")],
                       capture_output=True, text=True, timeout=120)
    failing = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("FAIL")]
    check("complaint extraction patterns", r.returncode == 0, "; ".join(failing)[:200])

    # 9. Bucket priority and the debt plausibility floor. Same silent-failure
    #    shape: a wrong answer is a plausible-looking bucket, not an error.
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "selftest_ranking_rules.py")],
                       capture_output=True, text=True, timeout=120)
    failing = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("FAIL")]
    check("bucket priority and debt floor", r.returncode == 0, "; ".join(failing)[:200])

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall checks passed")
    return 1 if FAILURES else 0


sys.exit(main())
