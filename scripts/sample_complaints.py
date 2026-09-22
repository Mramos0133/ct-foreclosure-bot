"""Dump OCR'd complaint text for cases where the P&I-unpaid-since pattern missed.

The UNPAID_SINCE_PATTERNS in complaint_document.py were written as a
best-effort guess and never confirmed against real pleadings -- the same
mistake the EMAP patterns in motions.py made. They hit on 8% of the
complaints OCR'd, while the principal patterns on the SAME text hit 87%,
so the OCR is fine and the date phrasing is simply wrong.

This samples cases where principal parsed but the date did not (proving
the text is readable), re-OCRs them, and writes the raw text so the real
boilerplate can be read directly and the patterns tuned against evidence.

  python3 scripts/sample_complaints.py --limit 25 --out /tmp/.../complaints
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.complaint_document import (
    fetch_complaint_text, extract_principal_amount, extract_unpaid_since,
)
from ct_foreclosure_bot.throttle import Throttle


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--out", required=True, help="directory for the dumped text")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--mode", choices=["missed", "hit"], default="missed",
                   help="missed: principal parsed but date did not (the gap). "
                        "hit: both parsed, useful as a control set.")
    return p.parse_args()


async def main():
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{args.checkpoint_db}?mode=ro", uri=True)
    rows = [json.loads(d) for (d,) in con.execute("SELECT data FROM case_results")]
    con.close()

    def wanted(r):
        if not (r.get("recent_complaint_hot") and r.get("complaint_doc_url")):
            return False
        has_principal = r.get("complaint_principal_amount") is not None
        has_date = bool(r.get("complaint_unpaid_since"))
        return (has_principal and not has_date) if args.mode == "missed" else (has_principal and has_date)

    sample = [r for r in rows if wanted(r)][:args.limit]
    print(f"{len(sample)} cases to sample (mode={args.mode})")

    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=True)
        try:
            context = await new_context(browser)
            for i, r in enumerate(sample, 1):
                docket = r["docket_no"]
                try:
                    text = await fetch_complaint_text(context, throttle, r["complaint_doc_url"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  {i}/{len(sample)} {docket}: FETCH FAILED {type(exc).__name__}")
                    continue
                (out / f"{docket}.txt").write_text(text)
                print(f"  {i}/{len(sample)} {docket}: {len(text)} chars, "
                      f"principal={extract_principal_amount(text)} "
                      f"date={extract_unpaid_since(text)}")
        finally:
            await browser.close()
    print(f"\nwrote {len(list(out.glob('*.txt')))} files to {out}")


asyncio.run(main())
