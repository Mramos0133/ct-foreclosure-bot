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

# SmartMLS numbers render as a run of 6-10 digits, sometimes with a
# leading letter prefix. Deliberately loose; `listings_from_rows` dedups.
MLS_NO_RE = re.compile(r"\b([A-Z]{0,2}\d{6,10})\b")
_STATUS_RE = re.compile(
    r"\b(expired|withdrawn|cancell?ed|temporarily off market|temp off market)\b",
    re.IGNORECASE,
)
# "123 Main St" / "45 North Rd Unit 2B" -- number then words. Used only to
# pick the most address-like text in a block, never to invent one.
_ADDRESS_RE = re.compile(
    r"\b\d+[A-Za-z]?\s+[A-Za-z0-9.'\-]+(?:\s+[A-Za-z0-9.'\-]+){0,5}\b"
)
_ZIP_RE = re.compile(r"\b(0[6-9]\d{3})\b")  # CT ZIPs are 06xxx-069xx


def _normalize_status(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in {"cancelled"}:
        return "canceled"
    if lowered in {"temp off market"}:
        return "temporarily off market"
    return lowered


def _block_to_listing(text: str, source: str, received_at: str) -> AlertListing | None:
    status_match = _STATUS_RE.search(text)
    mls_match = MLS_NO_RE.search(text)
    if not status_match or not mls_match:
        return None
    status = _normalize_status(status_match.group(1))
    if status not in EXPIRED_STATUSES:
        return None

    # Take the address candidate that is not the MLS number itself.
    address = ""
    for candidate in _ADDRESS_RE.findall(text):
        if mls_match.group(1) in candidate:
            continue
        address = candidate.strip()
        break

    zip_match = _ZIP_RE.search(text)
    return AlertListing(
        mls_no=mls_match.group(1),
        street_address=address,
        zip_code=zip_match.group(1) if zip_match else "",
        status=status,
        received_at=received_at,
        source_email=source,
    )


def parse_alert_html(html: str, source: str = "", received_at: str = "") -> list[AlertListing]:
    """Extract every expired-ish row from one alert message body."""
    soup = BeautifulSoup(html or "", "html.parser")
    listings: list[AlertListing] = []

    # Alerts are table-based; each listing is usually one <tr>. Falling
    # back to whole-document text would smear several listings together,
    # so blocks are tried narrowest-first.
    blocks = soup.find_all("tr") or soup.find_all(["div", "p"])
    for block in blocks:
        text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))
        if not text:
            continue
        listing = _block_to_listing(text, source, received_at)
        if listing:
            listings.append(listing)

    if not listings:
        whole = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        listing = _block_to_listing(whole, source, received_at)
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
