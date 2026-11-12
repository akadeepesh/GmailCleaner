"""
Tests for gmail/executor.py

Covers:
  - Unsubscribe method priority: one-click POST > GET > mailto fallback
  - HTTP success / failure status codes, timeouts and network errors
  - Unsubscribe hardening: blocked URLs, every redirect hop checked and capped,
    browser-style method switching, mailto fallback, response size cap,
    connect-time (DNS rebinding) block via the default guarded transport
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
from gmail.url_guard import GuardedTransport
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


class FakeServer:
    """httpx.MockTransport handler that records requests and returns canned responses."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(200)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def respond(self, status: int = 200, **kwargs) -> None:
        self.handler = lambda request: httpx.Response(status, **kwargs)

    def raise_(self, exc: Exception) -> None:
        def handler(request):
            raise exc
        self.handler = handler


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def executor(client, db, server):
    with GmailExecutor(client, db, transport=httpx.MockTransport(server)) as ex:
        yield ex


def _http_error(status: int = 500) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp=resp, content=b"error")


# ── Unsubscribe ───────────────────────────────────────────────────────────────

class TestUnsubscribe:
    def test_one_click_uses_post(self, executor, db, server):
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                                      supports_one_click=True)
        assert result.success and result.method == "http_post"
        [request] = server.requests
        assert request.method == "POST"
        assert request.content == b"List-Unsubscribe=One-Click"
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        db.mark_unsubscribed.assert_called_once_with("a.com", "http_post")
        db.update_action_status.assert_called_once_with(1, "executed")

    def test_plain_http_uses_get(self, executor, server):
        server.respond(204)
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert result.success and result.method == "http_get"
        [request] = server.requests
        assert request.method == "GET"
        assert str(request.url) == "https://a.com/u"

    @pytest.mark.parametrize("status", [200, 202, 204, 302])
    def test_success_status_codes(self, executor, server, status):
        # A 302 without a Location header is final and still counts as success.
        server.respond(status)
        assert executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u").success

    def test_http_error_status_marks_failed(self, executor, db, server):
        server.respond(404)
        result = executor.unsubscribe("a.com", "r1", 7, http_url="https://a.com/u")
        assert not result.success
        assert result.status_code == 404
        assert "404" in result.error
        db.mark_unsubscribed.assert_not_called()
        db.update_action_status.assert_called_once_with(7, "failed", error=result.error)

    def test_timeout_reported(self, executor, server):
        server.raise_(httpx.TimeoutException("slow"))
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                                      supports_one_click=True)
        assert not result.success and result.error == "Request timed out"

    def test_network_error_reported(self, executor, server):
        server.raise_(httpx.ConnectError("refused"))
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert not result.success and "refused" in result.error

    def test_http_failure_falls_back_to_mailto(self, executor, client, server):
        server.respond(500)
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

    def test_client_ignores_proxy_env_and_does_not_auto_follow(self, client, db, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
        with GmailExecutor(client, db) as ex:
            assert ex._http.trust_env is False
            assert ex._http.follow_redirects is False
            assert isinstance(ex._http._transport, GuardedTransport)


# ── Unsubscribe request hardening ─────────────────────────────────────────────

class TestUnsubscribeHardening:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/computeMetadata/v1/",
        "http://127.0.0.1:8080/api/execute",
        "http://localhost/",
        "http://user:pass@a.com/u",
        "http://a.com:22/u",
    ])
    def test_blocked_url_never_requested(self, executor, server, db, url):
        result = executor.unsubscribe("a.com", "r1", 1, http_url=url)
        assert not result.success
        assert result.error.startswith("Blocked unsafe unsubscribe URL: ")
        assert server.requests == []
        db.mark_unsubscribed.assert_not_called()

    def test_redirect_to_metadata_server_stops(self, executor, server):
        server.respond(302, headers={"Location": "http://169.254.169.254/computeMetadata/v1/"})
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert not result.success
        assert result.error.startswith("Blocked unsafe unsubscribe URL: ")
        assert [str(r.url) for r in server.requests] == ["https://a.com/u"]

    def test_more_than_five_redirects_fails(self, executor, server):
        server.handler = lambda r: httpx.Response(
            302, headers={"Location": f"/hop{len(server.requests)}"})
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert not result.success
        assert "redirect" in result.error.lower()
        assert len(server.requests) == 6  # the original request + 5 followed hops

    def test_follows_five_redirects(self, executor, server):
        def handler(request):
            n = len(server.requests)
            if n <= 5:
                return httpx.Response(301, headers={"Location": f"/hop{n}"})
            return httpx.Response(200)
        server.handler = handler
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert result.success and result.status_code == 200
        assert str(server.requests[-1].url) == "https://a.com/hop5"

    def test_relative_location_and_https_to_http_allowed(self, executor, server):
        def handler(request):
            if request.url.path == "/u":
                return httpx.Response(307, headers={"Location": "done?x=1"})
            if request.url.path == "/done":
                return httpx.Response(302, headers={"Location": "http://b.com/final"})
            return httpx.Response(200)
        server.handler = handler
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert result.success
        assert [str(r.url) for r in server.requests] == [
            "https://a.com/u", "https://a.com/done?x=1", "http://b.com/final"]

    @pytest.mark.parametrize("status,expected", [
        (303, "GET"), (302, "GET"), (301, "GET"), (307, "POST"), (308, "POST")])
    def test_redirect_method_after_post(self, executor, server, status, expected):
        server.handler = lambda r: (
            httpx.Response(status, headers={"Location": "/next"}) if r.url.path == "/u"
            else httpx.Response(200))
        executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u",
                             supports_one_click=True)
        second = server.requests[1]
        assert second.method == expected
        if expected == "GET":
            assert second.content == b""
            assert "content-type" not in second.headers
        else:
            assert second.content == b"List-Unsubscribe=One-Click"

    def test_blocked_url_falls_back_to_mailto(self, executor, server, client, caplog):
        with caplog.at_level("WARNING", logger="gmail.executor"):
            result = executor.unsubscribe(
                "a.com", "r1", 1,
                http_url="http://169.254.169.254/computeMetadata/v1/?token=SECRET",
                mailto="unsub@a.com")
        assert result.success and result.method == "mailto"
        client.send_message.assert_called_once()
        assert server.requests == []
        assert "a.com" in caplog.text
        assert "SECRET" not in caplog.text
        assert "computeMetadata" not in caplog.text

    def test_response_body_read_is_capped(self, executor, server):
        consumed = {"bytes": 0}

        def body():
            for _ in range(10_000):  # ~640 MB if fully read
                consumed["bytes"] += 65536
                yield b"x" * 65536

        server.handler = lambda r: httpx.Response(200, content=body())
        result = executor.unsubscribe("a.com", "r1", 1, http_url="https://a.com/u")
        assert result.success
        assert consumed["bytes"] <= GmailExecutor._MAX_RESPONSE_BYTES + 65536

    def test_dns_rebinding_blocked_by_default_transport(self, client, db):
        # Passes check_url (public-looking name) but resolves to a private
        # address at connect time: the guarded transport must refuse.
        with patch("gmail.url_guard.socket.getaddrinfo") as dns, \
             GmailExecutor(client, db) as ex:
            dns.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
            result = ex.unsubscribe("a.com", "r1", 1, http_url="https://unsub.a.com/u")
        assert not result.success
        assert result.error.startswith("Blocked unsafe unsubscribe URL: ")
        assert "10.0.0.5" in result.error


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

        server = FakeServer()
        with GmailExecutor(client, db, transport=httpx.MockTransport(server)) as ex:
            summary = execute_report(ex, db, report, on_result=lambda t, i, r: seen.append((t, i.get("domain") or i["group_id"])))

        assert summary.total_unsubscribed == 1
        assert [r.method for r in server.requests] == ["POST"]
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
