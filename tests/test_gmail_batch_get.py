"""
Tests for GmailClient.batch_get_messages rate-limit handling.

Gmail reports per-user quota exhaustion as HTTP 403 rateLimitExceeded as
well as 429. Both must be retried, while other errors are dropped.
"""

import json
from unittest.mock import MagicMock, patch

import httplib2
from googleapiclient.errors import HttpError

from gmail.client import GmailClient, _is_rate_limited


def _http_error(status: int, reason: str = "", message: str = "error") -> HttpError:
    errors = [{"message": message, "domain": "usageLimits", "reason": reason}] if reason else []
    body = {"error": {"code": status, "message": message, "errors": errors}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


class _FakeBatch:
    """Stands in for a BatchHttpRequest: answers each request via `respond(msg_id)`."""

    def __init__(self, callback, respond):
        self._callback = callback
        self._respond = respond
        self._ids: list[str] = []

    def add(self, _request, request_id):
        self._ids.append(request_id)

    def execute(self):
        for msg_id in self._ids:
            outcome = self._respond(msg_id)
            if isinstance(outcome, Exception):
                self._callback(msg_id, None, outcome)
            else:
                self._callback(msg_id, outcome, None)


def _client(respond) -> GmailClient:
    client = GmailClient.__new__(GmailClient)
    client._user_id = "me"
    service = MagicMock()
    service.new_batch_http_request.side_effect = lambda callback: _FakeBatch(callback, respond)
    client._service = service
    return client


class TestIsRateLimited:
    def test_429_is_rate_limited(self):
        assert _is_rate_limited(_http_error(429))

    def test_403_rate_limit_exceeded_is_rate_limited(self):
        assert _is_rate_limited(_http_error(403, "rateLimitExceeded", "Quota exceeded"))

    def test_403_user_rate_limit_exceeded_is_rate_limited(self):
        assert _is_rate_limited(_http_error(403, "userRateLimitExceeded"))

    def test_403_permission_denied_is_not_rate_limited(self):
        assert not _is_rate_limited(_http_error(403, "insufficientPermissions"))

    def test_404_is_not_rate_limited(self):
        assert not _is_rate_limited(_http_error(404, "notFound"))

    def test_non_http_error_is_not_rate_limited(self):
        assert not _is_rate_limited(ValueError("boom"))


@patch("time.sleep")
class TestBatchGetRetries:
    def test_403_quota_error_is_retried_then_succeeds(self, sleep):
        calls: dict[str, int] = {}

        def respond(msg_id):
            calls[msg_id] = calls.get(msg_id, 0) + 1
            if msg_id == "m2" and calls[msg_id] == 1:
                return _http_error(403, "rateLimitExceeded", "Quota exceeded")
            return {"id": msg_id}

        result = _client(respond).batch_get_messages(["m1", "m2", "m3"])

        assert [m["id"] for m in result] == ["m1", "m2", "m3"]
        assert calls == {"m1": 1, "m2": 2, "m3": 1}
        sleep.assert_called_once_with(2.0)

    def test_permanent_errors_are_not_retried(self, sleep):
        calls: list[str] = []

        def respond(msg_id):
            calls.append(msg_id)
            return _http_error(404, "notFound") if msg_id == "gone" else {"id": msg_id}

        result = _client(respond).batch_get_messages(["ok", "gone"])

        assert [m["id"] for m in result] == ["ok"]
        assert calls == ["ok", "gone"]
        sleep.assert_not_called()

    def test_gives_up_without_sleeping_after_last_attempt(self, sleep):
        result = _client(lambda _id: _http_error(429)).batch_get_messages(["m1"])

        assert result == []
        # Sleeps between attempts only: 2, 4, 8, 16, 32 (none after the 6th).
        assert [c.args[0] for c in sleep.call_args_list] == [2.0, 4.0, 8.0, 16.0, 32.0]
