"""Step 5: the vendor CSV and the review CSV.

Header handling is configurable because skip trace vendors reject a file
on header mismatch, and the exact strings differ per vendor. The values
this bot can supply are fixed (`CANONICAL_FIELDS`); only their printed
header text and order vary. Three ways to set them:

  (default)                 DEFAULT_HEADERS below
  --csv-headers "A,B,C,..." positional: your header text, in the same
                            order as CANONICAL_FIELDS
  --csv-header-map f.json   explicit: {"Your Header": "canonical_field"},
                            written in the order you want the columns

The positional form is checked for arity and refuses to run on a count
mismatch rather than silently writing a misaligned file -- a shifted
column would send mailing ZIPs to a name field and bill for every row.

Rows whose owner lookup ended in NEEDS_MANUAL_REVIEW or
PORTAL_UNAVAILABLE never reach the vendor file; they go to the review
CSV with the reason attached, per the spec.
"""

import csv
import json
from pathlib import Path

from .models import NA, Lead
from .names import split_owner_name

# The values this bot can put in a vendor file, in their natural order.
CANONICAL_FIELDS = [
    "owner_first",
    "owner_last",
    "property_address",
    "property_city",
    "property_state",
    "property_zip",
    "mailing_address",
    "mailing_city",
    "mailing_state",
    "mailing_zip",
]

# Placeholder until the operator supplies their vendor's exact strings.
DEFAULT_HEADERS = [
    "Owner First Name", "Owner Last Name",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
]

REVIEW_COLUMNS = [
    "MLS #", "Street Address", "Town", "Zip", "Status",
    "Owner Name", "MLS Pull Status", "Reason",
]


class HeaderSpecError(ValueError):
    """Raised when a supplied header spec cannot be trusted to line up."""


def resolve_headers(
    csv_headers: str | None = None,
    header_map_path: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Return [(printed_header, canonical_field)] in output order."""
    if csv_headers and header_map_path:
        raise HeaderSpecError("Pass --csv-headers or --csv-header-map, not both.")

    if header_map_path:
        data = json.loads(Path(header_map_path).read_text())
        if not isinstance(data, dict) or not data:
            raise HeaderSpecError("Header map must be a non-empty {header: field} object.")
        unknown = [v for v in data.values() if v not in CANONICAL_FIELDS]
        if unknown:
            raise HeaderSpecError(
                f"Unknown canonical field(s) {unknown}. Valid: {CANONICAL_FIELDS}"
            )
        return list(data.items())

    if csv_headers:
        headers = [h.strip() for h in csv_headers.split(",") if h.strip()]
        if len(headers) != len(CANONICAL_FIELDS):
            raise HeaderSpecError(
                f"--csv-headers has {len(headers)} columns but this bot supplies "
                f"{len(CANONICAL_FIELDS)} values, in this order: "
                f"{', '.join(CANONICAL_FIELDS)}. Positional mapping needs an exact "
                "count match -- use --csv-header-map for a different shape."
            )
        return list(zip(headers, CANONICAL_FIELDS))

    return list(zip(DEFAULT_HEADERS, CANONICAL_FIELDS))


def _blank(value: str | None) -> str:
    """NA is an internal marker, not something a vendor should receive."""
    return "" if value in (None, NA) else str(value)


def lead_to_row(lead: Lead) -> dict[str, str]:
    # Owner names here always come from an assessor card, which prints
    # LAST FIRST -- passed explicitly rather than leaning on the default.
    first, last = split_owner_name(
        "" if lead.owner.owner_name == NA else lead.owner.owner_name,
        order="last_first",
    )
    town = lead.mls.town if lead.mls.town != NA else lead.alert.town
    zip_code = lead.mls.zip_code if lead.mls.zip_code != NA else lead.alert.zip_code
    return {
        "owner_first": first,
        "owner_last": last,
        "property_address": _blank(lead.alert.street_address),
        "property_city": _blank(town),
        "property_state": "CT",
        "property_zip": _blank(zip_code),
        "mailing_address": _blank(lead.owner.mailing_address),
        "mailing_city": _blank(lead.owner.mailing_city),
        "mailing_state": _blank(lead.owner.mailing_state),
        "mailing_zip": _blank(lead.owner.mailing_zip),
    }


def write_skiptrace_csv(
    path: str | Path,
    leads: list[Lead],
    headers: list[tuple[str, str]] | None = None,
) -> int:
    """Write the vendor file. Only clean rows; returns rows written."""
    headers = headers or resolve_headers()
    clean = [l for l in leads if not l.needs_review]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([printed for printed, _ in headers])
        for lead in clean:
            row = lead_to_row(lead)
            writer.writerow([row.get(field, "") for _, field in headers])
    return len(clean)


def _review_reason(lead: Lead) -> str:
    reasons = []
    if getattr(lead.mls, "likely_rental", False):
        reasons.append(
            f"List price {lead.mls.list_price_final} looks like a monthly rent, "
            "not a sale price -- confirm this is a sale listing"
        )
    note = lead.owner.notes_text()
    if note:
        reasons.append(note)
    return "; ".join(reasons) or lead.owner.status


def write_review_csv(path: str | Path, leads: list[Lead]) -> int:
    """Everything the vendor file refused, with the reason."""
    flagged = [l for l in leads if l.needs_review]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REVIEW_COLUMNS)
        for lead in flagged:
            writer.writerow([
                lead.mls_no,
                lead.alert.street_address,
                lead.mls.town if lead.mls.town != NA else lead.alert.town,
                lead.mls.zip_code if lead.mls.zip_code != NA else lead.alert.zip_code,
                lead.alert.status,
                lead.owner.owner_name,
                lead.mls.pull_status,
                _review_reason(lead),
            ])
    return len(flagged)
