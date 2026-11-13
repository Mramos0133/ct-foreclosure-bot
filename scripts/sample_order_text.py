"""Dump the raw OCR text of real Order documents, to read rather than guess.

Written to diagnose why Current Law Day was populated on only 11 of 2945
cases while 1898 carried a strict-foreclosure motion. The Law Day line is
only ever inside the Order PDF, so the phrasing variants the regexes miss
cannot be found from the checkpoint -- they have to be read off real
filings.

Takes docket numbers, walks the same path the pipeline does
(fetch_docket -> find_order_entry -> fetch_document_text), and writes each
Order's full OCR text to a file plus a short console digest showing every
line mentioning "law day" or "sale date" and what extract_key_date()
currently makes of it. Read-only: touches no checkpoint.

  python3 scripts/sample_order_text.py --out-dir /tmp/orders DOCKET [DOCKET ...]
  python3 scripts/sample_order_text.py --out-dir /tmp/orders --from-json sample_A.json
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.case_analysis import analyze_docket
from ct_foreclosure_bot.docket import fetch_docket
from ct_foreclosure_bot.order_document import (
    extract_key_date, fetch_document_text, find_order_entry,
)
from ct_foreclosure_bot.throttle import Throttle

INTEREST = re.compile(r"law\s*day|sale\s*date|redemption|strict", re.I)


async def dump_one(context, throttle, docket_no: str, out_dir: Path) -> bool:
    print(f"\n{'='*72}\n{docket_no}")
    try:
        docket = await fetch_docket(context, throttle, docket_no)
    except Exception as exc:  # noqa: BLE001
        print(f"  docket fetch FAILED: {exc!r}")
        return False
    analysis = analyze_docket(docket)
    if not analysis.matched_motion_entries:
        print("  no matched motion entries")
        return False
    anchor = analysis.matched_motion_entries[-1]
    order = find_order_entry(docket.entries, anchor)
    print(f"  motion  {anchor.entry_no:<8} {anchor.description[:90]}")
    if order is None:
        print("  NO ORDER ENTRY FOUND for that motion")
        for e in docket.entries:
            if "ORDER" in e.description.upper():
                print(f"    orderish {e.entry_no:<8} {e.description[:80]}")
        return False
    print(f"  order   {order.entry_no:<8} filed {order.file_date}  {order.description[:70]}")
    try:
        text = await fetch_document_text(context, throttle, order.document_url)
    except Exception as exc:  # noqa: BLE001
        print(f"  order OCR FAILED: {exc!r}")
        return False
    path = out_dir / f"{docket_no}.txt"
    path.write_text(text)
    parsed, label = extract_key_date(text)
    print(f"  extract_key_date -> {parsed} ({label})     [{len(text)} chars -> {path.name}]")
    hits = [ln.strip() for ln in text.splitlines() if INTEREST.search(ln)]
    if hits:
        print("  lines of interest:")
        for ln in hits[:12]:
            print(f"    | {ln}")
    else:
        print("  NO line mentions law day / sale date / redemption / strict")
    return True


async def sample(docket_nos: list[str], out_dir: Path, min_delay: float) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    throttle = Throttle(min_delay=min_delay, max_delay=min_delay + 1.0)
    ok = 0
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=True)
        context = await new_context(browser)
        try:
            for docket_no in docket_nos:
                if await dump_one(context, throttle, docket_no, out_dir):
                    ok += 1
        finally:
            await context.close()
            await browser.close()
    print(f"\n{ok}/{len(docket_nos)} orders OCR'd into {out_dir}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dockets", nargs="*")
    p.add_argument("--from-json", help="JSON file holding a list of docket numbers")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--min-delay", type=float, default=2.0,
                   help="seconds between requests; must stay >= 2.0")
    args = p.parse_args()
    if args.min_delay < 2.0:
        p.error("--min-delay must stay at or above 2.0 seconds")
    dockets = list(args.dockets)
    if args.from_json:
        dockets += json.loads(Path(args.from_json).read_text())
    if not dockets:
        p.error("give docket numbers or --from-json")
    return asyncio.run(sample(dockets, Path(args.out_dir), args.min_delay))


sys.exit(main())
