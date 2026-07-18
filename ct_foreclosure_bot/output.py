"""Incremental CSV output.

Rows are appended and flushed as each matching case finishes, rather than
buffered in memory and written once at the end -- so a run that gets
interrupted still leaves a usable, complete-so-far CSV, and a resumed run
just keeps appending (existing docket numbers are skipped upstream via the
checkpoint, so this never double-writes a row for the same case).
"""

import csv
import os

COLUMNS = [
    "Town",
    "Docket No.",
    "Case Caption",
    "Motion Type Found",
    "Total Debt",
    "Encumbrances Subsequent to Plaintiff's Lien",
    "Attorney Fees",
    "Motion for Default - Failure to Appear (Y/N)",
    "Bankruptcy Stay + Reopened (Y/N)",
    "Bankruptcy Supporting Text",
    "Case Detail URL",
    "Foreclosure Worksheet Doc URL",
    "OCR Validated",
    "OCR Warning",
]


class ResultWriter:
    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(COLUMNS)
            self._fh.flush()

    def write(self, result, worksheet=None) -> None:
        row = [
            result.town,
            result.docket_no,
            result.case_caption,
            "; ".join(result.motion_types_found),
            f"{result.total_debt:.2f}" if result.total_debt is not None else "",
            result.encumbrances_subsequent_itemized,
            result.attorney_fees or "",
            "Y" if result.default_failure_to_appear else "N",
            "Y" if result.bankruptcy_stay_reopened else "N",
            result.bankruptcy_supporting_text,
            result.case_detail_url,
            result.worksheet_doc_url or "",
            ("Y" if worksheet.ocr_validated else "N") if worksheet else "",
            (worksheet.ocr_warning or "") if worksheet else "",
        ]
        self._writer.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class ReviewWriter:
    """Separate 'needs manual review' log: docket no + error + timestamp."""

    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(["Timestamp", "Town", "Docket No.", "Error"])
            self._fh.flush()

    def write(self, timestamp: str, town: str, docket_no: str, error: str) -> None:
        self._writer.writerow([timestamp, town, docket_no, error])
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
