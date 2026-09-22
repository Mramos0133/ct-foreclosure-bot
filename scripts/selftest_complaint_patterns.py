"""Regression guard for the complaint P&I-unpaid-since patterns.

Every fixture below is real OCR text from a CT foreclosure complaint
(collected with scripts/sample_complaints.py), including the exact
tesseract mangles seen in the wild. The patterns were previously written
from imagination and hit 8% of the complaints they ran on; these lock in
the phrasings that actually occur so the next edit cannot quietly undo it.

The negative cases matter as much as the positives. Many complaints state
no default date at all -- they plead "in default for nonpayment of monthly
installments of principal and interest" and stop -- and the same paragraph
usually continues "...and the Plaintiff has exercised its option to
accelerate the balance due on said Note". A pattern loose enough to reach
across that comma will bind "due" to the acceleration clause and invent a
date. Blank is correct there; the row still carries complaint_doc_url.

  python3 scripts/selftest_complaint_patterns.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.complaint_document import extract_unpaid_since

TODAY = date(2026, 9, 22)

# (name, text, expected)
CASES = [
    # --- real phrasings that must extract -------------------------------
    ("and-conjunction",
     "5. The installment of principal and interest that was due on December 1, 2025, "
     "and each and every month thereafter, has not been paid.", "2025-12-01"),
    ("no-and-conjunction",          # over half of real complaints drop "and"
     "7. The installment of principal interest due on September 1, 2024, and each and "
     "every month thereafter, remains unpaid.", "2024-09-01"),
    ("ocr-pipe-for-one",            # "February |, 2026"
     "5. The installment of principal interest due on February |, 2026, and each and "
     "every month thereafter,", "2026-02-01"),
    ("ocr-spaced-slashes",          # "07/01/ 2025"
     "5. The installment of principal interest due on 07/01/ 2025, and each and every "
     "month thereafter,", "2025-07-01"),
    ("two-digit-day",
     "The installment of principal interest due on October 8, 2025, and each and every "
     "month thereafter,", "2025-10-08"),
    ("abbreviated-month-sept",
     "The installment of principal interest due on Sept 1, 2025, and each and every "
     "month thereafter,", "2025-09-01"),
    ("in-default-since",
     "Said Note is in default since January 1, 2026, and remains so.", "2026-01-01"),

    # --- must NOT extract -----------------------------------------------
    ("no-date-stated",
     "9. The Defendant(s), Mario Posillico, is in default for nonpayment of monthly\n"
     "installments of principal and interest, and the Plaintiff has exercised its "
     "option to accelerate the\nbalance due on the Note, to declare the Note due and "
     "payable in full.", None),
    ("acceleration-clause-decoy",
     "5. Said Note is in default due to non-payment of monthly installments of principal "
     "and interest,\nand the Plaintiff, MidFirst Bank has elected to accelerate the "
     "balance due on said Note on March 1, 2026.", None),
    ("death-of-borrower",           # reverse mortgage: no P&I date exists
     "7. Said Note is in default due to death of the borrower(s), and the Plaintiff, "
     "Longbridge Financial, LLC has elected to accelerate the balance due on said Note.", None),
    ("note-description-no-date",
     "with interest from said date, in monthly installments of principal and interest.", None),
    ("future-date-is-a-misparse",
     "The installment of principal interest due on December 1, 2099, and each and every "
     "month thereafter,", None),
    ("ancient-date-is-a-misparse",
     "The installment of principal interest due on March 1, 1901, and each and every "
     "month thereafter,", None),
]


def main() -> int:
    failures = []
    for name, text, expected in CASES:
        got = extract_unpaid_since(text, today=TODAY)
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else f' -- expected {expected!r}, got {got!r}'}")
        if not ok:
            failures.append(name)

    print(f"\n{len(failures)} failure(s)" if failures else "\nall complaint patterns pass")
    return 1 if failures else 0


sys.exit(main())
