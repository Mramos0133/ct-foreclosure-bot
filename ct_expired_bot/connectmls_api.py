"""connectMLS JSON API client -- the good path for Step 2.

Found by watching what the connectMLS portal itself requests. The portal
is a single-page app: the HTML is an empty shell and every field comes
from JSON. So there is no reason to scrape rendered labels at all, and
this module replaces that approach for any listing it can reach.

VERIFIED, 2026-08-13, against a real shared link:

    GET https://smartmls-apiserver.connectmls.com/api/shared-link/{uuid}

returns {"Listings": [ {...83 fields...} ]} with NO authentication, for
a link the agent has shared. Confirmed values from that response:
ListingId 24119274, StreetAddress '105 Camptown Ave', City 'Derby',
Zip '06418', MlsStatus 'Expired', ListPrice 480000, OriginalListPrice
520000, BedroomsTotal 4, BathroomsFull 2, BathroomsHalf 0, SquareFeet
1496, Acres 0.06, LotSqft 2244, YearBuilt 1884.

WHAT THE SHARED-LINK RESPONSE DOES NOT CARRY: it returned null for
ListingAgent, SalesHistory, ListingReceivedDate and CloseDate, and has
no days-on-market, list-date or expiration-date field at all. Those are
four of the spec's Step 2 fields, so a shared link alone cannot fully
populate a row -- see `price_drops` handling in scoring.py, which
refuses to report "0 drops" when it simply does not know.

THE AUTHENTICATED ENDPOINTS: probing the same host, `/api/listing/{id}`
and `/api/listings/{id}` answer 401 rather than 404 -- they exist and
need a session. They are the likely home of the missing fields. The
exact path, id (MLS number vs. the internal `Id` GUID the payload also
carries) and auth header are NOT known yet, so nothing here guesses at
them. Run `python -m ct_expired_bot --discover-api <mls#>` against a
logged-in profile to capture the real calls, then wire them up here.
"""

import json
import logging
import re
import urllib.error
import urllib.request

from .models import NA, MLS_PULL_FAILED, MlsDetail

log = logging.getLogger(__name__)

API_ROOT = "https://smartmls-apiserver.connectmls.com"
SHARED_LINK_PATH = "/api/shared-link/{uuid}"
REMARKS_LIMIT = 200

_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE
)


class SharedLinkError(RuntimeError):
    pass


def shared_link_uuid(url: str) -> str | None:
    """Pull the listing UUID out of a /shared-link/<slug>/<uuid> URL."""
    match = _UUID_RE.search(url or "")
    return match.group(1) if match else None


def _text(value) -> str:
    """API nulls become NA, never "" or "None"."""
    if value is None or value == "":
        return NA
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _property_type(listing: dict) -> str:
    parts = [
        listing.get("PropertyTypeDescription"),
        listing.get("PropertySubTypeDescription"),
    ]
    joined = " - ".join(p for p in parts if p)
    return joined or NA


def _lot_size(listing: dict) -> str:
    acres, sqft = listing.get("Acres"), listing.get("LotSqft")
    if acres and sqft:
        return f"{acres} acres ({sqft} sqft)"
    if acres:
        return f"{acres} acres"
    return _text(sqft)


def listing_from_payload(listing: dict) -> MlsDetail:
    """Map one API listing object onto MlsDetail.

    Fields the API does not carry stay NA. Nothing is derived or
    estimated here -- in particular, an absent SalesHistory leaves
    price_history empty AND price_history_available False, which is what
    lets scoring tell "no price drops" apart from "no data".
    """
    detail = MlsDetail(mls_no=_text(listing.get("ListingId")))
    detail.list_price_final = _text(listing.get("ListPrice"))
    detail.list_price_original = _text(listing.get("OriginalListPrice"))
    detail.beds = _text(listing.get("BedroomsTotal"))
    detail.full_baths = _text(listing.get("BathroomsFull"))
    detail.half_baths = _text(listing.get("BathroomsHalf"))
    detail.sqft_above_grade = _text(listing.get("SquareFeet"))
    detail.lot_size = _lot_size(listing)
    detail.year_built = _text(listing.get("YearBuilt"))
    detail.property_type = _property_type(listing)
    detail.town = _text(listing.get("City"))
    detail.zip_code = _text(listing.get("Zip"))
    detail.listing_agent = _text(listing.get("ListingAgent"))
    remarks = listing.get("Remarks")
    detail.public_remarks = remarks[:REMARKS_LIMIT] if remarks else NA

    history = listing.get("SalesHistory")
    if history:
        detail.price_history = _price_history(history)
        detail.price_history_available = True

    # Not present in the shared-link response: days_on_market,
    # cumulative_dom, list_date, expiration_date, brokerage. Left at NA
    # rather than approximated from anything else.
    return detail


def _price_history(history) -> list:
    from .models import PriceChange

    changes = []
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            continue
        changes.append(
            PriceChange(
                date=_text(row.get("Date") or row.get("ChangeDate")),
                old_price=_text(row.get("OldPrice") or row.get("PreviousPrice")),
                new_price=_text(row.get("NewPrice") or row.get("Price")),
            )
        )
    return changes


def listing_status(listing: dict) -> str:
    """MlsStatus as printed, e.g. 'Expired'. NA when absent."""
    return _text(listing.get("MlsStatus") or listing.get("MlsStatusDisplay"))


def _get_json(url: str, timeout: int = 45) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SharedLinkError(f"{exc.code} from {url}") from exc
    except Exception as exc:
        raise SharedLinkError(str(exc)) from exc


def fetch_shared_link(url_or_uuid: str) -> MlsDetail:
    """Read a listing from a shared link. No browser, no auth.

    Never raises: a bad link yields an MLS_PULL_FAILED row, same policy
    as the browser path.
    """
    uuid = shared_link_uuid(url_or_uuid) or url_or_uuid.strip()
    detail = MlsDetail(mls_no=NA)
    try:
        payload = _get_json(API_ROOT + SHARED_LINK_PATH.format(uuid=uuid))
    except SharedLinkError as exc:
        detail.pull_status = MLS_PULL_FAILED
        detail.pull_error = f"shared-link API: {exc}"[:300]
        return detail

    listings = payload.get("Listings") or []
    if not listings:
        detail.pull_status = MLS_PULL_FAILED
        detail.pull_error = "shared-link API returned no Listings"
        return detail
    return listing_from_payload(listings[0])
