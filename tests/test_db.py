"""
Tests for store/db.py

Uses a temp file for the SQLite DB per test function to ensure isolation.
"""

import tempfile
from datetime import timezone
from pathlib import Path

import pytest

from store.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Fresh Database instance backed by a temp file for each test."""
    return Database(str(tmp_path / "test.db"))


class TestRuns:
    def test_create_run(self, db: Database):
        db.create_run("run-001", mode="scan")
        summary = db.get_run_summary("run-001")
        assert summary is not None
        assert summary["id"] == "run-001"
        assert summary["mode"] == "scan"
        assert summary["emails_scanned"] == 0

    def test_complete_run(self, db: Database):
        db.create_run("run-002", mode="scan")
        db.complete_run("run-002", emails_scanned=250, actions_pending=5,
                        actions_executed=0, actions_failed=0)
        summary = db.get_run_summary("run-002")
        assert summary["emails_scanned"] == 250
        assert summary["actions_pending"] == 5
        assert summary["completed_at"] is not None

    def test_get_nonexistent_run(self, db: Database):
        assert db.get_run_summary("nonexistent") is None


class TestSenders:
    def test_insert_sender(self, db: Database):
        db.upsert_sender("example.com", "Example Newsletter", 10)
        sender = db.get_sender("example.com")
        assert sender is not None
        assert sender.domain == "example.com"
        assert sender.display_name == "Example Newsletter"
        assert sender.email_count == 10
        assert not sender.already_unsubscribed

    def test_upsert_updates_count(self, db: Database):
        db.upsert_sender("example.com", "Example", 10)
        db.upsert_sender("example.com", "Example", 25)
        sender = db.get_sender("example.com")
        assert sender.email_count == 25

    def test_upsert_preserves_unsubscribed_at(self, db: Database):
        db.upsert_sender("example.com", "Example", 10)
        db.mark_unsubscribed("example.com", "http")
        db.upsert_sender("example.com", "Example", 20)  # Update count
        sender = db.get_sender("example.com")
        assert sender.already_unsubscribed  # Should not be cleared

    def test_mark_unsubscribed(self, db: Database):
        db.upsert_sender("example.com", "Example", 5)
        db.mark_unsubscribed("example.com", "mailto")
        sender = db.get_sender("example.com")
        assert sender.already_unsubscribed
        assert sender.unsubscribe_method == "mailto"

    def test_get_unsubscribed_domains(self, db: Database):
        db.upsert_sender("a.com", "A", 3)
        db.upsert_sender("b.com", "B", 3)
        db.mark_unsubscribed("a.com", "http")
        domains = db.get_unsubscribed_domains()
        assert "a.com" in domains
        assert "b.com" not in domains

    def test_get_nonexistent_sender(self, db: Database):
        assert db.get_sender("nothere.com") is None


class TestActionLog:
    def test_log_action_returns_id(self, db: Database):
        db.create_run("run-001", "scan")
        action_id = db.log_action("run-001", "unsubscribe", "example.com")
        assert isinstance(action_id, int)
        assert action_id > 0

    def test_get_pending_actions(self, db: Database):
        db.create_run("run-001", "scan")
        db.log_action("run-001", "unsubscribe", "a.com", status="pending")
        db.log_action("run-001", "unsubscribe", "b.com", status="executed")
        pending = db.get_pending_actions("run-001")
        assert len(pending) == 1
        assert pending[0].target == "a.com"

    def test_update_action_to_executed(self, db: Database):
        db.create_run("run-001", "scan")
        action_id = db.log_action("run-001", "unsubscribe", "a.com")
        db.update_action_status(action_id, "executed")
        pending = db.get_pending_actions("run-001")
        assert pending == []

    def test_update_action_with_error(self, db: Database):
        db.create_run("run-001", "scan")
        action_id = db.log_action("run-001", "unsubscribe", "a.com")
        db.update_action_status(action_id, "failed", error="Timeout")
        # Failed actions are not in pending list
        pending = db.get_pending_actions("run-001")
        assert pending == []

    def test_schema_initialised_idempotently(self, tmp_path: Path):
        """Creating a second Database on the same file should not fail."""
        path = str(tmp_path / "idempotent.db")
        db1 = Database(path)
        db2 = Database(path)  # Should not raise
        db2.upsert_sender("x.com", "X", 1)
        assert db2.get_sender("x.com") is not None