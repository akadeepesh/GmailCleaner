"""
GmailCleaner — Database Layer
SQLite interface providing persistent state across runs.

Manages three concerns:
  - Sender history and reputation tracking
  - Action log (pending → confirmed → executed)
  - Run metadata
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

# SQLite journal mode. WAL is fastest on local disk, but it needs shared memory,
# which network filesystems (e.g. Cloud Storage FUSE on Cloud Run) don't provide.
# Set SQLITE_JOURNAL_MODE=DELETE there.
_JOURNAL_MODES = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"}

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS senders (
    domain              TEXT PRIMARY KEY,
    display_name        TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    email_count         INTEGER NOT NULL DEFAULT 0,
    unsubscribed_at     TEXT,
    unsubscribe_method  TEXT,        -- 'http' | 'mailto' | null
    trusted             INTEGER NOT NULL DEFAULT 0,
    blocked             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS action_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    action_type     TEXT NOT NULL,   -- 'unsubscribe' | 'delete' | 'label' | 'skip'
    target          TEXT NOT NULL,   -- message_id or sender domain
    target_meta     TEXT,            -- JSON blob for extra context (subject, count, etc.)
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'executed' | 'failed' | 'skipped' | 'superseded'
    created_at      TEXT NOT NULL,
    executed_at     TEXT,
    error           TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    mode                TEXT NOT NULL,  -- 'scan' | 'execute'
    emails_scanned      INTEGER NOT NULL DEFAULT 0,
    actions_pending     INTEGER NOT NULL DEFAULT 0,
    actions_executed    INTEGER NOT NULL DEFAULT 0,
    actions_failed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS triage_results (
    message_id      TEXT PRIMARY KEY,
    bucket          TEXT NOT NULL,   -- NEEDS_REPLY | FYI | PROMOTIONS | JUNK | UNCLASSIFIED
    confidence      REAL NOT NULL,
    reason          TEXT,
    model           TEXT,
    classified_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_log_run_id ON action_log(run_id);
CREATE INDEX IF NOT EXISTS idx_action_log_status ON action_log(status);
CREATE INDEX IF NOT EXISTS idx_senders_unsubscribed ON senders(unsubscribed_at);
CREATE INDEX IF NOT EXISTS idx_triage_classified_at ON triage_results(classified_at);

CREATE TABLE IF NOT EXISTS watch_registration (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    history_id      TEXT NOT NULL,
    expiration_ms   TEXT NOT NULL,
    topic_name      TEXT NOT NULL,
    registered_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at         TEXT NOT NULL,
    new_count           INTEGER NOT NULL DEFAULT 0,
    needs_reply_count   INTEGER NOT NULL DEFAULT 0,
    promotions_count    INTEGER NOT NULL DEFAULT 0,
    junk_count          INTEGER NOT NULL DEFAULT 0,
    unsub_count         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv_store (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbox_events_recorded ON inbox_events(recorded_at);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SenderRecord:
    domain: str
    display_name: Optional[str]
    first_seen: datetime
    last_seen: datetime
    email_count: int
    unsubscribed_at: Optional[datetime]
    unsubscribe_method: Optional[str]
    trusted: bool
    blocked: bool

    @property
    def already_unsubscribed(self) -> bool:
        return self.unsubscribed_at is not None


@dataclass
class ActionRecord:
    id: Optional[int]
    run_id: str
    action_type: str
    target: str
    target_meta: Optional[str]
    status: str
    created_at: datetime
    executed_at: Optional[datetime]
    error: Optional[str]


# ──────────────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────────────

class Database:
    """
    Thread-safe SQLite wrapper for GmailCleaner state.

    Uses WAL mode by default for better concurrent read performance
    (override with SQLITE_JOURNAL_MODE) and context managers for safe
    transaction handling.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.debug("Database initialised at %s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        mode = os.getenv("SQLITE_JOURNAL_MODE", "WAL").upper()
        if mode not in _JOURNAL_MODES:
            logger.warning("Unknown SQLITE_JOURNAL_MODE %r — falling back to WAL", mode)
            mode = "WAL"
        with self._connect() as conn:
            conn.execute(f"PRAGMA journal_mode={mode}")
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits on success and rolls back on exception."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Runs ──────────────────────────────────────────────────────────────────

    def create_run(self, run_id: str, mode: str) -> None:
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (id, started_at, mode) VALUES (?, ?, ?)",
                (run_id, now, mode),
            )
        logger.debug("Run created: %s (mode=%s)", run_id, mode)

    def complete_run(
        self,
        run_id: str,
        emails_scanned: int,
        actions_pending: int,
        actions_executed: int,
        actions_failed: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    completed_at = ?,
                    emails_scanned = ?,
                    actions_pending = ?,
                    actions_executed = ?,
                    actions_failed = ?
                WHERE id = ?
                """,
                (_utcnow(), emails_scanned, actions_pending, actions_executed, actions_failed, run_id),
            )

    # ── Senders ───────────────────────────────────────────────────────────────

    def upsert_sender(
        self,
        domain: str,
        display_name: Optional[str],
        email_count: int,
    ) -> None:
        """
        Insert a new sender or update last_seen / email_count / display_name.
        Does not overwrite unsubscribed_at or trust/block flags.
        """
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO senders (domain, display_name, first_seen, last_seen, email_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, senders.display_name),
                    last_seen    = excluded.last_seen,
                    email_count  = excluded.email_count
                """,
                (domain, display_name, now, now, email_count),
            )

    def increment_sender(
        self,
        domain: str,
        display_name: Optional[str],
        count: int = 1,
    ) -> None:
        """
        Insert a new sender or add `count` to its email_count.
        Used by the real-time webhook, which sees messages one notification at a time.
        """
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO senders (domain, display_name, first_seen, last_seen, email_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, senders.display_name),
                    last_seen    = excluded.last_seen,
                    email_count  = senders.email_count + excluded.email_count
                """,
                (domain, display_name, now, now, count),
            )

    def list_senders(self) -> list[dict]:
        """Return every known sender, noisiest first (used by exports)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM senders ORDER BY email_count DESC, domain ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_top_senders(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain, display_name, email_count FROM senders "
                "ORDER BY email_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_sender(self, domain: str) -> Optional[SenderRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM senders WHERE domain = ?", (domain,)
            ).fetchone()
        return _row_to_sender(row) if row else None

    def mark_unsubscribed(self, domain: str, method: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE senders SET unsubscribed_at = ?, unsubscribe_method = ? WHERE domain = ?",
                (_utcnow(), method, domain),
            )
        logger.info("Marked %s as unsubscribed (method=%s)", domain, method)

    def get_unsubscribed_domains(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain FROM senders WHERE unsubscribed_at IS NOT NULL"
            ).fetchall()
        return {row["domain"] for row in rows}

    def get_blocked_domains(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain FROM senders WHERE blocked = 1"
            ).fetchall()
        return {row["domain"] for row in rows}

    # ── Action log ────────────────────────────────────────────────────────────

    def log_action(
        self,
        run_id: str,
        action_type: str,
        target: str,
        target_meta: Optional[str] = None,
        status: str = "pending",
    ) -> int:
        """Insert a new action and return its row ID."""
        now = _utcnow()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO action_log (run_id, action_type, target, target_meta, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, action_type, target, target_meta, status, now),
            )
            return cursor.lastrowid

    def update_action_status(
        self,
        action_id: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        executed_at = _utcnow() if status in ("executed", "failed") else None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE action_log SET status = ?, executed_at = ?, error = ? WHERE id = ?",
                (status, executed_at, error, action_id),
            )

    def get_pending_actions(self, run_id: str) -> list[ActionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM action_log WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            ).fetchall()
        return [_row_to_action(row) for row in rows]

    def supersede_pending_actions(self, keep_run_id: str) -> int:
        """
        Mark pending actions from older runs as 'superseded'.

        Each scan rewrites pending_actions.json from scratch, so pending rows
        from previous runs can no longer be confirmed. Returns rows updated.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE action_log SET status = 'superseded' "
                "WHERE status = 'pending' AND run_id != ?",
                (keep_run_id,),
            )
            return cursor.rowcount

    def list_actions(self, limit: Optional[int] = None) -> list[dict]:
        """Return the action log, newest first (used by exports)."""
        sql = "SELECT * FROM action_log ORDER BY created_at DESC, id DESC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_recent_runs(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run_summary(self, run_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ── Triage results ────────────────────────────────────────────────────────

    def save_triage_results(self, results: list[dict]) -> None:
        """
        Upsert triage classification results.

        Each dict must have: message_id, bucket, confidence, reason, model.
        Existing rows for the same message_id are overwritten (re-classification).
        """
        now = _utcnow()
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO triage_results (message_id, bucket, confidence, reason, model, classified_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    bucket        = excluded.bucket,
                    confidence    = excluded.confidence,
                    reason        = excluded.reason,
                    model         = excluded.model,
                    classified_at = excluded.classified_at
                """,
                [
                    (
                        r["message_id"],
                        r["bucket"],
                        r["confidence"],
                        r.get("reason", ""),
                        r.get("model", ""),
                        now,
                    )
                    for r in results
                ],
            )
        logger.debug("Saved %d triage result(s) to DB", len(results))

    def get_recent_triage_results(self, hours: int = 24) -> list[dict]:
        """
        Return triage results classified within the last `hours` hours.
        Used to avoid re-classifying messages on repeat scans the same day.
        """
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM triage_results WHERE classified_at >= ?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_triage_counts(self) -> dict[str, int]:
        """Return a count per triage bucket (all five buckets always present)."""
        counts = {b: 0 for b in ("NEEDS_REPLY", "FYI", "PROMOTIONS", "JUNK", "UNCLASSIFIED")}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bucket, COUNT(*) AS n FROM triage_results GROUP BY bucket"
            ).fetchall()
        for row in rows:
            counts[row["bucket"]] = row["n"]
        return counts

    def get_triage_result(self, message_id: str) -> Optional[dict]:
        """Return the triage result for a single message, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM triage_results WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    # ── Watch registration ────────────────────────────────────────────────────

    def save_watch_registration(self, reg: "WatchRegistration") -> None:
        """Upsert the singleton watch registration row."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO watch_registration (id, history_id, expiration_ms, topic_name, registered_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    history_id    = excluded.history_id,
                    expiration_ms = excluded.expiration_ms,
                    topic_name    = excluded.topic_name,
                    registered_at = excluded.registered_at
                """,
                (reg.history_id, reg.expiration_ms, reg.topic_name, reg.registered_at),
            )

    def get_watch_registration(self) -> Optional[dict]:
        """Return the current watch registration, or None if not registered."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watch_registration WHERE id = 1").fetchone()
        return dict(row) if row else None

    def clear_watch_registration(self) -> None:
        """Remove the watch registration (after stop_watch)."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM watch_registration WHERE id = 1")

    # ── History ID (Pub/Sub baseline) ─────────────────────────────────────────

    def save_history_id(self, history_id: str) -> None:
        """Store the most recently processed Gmail historyId."""
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO kv_store (key, value, updated_at) VALUES ('last_history_id', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (history_id, now),
            )

    def get_last_history_id(self) -> Optional[str]:
        """Return the last stored historyId, or None if never set."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = 'last_history_id'"
            ).fetchone()
        return row["value"] if row else None

    # ── Inbox events (dashboard stats) ───────────────────────────────────────

    def record_inbox_event(
        self,
        new_count: int,
        needs_reply_count: int,
        promotions_count: int,
        junk_count: int,
        unsub_count: int,
    ) -> None:
        """Record a snapshot of inbox activity from a webhook notification."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO inbox_events
                    (recorded_at, new_count, needs_reply_count, promotions_count, junk_count, unsub_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_utcnow(), new_count, needs_reply_count, promotions_count, junk_count, unsub_count),
            )

    def get_inbox_stats(self, days: int = 7) -> list[dict]:
        """
        Return daily inbox event aggregates for the last `days` days.
        Used by the web dashboard to render activity charts.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    date(recorded_at) AS day,
                    SUM(new_count)           AS total_new,
                    SUM(needs_reply_count)   AS total_needs_reply,
                    SUM(promotions_count)    AS total_promotions,
                    SUM(junk_count)          AS total_junk,
                    SUM(unsub_count)         AS total_unsub
                FROM inbox_events
                WHERE recorded_at >= ?
                GROUP BY date(recorded_at)
                ORDER BY day ASC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dashboard_summary(self) -> dict:
        """
        Return a flat summary dict for the dashboard header:
        total senders seen, unsubscribed count, pending actions, last run.
        """
        with self._connect() as conn:
            total_senders = conn.execute("SELECT COUNT(*) FROM senders").fetchone()[0]
            unsubscribed = conn.execute(
                "SELECT COUNT(*) FROM senders WHERE unsubscribed_at IS NOT NULL"
            ).fetchone()[0]
            pending_actions = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE status = 'pending'"
            ).fetchone()[0]
            last_run = conn.execute(
                "SELECT completed_at FROM runs WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            triage_needs_reply = conn.execute(
                "SELECT COUNT(*) FROM triage_results WHERE bucket = 'NEEDS_REPLY'"
            ).fetchone()[0]
            triage_unclassified = conn.execute(
                "SELECT COUNT(*) FROM triage_results WHERE bucket = 'UNCLASSIFIED'"
            ).fetchone()[0]
        return {
            "total_senders": total_senders,
            "unsubscribed": unsubscribed,
            "pending_actions": pending_actions,
            "last_run": last_run[0] if last_run else None,
            "triage_needs_reply": triage_needs_reply,
            "triage_unclassified": triage_unclassified,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_sender(row: sqlite3.Row) -> SenderRecord:
    return SenderRecord(
        domain=row["domain"],
        display_name=row["display_name"],
        first_seen=datetime.fromisoformat(row["first_seen"]),
        last_seen=datetime.fromisoformat(row["last_seen"]),
        email_count=row["email_count"],
        unsubscribed_at=datetime.fromisoformat(row["unsubscribed_at"]) if row["unsubscribed_at"] else None,
        unsubscribe_method=row["unsubscribe_method"],
        trusted=bool(row["trusted"]),
        blocked=bool(row["blocked"]),
    )


def _row_to_action(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=row["id"],
        run_id=row["run_id"],
        action_type=row["action_type"],
        target=row["target"],
        target_meta=row["target_meta"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
        error=row["error"],
    )