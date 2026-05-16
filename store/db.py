"""
GmailCleaner — Database Layer
SQLite interface providing persistent state across runs.

Manages three concerns:
  - Sender history and reputation tracking
  - Action log (pending → confirmed → executed)
  - Run metadata
"""

import logging
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

SCHEMA = """
PRAGMA journal_mode=WAL;
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
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'executed' | 'failed' | 'skipped'
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

CREATE INDEX IF NOT EXISTS idx_action_log_run_id ON action_log(run_id);
CREATE INDEX IF NOT EXISTS idx_action_log_status ON action_log(status);
CREATE INDEX IF NOT EXISTS idx_senders_unsubscribed ON senders(unsubscribed_at);
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

    Uses WAL mode for better concurrent read performance and
    context managers for safe transaction handling.
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
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
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

    def get_run_summary(self, run_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


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