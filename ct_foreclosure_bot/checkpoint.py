"""Resumable run state.

Two things need to survive a crash/interrupt and let a re-run skip
already-done work without redoing it:
  1. Which docket numbers have already been fully processed (checked for
     target motions, and -- if matched -- had their worksheet extracted).
     A docket is idempotent to reprocess, but the whole point of
     checkpointing under a hard rate limit is to *not* re-spend requests
     on it.
  2. Which towns have been fully walked end-to-end, so a full statewide
     run can skip whole towns on resume.

Backed by SQLite for atomic, crash-safe writes (each mark_* call commits
immediately, so a kill -9 mid-run loses at most the in-flight request, not
prior progress).
"""

import dataclasses
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_dockets (
    docket_no   TEXT PRIMARY KEY,
    town        TEXT NOT NULL,
    matched     INTEGER NOT NULL,   -- 1 if it had a target motion, else 0
    status      TEXT NOT NULL,      -- 'ok' | 'error'
    error       TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completed_towns (
    town         TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

-- Full structured CaseResult per match, stored as JSON. Kept separate from
-- processed_dockets (which is just the resume/skip bookkeeping) so the
-- lead-ranking export can query and re-sort every matched case's full
-- data at any time without re-running the crawl.
CREATE TABLE IF NOT EXISTS case_results (
    docket_no    TEXT PRIMARY KEY,
    lead_bucket  TEXT NOT NULL,
    data         TEXT NOT NULL,  -- JSON-serialized CaseResult
    saved_at     TEXT NOT NULL
);

-- Tracks which already-matched cases have had case_summary backfilled by
-- a maintenance pass (see cli.py --backfill-case-summary), independent of
-- processed_dockets/case_results so a re-run after a restart skips work
-- already done instead of re-fetching every docket from scratch.
CREATE TABLE IF NOT EXISTS summary_backfilled (
    docket_no      TEXT PRIMARY KEY,
    backfilled_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Checkpoint:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_docket_processed(self, docket_no: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM processed_dockets WHERE docket_no = ?", (docket_no,)
        )
        return cur.fetchone() is not None

    def mark_docket_processed(
        self, docket_no: str, town: str, matched: bool, status: str = "ok", error: str | None = None
    ) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT OR REPLACE INTO processed_dockets
                   (docket_no, town, matched, status, error, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (docket_no, town, int(matched), status, error, _now()),
            )
        self._conn.commit()

    def is_town_completed(self, town: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM completed_towns WHERE town = ?", (town,)
        )
        return cur.fetchone() is not None

    def mark_town_completed(self, town: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE INTO completed_towns (town, completed_at) VALUES (?, ?)",
                (town, _now()),
            )
        self._conn.commit()

    def review_items(self) -> list[tuple[str, str, str]]:
        """Return (docket_no, town, error) for every docket that errored."""
        cur = self._conn.execute(
            "SELECT docket_no, town, error FROM processed_dockets WHERE status = 'error'"
        )
        return cur.fetchall()

    def save_case_result(self, result) -> None:
        """Store a full CaseResult (from models.py), keyed by docket_no."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT OR REPLACE INTO case_results (docket_no, lead_bucket, data, saved_at)
                   VALUES (?, ?, ?, ?)""",
                (result.docket_no, result.lead_bucket, json.dumps(dataclasses.asdict(result)), _now()),
            )
        self._conn.commit()

    def all_case_results(self):
        """Return every stored CaseResult, reconstructed from JSON."""
        from .models import CaseResult

        cur = self._conn.execute("SELECT data FROM case_results")
        return [CaseResult(**json.loads(row[0])) for row in cur.fetchall()]

    def is_summary_backfilled(self, docket_no: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM summary_backfilled WHERE docket_no = ?", (docket_no,)
        )
        return cur.fetchone() is not None

    def mark_summary_backfilled(self, docket_no: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE INTO summary_backfilled (docket_no, backfilled_at) VALUES (?, ?)",
                (docket_no, _now()),
            )
        self._conn.commit()

    def summary_backfill_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM case_results").fetchone()[0]
        done = self._conn.execute("SELECT COUNT(*) FROM summary_backfilled").fetchone()[0]
        return {"total": total, "backfilled": done}

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM processed_dockets").fetchone()[0]
        matched = self._conn.execute(
            "SELECT COUNT(*) FROM processed_dockets WHERE matched = 1"
        ).fetchone()[0]
        errors = self._conn.execute(
            "SELECT COUNT(*) FROM processed_dockets WHERE status = 'error'"
        ).fetchone()[0]
        towns_done = self._conn.execute("SELECT COUNT(*) FROM completed_towns").fetchone()[0]
        return {"dockets_processed": total, "matched": matched, "errors": errors, "towns_completed": towns_done}
