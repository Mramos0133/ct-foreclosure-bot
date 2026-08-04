"""Scan dockets in given towns for Foreclosure Mediation activity and dump
the raw entry text for analysis.

Why raw-dump rather than classify in place: the mediation/EMAP phrasing
patterns in motions.py were written as a best-effort guess and flagged in
CLAUDE.md as never having been confirmed against real docket text. This
collects every entry containing MEDIAT/EMAP/LOSS MITIGATION across a set
of towns so the real phrasing can be read directly, the "period expired
or terminated" variants identified for certain, and the patterns tuned
against evidence instead of assumption.

Scans every PROCESSED docket in the towns, not just matched ones -- a
case sitting in mediation often has no judgment motion yet, so restricting
to matched cases would miss exactly the population being looked for.

  python3 scripts/scan_mediation.py Milford Bridgeport Stratford \
      --output /tmp/mediation_scan.json
"""
import argparse
import asyncio
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from ct_foreclosure_bot.browser import launch_browser, new_context
from ct_foreclosure_bot.docket import fetch_docket
from ct_foreclosure_bot.motions import is_complaint_entry, normalize
from ct_foreclosure_bot.throttle import Throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("scan_mediation")

INTEREST = re.compile(r"MEDIAT|EMAP|LOSS\s+MITIGATION|LOAN\s+MODIFICATION")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("towns", nargs="+")
    p.add_argument("--checkpoint-db", default="statewide_checkpoint.sqlite3")
    p.add_argument("--output", required=True)
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()


async def main():
    args = parse_args()
    con = sqlite3.connect(args.checkpoint_db)
    try:
        dockets = [
            (d, t) for d, t in con.execute(
                "SELECT docket_no, town FROM processed_dockets WHERE town IN (%s) ORDER BY docket_no"
                % ",".join("?" * len(args.towns)), args.towns)
        ]
        stored = {}
        for (data,) in con.execute("SELECT data FROM case_results"):
            r = json.loads(data)
            if r["town"] in set(args.towns):
                stored[r["docket_no"]] = r
    finally:
        con.close()

    if args.limit:
        dockets = dockets[:args.limit]
    log.info("scanning %d dockets across %s", len(dockets), args.towns)

    out = {}
    throttle = Throttle(min_delay=2.0, max_delay=3.0)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=True)
        try:
            context = await new_context(browser)
            for i, (docket_no, town) in enumerate(dockets, 1):
                try:
                    docket = await fetch_docket(context, throttle, docket_no)
                except Exception:
                    log.exception("failed %s", docket_no)
                    continue

                hits = [
                    {"entry_no": e.entry_no, "date": e.file_date, "filed_by": e.filed_by,
                     "desc": e.description, "doc_url": e.document_url}
                    for e in docket.entries if INTEREST.search(normalize(e.description))
                ]
                complaints = [
                    {"entry_no": e.entry_no, "date": e.file_date, "desc": e.description}
                    for e in docket.entries if is_complaint_entry(e.description)
                ]
                if hits:
                    rec = stored.get(docket_no, {})
                    out[docket_no] = {
                        "town": town,
                        "caption": docket.case_caption,
                        "case_detail_url": docket.case_detail_url,
                        "street_address": rec.get("street_address", ""),
                        "zip_code": rec.get("zip_code", ""),
                        "matched_in_checkpoint": docket_no in stored,
                        "lead_bucket": rec.get("lead_bucket"),
                        "entry_count": len(docket.entries),
                        "complaints": complaints,
                        "mediation_entries": hits,
                    }
                    log.info("HIT  %s %s  (%d mediation-ish entries)", town, docket_no, len(hits))

                if i % 25 == 0:
                    log.info("progress %d/%d, %d hits so far", i, len(dockets), len(out))
                    Path(args.output).write_text(json.dumps(out, indent=1))
        finally:
            await browser.close()

    Path(args.output).write_text(json.dumps(out, indent=1))
    log.info("done: %d dockets with mediation-ish entries -> %s", len(out), args.output)


asyncio.run(main())
