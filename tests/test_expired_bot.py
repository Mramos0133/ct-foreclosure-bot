"""Tests for the expired-listing bot's offline logic.

Scope note: the three network-facing modules (alerts, mls, assessor) are
not covered here, because their correctness is a question about somebody
else's live HTML and a fixture would only test my guess at it. What IS
covered is everything that decides what reaches the vendor -- name
splitting, scoring, the review/vendor split, and the append-only
workbook contract -- which is where a silent error actually costs money.

Run with:  python -m unittest discover -s tests -v
"""

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ct_expired_bot.models import (
    NA,
    NEEDS_MANUAL_REVIEW,
    PORTAL_UNAVAILABLE,
    AlertListing,
    Lead,
    MlsDetail,
    OwnerRecord,
    PriceChange,
    parse_money,
)
from ct_expired_bot.names import split_owner_name
from ct_expired_bot.scoring import addresses_match, count_price_drops, score_lead, sqft_mismatch_note
from ct_expired_bot.skiptrace import (
    HeaderSpecError,
    resolve_headers,
    write_review_csv,
    write_skiptrace_csv,
)
from ct_expired_bot import workbook as wb


def make_lead(**kwargs) -> Lead:
    alert = kwargs.pop("alert", AlertListing(mls_no="24000001", street_address="19 Abel Ave", town="Stamford", zip_code="06902", status="expired"))
    mls = kwargs.pop("mls", MlsDetail(mls_no=alert.mls_no))
    owner = kwargs.pop("owner", OwnerRecord())
    return Lead(alert=alert, mls=mls, owner=owner, **kwargs)


class TestNameSplitting(unittest.TestCase):
    def test_real_vgsi_names_are_last_first(self):
        """Sampled live from Vision/Stamford 2026-08-04. Reading these as
        FIRST LAST would send every one to the vendor reversed.
        """
        for printed, expected in [
            ("CARTWRIGHT ANGELA", ("ANGELA", "CARTWRIGHT")),
            ("ALI MOHAMMED", ("MOHAMMED", "ALI")),
            ("CORREA ELIZABETH", ("ELIZABETH", "CORREA")),
            ("AGURCIA REINALDO", ("REINALDO", "AGURCIA")),
            ("MELECIO JAMIE K", ("JAMIE K", "MELECIO")),
            ("COMERFORD ELAINE P", ("ELAINE P", "COMERFORD")),
            ("CAIRO MATTHEW P", ("MATTHEW P", "CAIRO")),
        ]:
            self.assertEqual(split_owner_name(printed), expected, printed)

    def test_real_vgsi_et_al_names_are_not_split(self):
        for printed in ["GOULD RICHARD ET AL", "HLEBOGIANNIS MARIA ET AL", "GUAMAN JAIME O ET AL"]:
            first, last = split_owner_name(printed)
            self.assertEqual(first, "", printed)
            self.assertEqual(last, printed)

    def test_real_vgsi_entity_names_are_not_split(self):
        for printed in ["CAROLINA K & G LLC", "390 SAN MIGUEL LLC"]:
            first, last = split_owner_name(printed)
            self.assertEqual(first, "")
            self.assertEqual(last, printed)

    def test_first_last_order_when_source_uses_it(self):
        self.assertEqual(split_owner_name("JOHN SMITH", order="first_last"), ("JOHN", "SMITH"))
        self.assertEqual(split_owner_name("JOHN A SMITH", order="first_last"), ("JOHN A", "SMITH"))

    def test_rejects_unknown_order(self):
        with self.assertRaises(ValueError):
            split_owner_name("JOHN SMITH", order="sideways")

    def test_splits_last_comma_first_regardless_of_order(self):
        self.assertEqual(split_owner_name("SMITH, JOHN"), ("JOHN", "SMITH"))
        self.assertEqual(split_owner_name("SMITH, JOHN", order="first_last"), ("JOHN", "SMITH"))

    def test_keeps_middle_initial_with_first_name(self):
        self.assertEqual(split_owner_name("SMITH, JOHN A"), ("JOHN A", "SMITH"))

    def test_suffix_rides_with_surname(self):
        self.assertEqual(split_owner_name("SMITH JOHN JR"), ("JOHN", "SMITH JR"))
        self.assertEqual(split_owner_name("JOHN SMITH JR", order="first_last"), ("JOHN", "SMITH JR"))

    def test_hyphenated_surname_still_splits(self):
        self.assertEqual(split_owner_name("SMITH-JONES MARY"), ("MARY", "SMITH-JONES"))

    def test_entities_go_whole_into_last_name(self):
        for name in [
            "SMITH FAMILY TRUST",
            "ACME PROPERTIES LLC",
            "ESTATE OF JOHN SMITH",
            "SMITH JOHN TRUSTEE",
            "123 MAIN ST REALTY INC",
        ]:
            first, last = split_owner_name(name)
            self.assertEqual(first, "", f"{name!r} must not yield a first name")
            self.assertEqual(last, name)

    def test_multiple_owners_go_whole_into_last_name(self):
        for name in ["JOHN & MARY SMITH", "SMITH JOHN AND MARY", "SMITH, JOHN, JONES, MARY"]:
            first, last = split_owner_name(name)
            self.assertEqual(first, "")
            self.assertEqual(last, name)

    def test_ambiguous_three_token_name_is_not_split(self):
        # "Is the surname BERG or VAN DER BERG?" -- refuse rather than guess.
        first, last = split_owner_name("MARY VAN DER BERG")
        self.assertEqual(first, "")
        self.assertEqual(last, "MARY VAN DER BERG")

    def test_empty_name(self):
        self.assertEqual(split_owner_name(""), ("", ""))


class TestScoring(unittest.TestCase):
    def test_counts_only_decreases(self):
        mls = MlsDetail(mls_no="1", price_history=[
            PriceChange("01/01/2026", "$500,000", "$475,000"),   # drop
            PriceChange("02/01/2026", "$475,000", "$450,000"),   # drop
            PriceChange("03/01/2026", "$450,000", "$460,000"),   # increase
        ])
        self.assertEqual(count_price_drops(mls), 2)

    def test_reduction_and_ppsf(self):
        lead = make_lead(mls=MlsDetail(
            mls_no="1", list_price_original="$500,000", list_price_final="$450,000",
            sqft_above_grade="2,000", expiration_date="07/01/2026",
        ))
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.total_reduction_dollars, "50000")
        self.assertEqual(lead.total_reduction_pct, "10.0%")
        self.assertEqual(lead.price_per_sqft, "225.00")
        self.assertEqual(lead.days_since_expired, "34")

    def test_missing_inputs_stay_na_not_zero(self):
        lead = make_lead(mls=MlsDetail(mls_no="1", list_price_final="$450,000"))
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.price_per_sqft, NA)
        self.assertEqual(lead.total_reduction_dollars, NA)
        self.assertEqual(lead.days_since_expired, NA)

    def test_high_from_absentee(self):
        lead = make_lead(owner=OwnerRecord(mailing_address="500 Elsewhere Rd"))
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.absentee, "Yes")
        self.assertEqual(lead.lead_score, "High")

    def test_owner_occupied_is_not_absentee(self):
        lead = make_lead(owner=OwnerRecord(mailing_address="19 ABEL AVENUE"))
        lead.alert.street_address = "19 Abel Ave"
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.absentee, "No")

    def test_absentee_unknown_when_mailing_missing(self):
        lead = make_lead()
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.absentee, NA)
        self.assertNotEqual(lead.lead_score, "High")

    def test_high_from_two_price_drops(self):
        lead = make_lead(mls=MlsDetail(mls_no="1", price_history=[
            PriceChange("01/01/2026", "$500,000", "$475,000"),
            PriceChange("02/01/2026", "$475,000", "$450,000"),
        ]))
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.lead_score, "High")

    def test_high_from_dom(self):
        lead = make_lead(mls=MlsDetail(mls_no="1", cumulative_dom="180"))
        score_lead(lead, today=date(2026, 8, 4))
        self.assertEqual(lead.lead_score, "High")

    def test_medium_and_low_boundaries(self):
        medium = make_lead(mls=MlsDetail(mls_no="1", cumulative_dom="60"))
        score_lead(medium, today=date(2026, 8, 4))
        self.assertEqual(medium.lead_score, "Medium")

        upper = make_lead(mls=MlsDetail(mls_no="1", cumulative_dom="119"))
        score_lead(upper, today=date(2026, 8, 4))
        self.assertEqual(upper.lead_score, "Medium")

        low = make_lead(mls=MlsDetail(mls_no="1", cumulative_dom="59"))
        score_lead(low, today=date(2026, 8, 4))
        self.assertEqual(low.lead_score, "Low")

    def test_address_abbreviation_folding(self):
        self.assertTrue(addresses_match("123 Main Street", "123 MAIN ST"))
        self.assertTrue(addresses_match("45 North Rd", "45 N ROAD"))
        self.assertFalse(addresses_match("123 Main St", "124 Main St"))
        self.assertIsNone(addresses_match("123 Main St", NA))

    def test_sqft_mismatch_note(self):
        mls = MlsDetail(mls_no="1", sqft_above_grade="2,000")
        self.assertIsNone(sqft_mismatch_note(mls, OwnerRecord(assessor_living_area="1,900")))
        self.assertIsNotNone(sqft_mismatch_note(mls, OwnerRecord(assessor_living_area="900")))

    def test_parse_money_never_returns_zero_for_missing(self):
        self.assertIsNone(parse_money(NA))
        self.assertIsNone(parse_money(""))
        self.assertIsNone(parse_money("n/a"))
        self.assertEqual(parse_money("$1,234.50"), 1234.50)


class TestSkiptraceCsv(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_review_rows_never_reach_vendor_file(self):
        clean = make_lead(owner=OwnerRecord(owner_name="SMITH JOHN", mailing_address="19 Abel Ave"))
        flagged = make_lead(
            alert=AlertListing(mls_no="24000002", street_address="5 Oak St", town="Stamford"),
            owner=OwnerRecord(status=NEEDS_MANUAL_REVIEW, owner_name=NEEDS_MANUAL_REVIEW, notes=["two parcels matched"]),
        )
        unavailable = make_lead(
            alert=AlertListing(mls_no="24000003", street_address="7 Elm St", town="Hartford"),
            owner=OwnerRecord(status=PORTAL_UNAVAILABLE, owner_name=PORTAL_UNAVAILABLE),
        )
        leads = [clean, flagged, unavailable]

        vendor = self.dir / "skiptrace.csv"
        review = self.dir / "review.csv"
        self.assertEqual(write_skiptrace_csv(vendor, leads), 1)
        self.assertEqual(write_review_csv(review, leads), 2)

        with vendor.open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Owner First Name"], "JOHN")
        self.assertEqual(rows[0]["Owner Last Name"], "SMITH")
        self.assertEqual(rows[0]["Property State"], "CT")
        blob = vendor.read_text()
        self.assertNotIn(NEEDS_MANUAL_REVIEW, blob)
        self.assertNotIn(PORTAL_UNAVAILABLE, blob)

    def test_na_never_written_to_vendor_file(self):
        lead = make_lead(owner=OwnerRecord(owner_name="SMITH JOHN"))  # mailing fields all NA
        vendor = self.dir / "skiptrace.csv"
        write_skiptrace_csv(vendor, [lead])
        with vendor.open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Mailing Address"], "")

    def test_custom_headers_positional(self):
        headers = resolve_headers(csv_headers="F,L,Addr,City,St,Zip,MAddr,MCity,MSt,MZip")
        vendor = self.dir / "custom.csv"
        write_skiptrace_csv(vendor, [make_lead(owner=OwnerRecord(owner_name="SMITH JOHN"))], headers=headers)
        first_line = vendor.read_text().splitlines()[0]
        self.assertEqual(first_line, "F,L,Addr,City,St,Zip,MAddr,MCity,MSt,MZip")

    def test_wrong_header_count_refuses(self):
        with self.assertRaises(HeaderSpecError):
            resolve_headers(csv_headers="First,Last,Address")


class TestWorkbook(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "CT-Expired-Master.xlsx"

    def test_creates_three_tabs_with_manual_columns(self):
        self.assertTrue(wb.ensure_workbook(self.path))
        from openpyxl import load_workbook

        book = load_workbook(self.path)
        self.assertEqual(set(book.sheetnames), {"Leads", "Town Portals", "Meta"})
        headers = [c.value for c in book["Leads"][1]]
        for manual in wb.MANUAL_COLUMNS:
            self.assertIn(manual, headers)

    def test_append_preserves_manual_edits(self):
        wb.ensure_workbook(self.path)
        wb.append_leads(self.path, [make_lead(owner=OwnerRecord(owner_name="SMITH JOHN"))])

        from openpyxl import load_workbook

        book = load_workbook(self.path)
        sheet = book["Leads"]
        headers = {c.value: c.column for c in sheet[1]}
        sheet.cell(row=2, column=headers["Phone 1"], value="203-555-0100")
        sheet.cell(row=2, column=headers["My Notes"], value="left voicemail")
        book.save(self.path)

        second = make_lead(alert=AlertListing(mls_no="24000099", street_address="8 Pine St", town="Stamford"))
        wb.append_leads(self.path, [second])

        book = load_workbook(self.path)
        sheet = book["Leads"]
        self.assertEqual(sheet.cell(row=2, column=headers["Phone 1"]).value, "203-555-0100")
        self.assertEqual(sheet.cell(row=2, column=headers["My Notes"]).value, "left voicemail")
        self.assertEqual(sheet.cell(row=3, column=headers["MLS #"]).value, "24000099")
        self.assertIsNone(sheet.cell(row=3, column=headers["Phone 1"]).value)

    def test_existing_mls_numbers_drives_dedupe(self):
        wb.ensure_workbook(self.path)
        self.assertEqual(wb.existing_mls_numbers(self.path), set())
        wb.append_leads(self.path, [make_lead()])
        self.assertEqual(wb.existing_mls_numbers(self.path), {"24000001"})

    def test_append_survives_reordered_columns(self):
        wb.ensure_workbook(self.path)
        from openpyxl import load_workbook

        book = load_workbook(self.path)
        book["Leads"].insert_cols(1)
        book["Leads"].cell(row=1, column=1, value="My Own Column")
        book.save(self.path)

        wb.append_leads(self.path, [make_lead(owner=OwnerRecord(owner_name="SMITH JOHN"))])
        book = load_workbook(self.path)
        sheet = book["Leads"]
        headers = {c.value: c.column for c in sheet[1]}
        self.assertEqual(sheet.cell(row=2, column=headers["MLS #"]).value, "24000001")
        self.assertEqual(sheet.cell(row=2, column=headers["Owner Name"]).value, "SMITH JOHN")

    def test_last_run_roundtrip(self):
        wb.ensure_workbook(self.path)
        self.assertEqual(wb.read_last_run(self.path), "")
        wb.write_last_run(self.path, "2026-08-04T12:00:00+00:00")
        self.assertEqual(wb.read_last_run(self.path), "2026-08-04T12:00:00+00:00")

    def test_portals_roundtrip(self):
        from ct_expired_bot.models import TownPortal

        wb.ensure_workbook(self.path)
        wb.upsert_portals(self.path, [TownPortal("Stamford", "https://gis.vgsi.com/stamfordct/", "Vision", "ok")])
        registry = wb.read_portals(self.path)
        self.assertEqual(registry["Stamford"].platform, "Vision")
        # Upsert must update in place, not duplicate the row.
        wb.upsert_portals(self.path, [TownPortal("Stamford", "https://example.com", "Custom", "changed")])
        registry = wb.read_portals(self.path)
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry["Stamford"].platform, "Custom")


if __name__ == "__main__":
    unittest.main()
