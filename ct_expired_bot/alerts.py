"""Step 1: pull Expired/Withdrawn/Canceled rows out of SmartMLS alert email.

>>> UNVERIFIED AGAINST A REAL ALERT EMAIL <<<
The extraction patterns below are a structural best guess, in the same
spirit as the EMAP/loan-mod patterns flagged in ct_foreclosure_bot's
motions.py. They have not been checked against a genuine SmartMLS
"Listing Alert" message, because writing them required an alert in hand.
Run `--dump-alerts` on your first real batch, eyeball what comes out, and
tighten `MLS_NO_RE` / `_STATUS_RE` here if your alerts are laid out
differently. Everything downstream of this module is source-agnostic, so
only this file should need to change.

Two supported inputs, both of which keep credentials out of the tool:
  - a directory of saved .eml/.html alert messages (`--alerts-dir`)
  - IMAP, with the password read from CT_EXPIRED_IMAP_PASSWORD at run
    time and never written anywhere (`--imap-host/--imap-user`)

A Cowork/connector run supplies the same rows through
`listings_from_rows()` instead, so the email connector can do the fetch
and this module still owns the parsing.
"""

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

from bs4 import BeautifulSoup

from .models import EXPIRED_STATUSES, AlertListing

log = logging.getLogger(__name__)

# Anchored on the "MLS#:" label the alert actually prints, rather than on
# a bare run of digits. The label is what makes this reliable: an alert
# body is full of other numbers (price, sqft, year built, phone numbers),
# and a bare \d{6,10} would happily match "1,116 SqFt" once the comma is
# stripped. Observed format: "MLS#: 24139663".
MLS_ANCHOR_RE = re.compile(r"MLS\s*#?\s*:?\s*([A-Z]{0,2}\d{6,10})\b", re.IGNORECASE)
# Fallback for a layout that omits the label entirely.
MLS_NO_RE = re.compile(r"\b([A-Z]{0,2}\d{6,10})\b")
_STATUS_RE = re.compile(
    r"\b(expired|withdrawn|cancell?ed|temporarily off market|temp off market)\b",
    re.IGNORECASE,
)
# "701 Bucks Hill Road" -- house number then street words, on ONE line.
# Matching across newlines is what breaks this: the price sits on the
# line above the address, so a \s+ here happily produces
# "000 701 Bucks Hill Road" out of "$320,000\n701 Bucks Hill Road".
_ADDRESS_RE = re.compile(
    r"^\d+[A-Za-z]?[ \t]+[A-Za-z0-9.'\-]+(?:[ \t]+[A-Za-z0-9.'\-]+){0,5}$"
)
# "Waterbury, CT 06704" -- town, state, ZIP on one line.
_TOWN_STATE_ZIP_RE = re.compile(r"([A-Za-z][A-Za-z .'\-]+),\s*CT\s+(0[6-9]\d{3})", re.IGNORECASE)
_ZIP_RE = re.compile(r"\b(0[6-9]\d{3})\b")  # CT ZIPs are 06xxx-069xx


def _normalize_status(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in {"cancelled"}:
        return "canceled"
    if lowered in {"temp off market"}:
        return "temporarily off market"
    return lowered


def _chunk_to_listing(text: str, mls_no: str, source: str, received_at: str) -> AlertListing | None:
    status_match = _STATUS_RE.search(text)
    if not status_match:
        return None
    status = _normalize_status(status_match.group(1))
    if status not in EXPIRED_STATUSES:
        return None

    town, zip_code = "", ""
    tsz = _TOWN_STATE_ZIP_RE.search(text)
    if tsz:
        town, zip_code = tsz.group(1).strip(), tsz.group(2)
    else:
        zip_match = _ZIP_RE.search(text)
        zip_code = zip_match.group(1) if zip_match else ""

    # Line-oriented, because the alert prints each field on its own line
    # and that is far more reliable than scanning a flattened blob: the
    # street address is the first standalone line that looks like one and
    # is not the MLS line, the price, or the "Town, CT ZIP" line.
    address = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or "$" in line or mls_no in line:
            continue
        if _TOWN_STATE_ZIP_RE.search(line):
            continue
        if _ADDRESS_RE.match(line):
            address = line
            break

    return AlertListing(
        mls_no=mls_no,
        street_address=address,
        town=town,
        zip_code=zip_code,
        status=status,
        received_at=received_at,
        source_email=source,
    )


def _split_on_mls_anchors(text: str) -> list[tuple[str, str]]:
    """Slice a body into one (mls_no, chunk) per listing.

    Splitting on the MLS# anchors rather than on DOM elements is what
    makes this robust to the markup: the same code handles a table-based
    card layout and a div-based one, and an alert carrying three
    listings yields three chunks either way. Each chunk runs from a
    little before its own anchor to just before the next one, so the
    status badge -- which prints above the MLS line -- stays with the
    listing it belongs to.
    """
    anchors = list(MLS_ANCHOR_RE.finditer(text))
    if not anchors:
        return []
    chunks: list[tuple[str, str]] = []
    for index, match in enumerate(anchors):
        start = anchors[index - 1].end() if index else 0
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        chunks.append((match.group(1), text[start:end]))
    return chunks


def parse_alert_html(html: str, source: str = "", received_at: str = "") -> list[AlertListing]:
    """Extract every expired-ish row from one alert message body."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = re.sub(r"[ \t]+", " ", soup.get_text("\n", strip=True))
    listings: list[AlertListing] = []

    for mls_no, chunk in _split_on_mls_anchors(text):
        listing = _chunk_to_listing(chunk, mls_no, source, received_at)
        if listing:
            listings.append(listing)
    if listings:
        return listings

    # No "MLS#" label anywhere: fall back to per-DOM-block scanning with a
    # bare number match, which is looser but better than returning nothing.
    for block in soup.find_all("tr") or soup.find_all(["div", "p"]):
        block_text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))
        match = MLS_NO_RE.search(block_text)
        if not block_text or not match:
            continue
        listing = _chunk_to_listing(block_text, match.group(1), source, received_at)
        if listing:
            listings.append(listing)
    return listings


def _message_body_html(msg: Message) -> str:
    """Prefer text/html; fall back to text/plain wrapped so bs4 can read it."""
    html_parts, text_parts = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (html_parts if content_type == "text/html" else text_parts).append(decoded)
    if html_parts:
        return "\n".join(html_parts)
    return "<pre>" + "\n".join(text_parts) + "</pre>"


def _message_received_at(msg: Message) -> str:
    raw = msg.get("Date")
    if not raw:
        return ""
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def listings_from_message(msg: Message, source: str) -> list[AlertListing]:
    return parse_alert_html(
        _message_body_html(msg), source=source, received_at=_message_received_at(msg)
    )


def load_from_dir(alerts_dir: str | Path, since: datetime | None = None) -> list[AlertListing]:
    """Parse every .eml/.html/.htm file in a directory."""
    directory = Path(alerts_dir)
    collected: list[AlertListing] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".eml", ".html", ".htm"}:
            continue
        try:
            if path.suffix.lower() == ".eml":
                msg = email.message_from_bytes(path.read_bytes())
                found = listings_from_message(msg, source=path.name)
            else:
                found = parse_alert_html(
                    path.read_text(errors="replace"), source=path.name
                )
        except Exception as exc:  # one unreadable file never stops the run
            log.warning("could not parse alert file %s: %s", path.name, exc)
            continue
        if since is not None:
            found = [f for f in found if _at_or_after(f.received_at, since)]
        collected.extend(found)
    return collected


def _at_or_after(received_at: str, since: datetime) -> bool:
    if not received_at:
        return True  # undated file: keep it, dedup by MLS# handles repeats
    try:
        return datetime.fromisoformat(received_at) >= since
    except ValueError:
        return True


def load_from_imap(
    host: str,
    user: str,
    password: str,
    since: datetime | None = None,
    mailbox: str = "INBOX",
    sender_filter: str | None = None,
    subject_filter: str = "SmartMLS",
) -> list[AlertListing]:
    """Fetch and parse alert messages over IMAP.

    `password` is passed in from the environment by the caller and is not
    persisted by this module.
    """
    criteria = ["ALL"]
    if since is not None:
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
    if sender_filter:
        criteria += ["FROM", sender_filter]
    elif subject_filter:
        criteria += ["SUBJECT", subject_filter]

    collected: list[AlertListing] = []
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(mailbox, readonly=True)
        typ, data = imap.search(None, *criteria)
        if typ != "OK":
            log.warning("IMAP search failed: %s", typ)
            return []
        for uid in (data[0].split() if data and data[0] else []):
            typ, payload = imap.fetch(uid, "(RFC822)")
            if typ != "OK" or not payload or not payload[0]:
                continue
            msg = email.message_from_bytes(payload[0][1])
            collected.extend(listings_from_message(msg, source=f"imap:{uid.decode()}"))
    return collected


def dedupe(listings: list[AlertListing]) -> list[AlertListing]:
    """One row per MLS number, keeping the earliest-seen occurrence."""
    seen: dict[str, AlertListing] = {}
    for listing in listings:
        if listing.mls_no not in seen:
            seen[listing.mls_no] = listing
    return list(seen.values())
