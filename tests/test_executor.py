"""
Tests for gmail/executor.py

Covers:
  - Unsubscribe method priority: one-click POST > GET > mailto fallback
  - HTTP success / failure status codes, timeouts and network errors
  - Mailto unsubscribe composes an "unsubscribe" email via the Gmail API
  - DB updates: mark_unsubscribed + action status on success and failure
  - delete_group: moves messages to Trash in 1,000-ID chunks
  - delete_group: partial and total failure accounting
  - execute_report: only confirmed, not-yet-executed items run; executed_at stamped
  - find_or_create_action: reuses pending rows, creates confirmed rows otherwise
"""

import base64
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import httpx
import pytest
from googleapiclient.errors import HttpError

from gmail.executor import (
    ExecutionSummary,
    GmailExecutor,
    UnsubscribeResult,
    DeleteResult,
    confirmed_items,
    execute_report,
    find_or_create_action,
)
from store.db import Database


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_sleep():
    with patch("gmail.executor.time.sleep"):
        yield


@pytest.fixture
def client():
    return MagicMock()


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def executor(client, db):
    with GmailExecutor(client, db) as ex:
        yield ex


def _http_error(status: int = 500) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp=resp, content=b"error")


# ── Unsubscribe ───────────────────────────────────────────────────────────────

class TestUnsubscribe:
    def test_one_click_uses_post(self, executor, db):
        with patch.object(executor._http, "post", return_value=MagicMock(status_code=200)) as post:
            result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                                          supports_one_click=True)
        assert result.success and result.method == "http_post"
        _, kwargs = post.call_args
        assert kwargs["content"] == b"List-Unsubscribe=One-Click"
        assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        db.mark_unsubscribed.assert_called_once_with("a.com", "http_post")
        db.update_action_status.assert_called_once_with(1, "executed")

    def test_plain_http_uses_get(self, executor):
        with patch.object(executor._http, "get", return_value=MagicMock(status_code=204)) as get, \
             patch.object(executor._http, "post") as post:
            result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert result.success and result.method == "http_get"
        get.assert_called_once_with("https://a.com/u")
        post.assert_not_called()

    @pytest.mark.parametrize("status", [200, 202, 204, 302])
    def test_success_status_codes(self, executor, status):
        with patch.object(executor._http, "get", return_value=MagicMock(status_code=status)):
            assert executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u").success

    def test_http_error_status_marks_failed(self, executor, db):
        with patch.object(executor._http, "get", return_value=MagicMock(status_code=404)):
            result = executor.unsubscribe("a.com", "r1", 7, http_url="https://a.com/u")
        assert not result.success
        assert result.status_code == 404
        assert "404" in result.error
        db.mark_unsubscribed.assert_not_called()
        db.update_action_status.assert_called_once_with(7, "failed", error=result.error)

    def test_timeout_reported(self, executor):
        with patch.object(executor._http, "post", side_effect=httpx.TimeoutException("slow")):
            result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                                          supports_one_click=True)
        assert not result.success and result.error == "Request timed out"

    def test_network_error_reported(self, executor):
        with patch.object(executor._http, "get", side_effect=httpx.ConnectError("refused")):
            result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert not result.success and "refused" in result.error

    def test_http_failure_falls_back_to_mailto(self, executor, client):
        with patch.object(executor._http, "get", return_value=MagicMock(status_code=500)):
            result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                                          mailto="unsub@a.com")
        assert result.success and result.method == "mailto"
        client.send_message.assert_called_once()

    def test_mailto_composes_unsubscribe_email(self, executor, client):
        result = executor.unsubscribe("a.com", "r1", 1, mailto="unsub@a.com")
        assert result.success
        raw = client.send_message.call_args[0][0]
        msg = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert msg["To"] == "unsub@a.com"
        assert msg["Subject"] == "unsubscribe"

    def test_mailto_api_error_marks_failed(self, executor, client, db):
        client.send_message.side_effect = _http_error(403)
        result = executor.unsubscribe("a.com", "r1", 3, mailto="unsub@a.com")
        assert not result.success and result.method == "mailto"
        db.update_action_status.assert_called_once()
        assert db.update_action_status.call_args[0][:2] == (3, "failed")

    def test_no_target_fails_cleanly(self, executor, db):
        result = executor.unsubscribe("a.com", "r1", 1)
        assert not result.success
        assert result.method == "failed"
        assert "No valid unsubscribe target" in result.error

    def test_user_agent_identifies_project(self, executor):
        ua = executor._http.headers["User-Agent"]
        assert ua.startswith("GmailCleaner/")
        assert "github.com/akadeepesh/GmailCleaner" in ua


# ── Bulk cleanup ──────────────────────────────────────────────────────────────

class TestDeleteGroup:
    def test_moves_messages_to_trash(self, executor, client, db):
        result = executor.delete_group("g1", ["m1", "m2", "m3"], "r1", 5)
        client.batch_trash.assert_called_once_with(["m1", "m2", "m3"])
        assert result.deleted == 3 and result.failed == 0 and result.errors == []
        db.update_action_status.assert_called_once_with(5, "executed")

    def test_never_permanently_deletes(self, executor, client):
        executor.delete_group("g1", ["m1"], "r1", 5)
        client.batch_delete.assert_not_called()

    def test_chunks_of_1000(self, executor, client):
        ids = [f"m{i}" for i in range(2500)]
        result = executor.delete_group("g1", ids, "r1", 1)
        sizes = [len(c.args[0]) for c in client.batch_trash.call_args_list]
        assert sizes == [1000, 1000, 500]
        assert result.deleted == 2500

    def test_partial_failure(self, executor, client, db):
        ids = [f"m{i}" for i in range(1500)]
        client.batch_trash.side_effect = [None, _http_error()]
        result = executor.delete_group("g1", ids, "r1", 9)
        assert result.deleted == 1000 and result.failed == 500
        assert len(result.errors) == 1 and "Chunk 2/2" in result.errors[0]
        status_call = db.update_action_status.call_args
        assert status_call.args[:2] == (9, "failed")
        assert "Partial" in status_call.kwargs["error"]

    def test_total_failure(self, executor, client, db):
        client.batch_trash.side_effect = _http_error()
        result = executor.delete_group("g1", ["m1"], "r1", 2)
        assert result.deleted == 0 and result.failed == 1
        assert db.update_action_status.call_args.args[:2] == (2, "failed")

    def test_empty_group(self, executor, client, db):
        result = executor.delete_group("g1", [], "r1", 1)
        client.batch_trash.assert_not_called()
        assert result.deleted == 0 and result.failed == 0


# ── Summary ───────────────────────────────────────────────────────────────────

class TestExecutionSummary:
    def test_totals(self):
        summary = ExecutionSummary(
            run_id="r1",
            unsubscribe_results=[
                UnsubscribeResult("a.com", "http_post", True),
                UnsubscribeResult("b.com", "failed", False, error="x"),
            ],
            delete_results=[DeleteResult("g1", 10, 8, 2, ["err"])],
        )
        assert summary.total_unsubscribed == 1
        assert summary.total_deleted == 8
        assert summary.total_failed == 3


# ── Report execution ──────────────────────────────────────────────────────────

def _report() -> dict:
    return {
        "run_id": "r1",
        "unsubscribe": [
            {"domain": "a.com", "confirmed": True, "http_url": "https://a.com/u",
             "supports_one_click": True},
            {"domain": "b.com", "confirmed": False, "http_url": "https://b.com/u"},
            {"domain": "c.com", "confirmed": True, "executed_at": "2024-01-01T00:00:00"},
        ],
        "delete_groups": [
            {"group_id": "domain:d.com", "confirmed": True, "message_ids": ["m1", "m2"]},
            {"group_id": "domain:e.com", "confirmed": False, "message_ids": ["m3"]},
        ],
    }


class TestExecuteReport:
    def test_confirmed_items_filters(self):
        unsub, delete = confirmed_items(_report())
        assert [i["domain"] for i in unsub] == ["a.com"]
        assert [i["group_id"] for i in delete] == ["domain:d.com"]

    def test_executes_only_confirmed_and_stamps(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.create_run("r1", mode="scan")
        client = MagicMock()
        report = _report()
        seen = []

        with GmailExecutor(client, db) as ex, \
             patch.object(ex._http, "post", return_value=MagicMock(status_code=200)):
            summary = execute_report(ex, db, report, on_result=lambda t, i, r: seen.append((t, i.get("domain") or i["group_id"])))

        assert summary.total_unsubscribed == 1
        assert summary.total_deleted == 2
        assert seen == [("unsubscribe", "a.com"), ("delete", "domain:d.com")]
        assert report["unsubscribe"][0]["executed_at"]
        assert "executed_at" not in report["unsubscribe"][1]
        assert report["delete_groups"][0]["executed_at"]
        client.batch_trash.assert_called_once_with(["m1", "m2"])

        # The execute pass is recorded as its own run.
        modes = [r["mode"] for r in db.get_recent_runs()]
        assert modes.count("execute") == 1

    def test_failed_items_not_stamped(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        client = MagicMock()
        client.batch_trash.side_effect = _http_error()
        report = {"run_id": "r1", "unsubscribe": [],
                  "delete_groups": [{"group_id": "g", "confirmed": True, "message_ids": ["m1"]}]}
        with GmailExecutor(client, db) as ex:
            summary = execute_report(ex, db, report)
        assert summary.total_failed == 1
        assert "executed_at" not in report["delete_groups"][0]


class TestFindOrCreateAction:
    def test_reuses_pending_row(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        action_id = db.log_action("r1", "delete", "g1", status="pending")
        assert find_or_create_action(db, "r1", "delete", "g1") == action_id

    def test_creates_confirmed_row(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        new_id = find_or_create_action(db, "r1", "unsubscribe", "a.com")
        actions = db.list_actions()
        assert actions[0]["id"] == new_id
        assert actions[0]["status"] == "confirmed"
