"""
Tests for pubsub/webhook.py

Covers:
  - PubSubNotification.from_request_body: valid, malformed, missing fields
  - base64 padding edge cases in notification decode
  - WebhookHandler.handle: baseline set on first call
  - WebhookHandler.handle: already-processed (historyId not newer) skipped
  - WebhookHandler.handle: new messages fetched and processed
  - WebhookHandler.handle: historyId advanced after processing
  - WebhookHandler.handle: 404 history (too old) returns gracefully
  - verify_pubsub_token: missing header returns False
  - verify_pubsub_token: malformed header returns False
  - DB interaction: record_inbox_event called with correct counts
  - Unsub engine and triage engine called when messages are new
"""

import base64
import json
from unittest.mock import MagicMock, patch, call

import pytest

from pubsub.webhook import PubSubNotification, WebhookHandler, verify_pubsub_token


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encode_data(email: str, history_id: str) -> str:
    payload = json.dumps({"emailAddress": email, "historyId": history_id})
    return base64.b64encode(payload.encode()).decode()


def _make_pubsub_body(email: str = "user@gmail.com", history_id: str = "1000") -> dict:
    return {
        "message": {
            "data": _encode_data(email, history_id),
            "messageId": "msg-abc",
            "publishTime": "2024-05-16T00:00:00Z",
        },
        "subscription": "projects/proj/subscriptions/sub",
    }


def _make_handler(
    last_history_id: str | None = "900",
    fetch_messages: list | None = None,
) -> tuple[WebhookHandler, MagicMock, MagicMock, MagicMock]:
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_unsub = MagicMock()
    mock_triage = MagicMock()

    mock_db.get_last_history_id.return_value = last_history_id
    mock_client.list_history.return_value = fetch_messages or []
    mock_unsub.get_new_candidates.return_value = []
    mock_triage.classify.return_value = MagicMock(
        needs_reply=[], promotions=[], junk=[]
    )

    handler = WebhookHandler(
        client=mock_client,
        db=mock_db,
        unsub_engine=mock_unsub,
        triage_engine=mock_triage,
    )
    return handler, mock_client, mock_db, mock_unsub


# ── PubSubNotification ────────────────────────────────────────────────────────

class TestPubSubNotification:
    def test_valid_body(self):
        body = _make_pubsub_body("me@gmail.com", "1234")
        notif = PubSubNotification.from_request_body(body)
        assert notif.email_address == "me@gmail.com"
        assert notif.history_id == "1234"
        assert notif.raw_message_id == "msg-abc"

    def test_missing_message_key_raises(self):
        with pytest.raises(ValueError, match="Malformed"):
            PubSubNotification.from_request_body({})

    def test_missing_data_key_raises(self):
        body = {"message": {"messageId": "x"}}
        with pytest.raises(ValueError):
            PubSubNotification.from_request_body(body)

    def test_invalid_base64_raises(self):
        body = {"message": {"data": "!!!not-base64!!!", "messageId": "x"}}
        with pytest.raises(ValueError):
            PubSubNotification.from_request_body(body)

    def test_missing_email_address_raises(self):
        payload = json.dumps({"historyId": "123"})
        data = base64.b64encode(payload.encode()).decode()
        body = {"message": {"data": data, "messageId": "x"}}
        with pytest.raises(ValueError):
            PubSubNotification.from_request_body(body)

    def test_base64_padding_handled(self):
        # Some Pub/Sub implementations omit padding — ensure we handle it
        payload = json.dumps({"emailAddress": "a@b.com", "historyId": "1"})
        data = base64.b64encode(payload.encode()).decode().rstrip("=")
        body = {"message": {"data": data, "messageId": "x"}}
        notif = PubSubNotification.from_request_body(body)
        assert notif.email_address == "a@b.com"

    def test_history_id_stored_as_string(self):
        body = _make_pubsub_body(history_id="99999")
        notif = PubSubNotification.from_request_body(body)
        assert isinstance(notif.history_id, str)

    def test_integer_history_id_in_payload(self):
        payload = json.dumps({"emailAddress": "x@y.com", "historyId": 42})
        data = base64.b64encode(payload.encode()).decode()
        body = {"message": {"data": data, "messageId": "x"}}
        notif = PubSubNotification.from_request_body(body)
        assert notif.history_id == "42"


# ── WebhookHandler ────────────────────────────────────────────────────────────

class TestWebhookHandler:
    def _notif(self, history_id: str = "1000") -> PubSubNotification:
        return PubSubNotification(
            email_address="user@gmail.com",
            history_id=history_id,
            raw_message_id="msg-1",
        )

    def test_no_baseline_sets_baseline_and_returns(self):
        handler, client, db, unsub = _make_handler(last_history_id=None)
        result = handler.handle(self._notif("1000"))
        assert result["status"] == "baseline_set"
        db.save_history_id.assert_called_once_with("1000")
        client.list_history.assert_not_called()

    def test_already_processed_skipped(self):
        # Incoming historyId (900) <= stored (1000) → skip
        handler, client, db, unsub = _make_handler(last_history_id="1000")
        result = handler.handle(self._notif("900"))
        assert result["status"] == "already_processed"
        client.list_history.assert_not_called()

    def test_same_history_id_skipped(self):
        handler, client, db, unsub = _make_handler(last_history_id="1000")
        result = handler.handle(self._notif("1000"))
        assert result["status"] == "already_processed"

    def test_no_new_messages_advances_history_id(self):
        handler, client, db, unsub = _make_handler(
            last_history_id="900", fetch_messages=[]
        )
        result = handler.handle(self._notif("1001"))
        assert result["status"] == "no_new_messages"
        db.save_history_id.assert_called_once_with("1001")

    def test_new_messages_processed(self):
        from gmail.parser import GmailMessage
        from datetime import datetime, timezone

        msg = MagicMock(spec=GmailMessage)
        msg.id = "m1"
        msg.sender_domain = "example.com"
        msg.sender_name = "Example"

        handler, client, db, unsub = _make_handler(
            last_history_id="900", fetch_messages=[{"id": "m1", "threadId": "t1"}]
        )
        # parse_messages will be called internally — patch it to return our mock
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            result = handler.handle(self._notif("1001"))

        assert result["status"] == "processed"
        assert result["new_messages"] == 1
        db.save_history_id.assert_called_once_with("1001")

    def test_unsub_engine_called_with_new_messages(self):
        from unittest.mock import MagicMock
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(last_history_id="900", fetch_messages=[{"id": "m1", "threadId": "t1"}])
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            handler.handle(self._notif("1001"))

        unsub.get_new_candidates.assert_called_once()

    def test_triage_engine_called_with_new_messages(self):
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(last_history_id="900", fetch_messages=[{"id": "m1", "threadId": "t1"}])
        mock_triage = handler._triage_engine
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            handler.handle(self._notif("1001"))

        mock_triage.classify.assert_called_once()

    def test_no_triage_engine_still_processes(self):
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.get_last_history_id.return_value = "900"
        mock_client.list_history.return_value = [{"id": "m1", "threadId": "t1"}]
        mock_unsub = MagicMock()
        mock_unsub.get_new_candidates.return_value = []

        handler = WebhookHandler(
            client=mock_client,
            db=mock_db,
            unsub_engine=mock_unsub,
            triage_engine=None,  # No triage engine
        )
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            result = handler.handle(
                PubSubNotification("u@g.com", "1001", "m1")
            )
        assert result["status"] == "processed"
        assert result["needs_reply"] == 0

    def test_record_inbox_event_called(self):
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(last_history_id="900", fetch_messages=[{"id": "m1", "threadId": "t1"}])
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            handler.handle(self._notif("1001"))

        db.record_inbox_event.assert_called_once()

    def test_404_history_returns_empty_gracefully(self):
        from googleapiclient.errors import HttpError
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.reason = "Not Found"

        handler, client, db, unsub = _make_handler(last_history_id="100")
        client.list_history.side_effect = HttpError(resp=mock_resp, content=b"not found")
        result = handler.handle(self._notif("9999"))
        # Should return no_new_messages gracefully, not crash
        assert result["status"] == "no_new_messages"

    def test_metadata_fetched_for_history_ids(self):
        """history.list returns IDs only — headers must be fetched before parsing."""
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(
            last_history_id="900",
            fetch_messages=[{"id": "m1"}, {"id": "m2"}, {"id": "m1"}],
        )
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            handler.handle(self._notif("1001"))

        args, kwargs = client.batch_get_messages.call_args
        assert args[0] == ["m1", "m2"]  # de-duplicated, order preserved
        assert kwargs["format"] == "metadata"

    def test_sender_counts_incremented_not_overwritten(self):
        msgs = []
        for _ in range(3):
            m = MagicMock()
            m.sender_domain = "x.com"
            m.sender_name = "X"
            msgs.append(m)

        handler, client, db, unsub = _make_handler(
            last_history_id="900", fetch_messages=[{"id": "m1"}]
        )
        with patch("pubsub.webhook.parse_messages", return_value=msgs):
            handler.handle(self._notif("1001"))

        db.increment_sender.assert_called_once_with(domain="x.com", display_name="X", count=3)
        db.upsert_sender.assert_not_called()

    def test_candidates_merged_into_pending_report(self):
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(
            last_history_id="900", fetch_messages=[{"id": "m1"}]
        )
        handler._pending_actions_path = "some/path.json"
        candidate = MagicMock()
        unsub.get_new_candidates.return_value = [candidate]

        with patch("pubsub.webhook.parse_messages", return_value=[msg]),              patch("pubsub.webhook.merge_unsubscribe_candidates") as mock_merge:
            handler.handle(self._notif("1001"))

        mock_merge.assert_called_once_with([candidate], "some/path.json")

    def test_history_id_advanced_after_processing(self):
        msg = MagicMock()
        msg.sender_domain = "x.com"
        msg.sender_name = "X"

        handler, client, db, unsub = _make_handler(last_history_id="900", fetch_messages=[{"id": "m1", "threadId": "t1"}])
        with patch("pubsub.webhook.parse_messages", return_value=[msg]):
            handler.handle(self._notif("2500"))

        db.save_history_id.assert_called_with("2500")


# ── JWT verification ──────────────────────────────────────────────────────────

class TestVerifyPubsubToken:
    def test_missing_header_returns_false(self):
        assert verify_pubsub_token(None, "https://example.com") is False

    def test_empty_header_returns_false(self):
        assert verify_pubsub_token("", "https://example.com") is False

    def test_no_bearer_prefix_returns_false(self):
        assert verify_pubsub_token("sometoken", "https://example.com") is False

    def test_valid_token_calls_google_verify(self):
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value={"sub": "12345"}):
            with patch("google.auth.transport.requests.Request"):
                result = verify_pubsub_token("Bearer valid-token", "https://example.com")
                assert result is True

    def test_invalid_token_returns_false(self):
        with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=Exception("invalid")):
            with patch("google.auth.transport.requests.Request"):
                result = verify_pubsub_token("Bearer bad-token", "https://example.com")
                assert result is False