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
