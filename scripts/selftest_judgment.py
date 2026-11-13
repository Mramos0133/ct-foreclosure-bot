"""Regression guard for the judgment / law day / title columns.

These are silent-failure shaped the same way the bucket rules are: a
wrong answer is a plausible-looking string, not an error. Title Vested
Status is the one that can do real damage -- it is what tells Matt an
owner is no longer worth approaching -- so the hedged cases are pinned
here deliberately.

  python3 scripts/selftest_judgment.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.judgment import (
    classify_material_event, current_law_day, extract_judgment_type,
    judgment_date_from_entry, last_material_event, title_vested_status,
)
from ct_foreclosure_bot.models import DocketEntry

FAILURES: list[str] = []
TODAY = date(2026, 11, 13)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def entry(no, file_date, description):
    return DocketEntry(entry_no=no, file_date=file_date, filed_by="",
                       description=description, arguable="")


def main() -> int:
    print("judgment column self-test")

    # --- judgment type: Order text wins, date label is the fallback
    check("order text names strict foreclosure",
          extract_judgment_type("MOTION FOR STRICT FORECLOSURE IS GRANTED", None)
          == "Strict Foreclosure")
    check("order text names foreclosure by sale",
          extract_judgment_type("JUDGMENT OF FORECLOSURE BY SALE", None)
          == "Foreclosure by Sale")
    check("falls back to Law Day label",
          extract_judgment_type(None, "Law Day") == "Strict Foreclosure")
    check("falls back to Sale Date label",
          extract_judgment_type(None, "Sale Date") == "Foreclosure by Sale")
    check("no order text and no label -> unknown, not a guess",
          extract_judgment_type(None, None) is None)

    # --- current law day belongs only to strict foreclosures
    check("law day carried on a strict foreclosure",
          current_law_day("2026-12-01", "Strict Foreclosure") == "2026-12-01")
    check("a sale date is NOT reported as a law day",
          current_law_day("2026-12-01", "Foreclosure by Sale") is None,
          str(current_law_day("2026-12-01", "Foreclosure by Sale")))

    # --- judgment date is the Order entry's own file date
    check("judgment date read off the order entry",
          judgment_date_from_entry(entry("105.00", "03/14/2026", "ORDER")) == "2026-03-14")
    check("no order entry -> no judgment date",
          judgment_date_from_entry(None) is None)

    # --- title status. The hedges matter more than the happy paths.
    future = title_vested_status("Strict Foreclosure", "2026-12-01", TODAY, None)
    check("future law day -> redemption running", "Redemption period running" in future, future)

    vested = title_vested_status("Strict Foreclosure", "2026-09-01", TODAY, None)
    check("past law day, nothing since -> likely vested", "Likely vested" in vested, vested)

    reset = title_vested_status("Strict Foreclosure", "2026-09-01", TODAY, "2026-10-15")
    check("past law day WITH later activity -> flagged as likely reset",
          "likely reset" in reset, reset)
    check("  ...and does NOT claim vested", "Likely vested" not in reset, reset)

    sale_past = title_vested_status("Foreclosure by Sale", "2026-09-01", TODAY, None)
    check("past sale date does NOT claim title vested",
          "vested" not in sale_past.lower(), sale_past)
    check("  ...and points at confirmation instead", "confirmation" in sale_past, sale_past)

    sale_future = title_vested_status("Foreclosure by Sale", "2026-12-01", TODAY, None)
    check("future sale date -> owner still in title",
          "Owner still in title" in sale_future, sale_future)

    for jt, kd in (("Strict Foreclosure", None), (None, "2026-12-01"), (None, None)):
        s = title_vested_status(jt, kd, TODAY, None)
        check(f"unknown inputs stay unknown (type={jt}, date={kd})",
              s.startswith("Unknown"), s)

    # --- material events
    cases = [
        ("MOTION TO OPEN JUDGMENT AFTER BANKRUPTCY", "Judgment reopened after bankruptcy"),
        ("MOTION TO EXTEND SALE DATE", "Sale date / Law Day extended"),
        ("NOTICE OF BANKRUPTCY STAY", "Bankruptcy"),
        ("MOTION FOR DEFAULT FOR FAILURE TO APPEAR", "Default for failure to appear"),
        ("COMPLAINT", "Complaint filed"),
    ]
    for text, want in cases:
        got = classify_material_event(entry("1", "01/01/2026", text))
        check(f"event: {text[:38]!r} -> {want}", got == want, f"got {got!r}")
    check("routine clerical entry is not material",
          classify_material_event(entry("1", "01/01/2026", "CERTIFICATE OF CLOSED PLEADINGS")) is None)

    # --- last material event picks the latest BY DATE, not by entry number
    entries = [
        entry("200.00", "02/01/2026", "MOTION TO EXTEND SALE DATE"),
        entry("100.00", "08/20/2026", "NOTICE OF BANKRUPTCY"),   # lower number, later date
        entry("300.00", "01/05/2026", "COMPLAINT"),
    ]
    label, when = last_material_event(entries)
    check("last material event is the latest by date", (label, when) == ("Bankruptcy", "2026-08-20"),
          f"got {(label, when)}")
    check("no material entries -> empty, not a guess",
          last_material_event([entry("1", "01/01/2026", "CERTIFICATE OF CLOSED PLEADINGS")])
          == ("", None))
    check("empty docket -> empty", last_material_event([]) == ("", None))

    # --- the export carries all five columns
    from ct_foreclosure_bot.excel_export import COLUMNS
    headers = [h for h, _ in COLUMNS]
    for want in ("Judgment Type", "Judgment Date", "Current Law Day",
                 "Title Vested Status", "Last Material Court Event",
                 "Last Material Event Date"):
        check(f"column present: {want}", want in headers)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall judgment rules pass")
    return 1 if FAILURES else 0


sys.exit(main())
