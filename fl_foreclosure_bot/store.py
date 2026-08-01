"""Accumulated results + which (county, date) pages have been scraped.

Exists so a run can scrape only the dates it hasn't seen yet while the
exported workbook still shows the whole picture. Without this, an
incremental run could only ever export the handful of days it just
fetched.

Two tables:
  listings      -- every AuctionListing ever scraped, as JSON, keyed by
                   county+case+auction_date.
  scraped_dates -- which (county, auction_date) pages have been fetched,
                   so the next run can skip them.

Re-scraping a date REPLACES that date's rows rather than merging into
them (see replace_date): a case that got rescheduled off a date, or an
auction that vanished from a page, must not linger as a stale row --
the page as it stands today is the truth for that date.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key          TEXT PRIMARY KEY,   -- county|auction_date|case_no
    county       TEXT NOT NULL,
    auction_date TEXT NOT NULL,      -- ISO
    case_no      TEXT NOT NULL,
    data         TEXT NOT NULL,      -- JSON-serialized AuctionListing
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_county_date
    ON listings (county, auction_date);

CREATE TABLE IF NOT EXISTS scraped_dates (
    county       TEXT NOT NULL,
    auction_date TEXT NOT NULL,      -- ISO
    scraped_at   TEXT NOT NULL,
    PRIMARY KEY (county, auction_date)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_date_scraped(self, county: str, auction_date: date) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM scraped_dates WHERE county = ? AND auction_date = ?",
            (county, auction_date.isoformat()),
        )
        return cur.fetchone() is not None

    def replace_date(self, county: str, auction_date: date, listings: list) -> None:
        """Make the store's rows for this (county, date) exactly `listings`.

        Committed as one transaction so an interrupted run can't leave a
        date half-emptied.
        """
        iso = auction_date.isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "DELETE FROM listings WHERE county = ? AND auction_date = ?", (county, iso)
            )
            for listing in listings:
                cur.execute(
                    """INSERT OR REPLACE INTO listings
                       (key, county, auction_date, case_no, data, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"{county}|{iso}|{listing.case_no}",
                        county,
                        iso,
                        listing.case_no,
                        json.dumps(dataclasses.asdict(listing)),
                        _now(),
                    ),
                )
            cur.execute(
                """INSERT OR REPLACE INTO scraped_dates (county, auction_date, scraped_at)
                   VALUES (?, ?, ?)""",
                (county, iso, _now()),
            )
        self._conn.commit()

    def all_listings(self) -> list:
        from .models import AuctionListing

        cur = self._conn.execute("SELECT data FROM listings")
        out = []
        for (raw,) in cur.fetchall():
            fields = json.loads(raw)
            # Tolerate rows written by an older version that predates a
            # newly added field, rather than failing the whole export.
            known = {f.name for f in dataclasses.fields(AuctionListing)}
            out.append(AuctionListing(**{k: v for k, v in fields.items() if k in known}))
        return out

    def stats(self) -> dict:
        listings = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        dates = self._conn.execute("SELECT COUNT(*) FROM scraped_dates").fetchone()[0]
        return {"listings": listings, "dates_scraped": dates}
