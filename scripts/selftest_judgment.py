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

    # --- key-date extraction, against VERBATIM lines from real Orders
    #
    # Every string below was read off an actual filing on 2026-11-13 via
    # scripts/sample_order_text.py; the docket number is given so the
    # evidence is traceable rather than taken on trust. The first four
    # were silently unmatched before the connector list was widened,
    # which is what left Current Law Day populated on 11 of 2945 cases.
    from ct_foreclosure_bot.order_document import extract_key_date, is_order_granted
    real_orders = [
        ("WWM-CV-24-6031107-S",
         "LAW DAY SET FOR Monday, November 16, 2026, for the owner of the equity of "
         "redemption, and\nsubsequent days for subsequent encumbrancers",
         date(2026, 11, 16), "Law Day"),
        ("FST-CV-23-6060335-S",
         "LAW DAY SET FOR Tuesday, December 01, 2026, for the owner of the equity",
         date(2026, 12, 1), "Law Day"),
        ("HHD-CV-25-6213476-S",
         "The clerk will set the new Sale Date to 1/23/2027. The Committee is instructed "
         "to incur no additional\nfees and costs prior to 12/9/26.",
         date(2027, 1, 23), "Sale Date"),
        ("HHD-CV-25-6214629-S",
         "The clerk will set the new Sale Date of March 6, 2027. The Committee is "
         "instructed to incur no\nadditional fees and costs prior to 01/20/26.",
         date(2027, 3, 6), "Sale Date"),
        ("UWY-CV-25-6088378-S",
         "A new sale date of 8/29/2026 is ordered. The sale shall be conducted",
         date(2026, 8, 29), "Sale Date"),
        # These already worked; pinned so widening never breaks them.
        ("HHD-CV-25-6204270-S",
         "Title Fee: $225.00\nLaw Day: 10/13/2026, for the owner of equity of redemption",
         date(2026, 10, 13), "Law Day"),
        ("HHD-CV-25-6208063-S",
         "Law Day: 01/05/26, for the owner of equity of redemption",
         date(2026, 1, 5), "Law Day"),
        ("HHD-CV-26-6217313-S",
         "Per the oral record the Law Day is 08/17/26, for the owner of equity",
         date(2026, 8, 17), "Law Day"),
        ("HHD-CV-18-6102185-S",
         "MOTION TO OPEN JUDGMENT AND EXTEND THE SALE DATE IS GRANTED.\n"
         "Sale Date: SATURDAY, 10/28/2023 @ Noon",
         date(2023, 10, 28), "Sale Date"),
        ("HHD-CV-25-6210279-S",
         "ORDER REGARDING:\n07/27/2026 138.00 MOTION TO RESET LAW DAYS AFTER FILING OF A "
         "BANKRUPTCY\nPETITION (CGS 49-15)\nNew Sale Date: Saturday, 01/09/2027 at 12:00 noon",
         date(2027, 1, 9), "Sale Date"),
    ]
    for docket_no, text, want_date, want_label in real_orders:
        got_date, got_label = extract_key_date(text)
        check(f"real order {docket_no} -> {want_label} {want_date}",
              (got_date, got_label) == (want_date, want_label),
              f"got {(got_date, got_label)}")

    # The tight gap is the whole safety property: an unrelated date later
    # in the sentence must NOT be read as the key date. This is the
    # committee-fee deadline from HHD-CV-24-6184277-S's real Order.
    fee_trap = ("The clerk will set the new Sale Date of February 27, 2027. The Committee "
                "is instructed to incur no\nadditional fees and costs prior to 01/13/27. "
                "A new appraisal is required for the new sale date, but the\nrequirement")
    check("the operative date wins over a later fee deadline",
          extract_key_date(fee_trap) == (date(2027, 2, 27), "Sale Date"),
          f"got {extract_key_date(fee_trap)}")
    check("a 'RESET LAW DAYS' motion heading alone yields no Law Day",
          extract_key_date("MOTION TO RESET LAW DAYS AFTER FILING OF A BANKRUPTCY\n"
                           "PETITION (CGS 49-15) filed 07/27/2026") == (None, None))
    check("a dateless order stays blank rather than guessing",
          extract_key_date("ORDER REGARDING:\n07/02/2026 123.00 MOTION TO OPEN JUDGMENT\n"
                           "ORDER: GRANTED") == (None, None))

    # --- the explicit disposition line outranks the word "granted"
    off_order = ("ORDER REGARDING:\n01/06/2026 111.00 MOTION FOR JUDGMENT-STRICT FORECLOSURE\n"
                 "The foregoing, having been considered by the Court, is hereby:\n"
                 "ORDER: OFF\nPetition to participate in mediation granted by the Court.")
    check("DBD-CV-25-6056466-S 'ORDER: OFF' is not a granted judgment",
          is_order_granted(off_order) is False)
    check("'ORDER: GRANTED' is granted",
          is_order_granted("The foregoing is hereby:\nORDER: GRANTED\nDisposition: JOGSF") is True)
    check("'ORDER: DENIED' is not granted",
          is_order_granted("ORDER: DENIED\nThe motion is denied.") is False)
    check("granted judgment with no disposition line still reads as granted",
          is_order_granted("MOTION FOR STRICT FORECLOSURE IS GRANTED\nLaw Day: 10/13/2026") is True)
    check("'hereby entered' template still reads as granted",
          is_order_granted("Judgment of Foreclosure by Sale is hereby entered as follows:") is True)

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
