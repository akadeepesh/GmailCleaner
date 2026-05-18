"""
Tests for web/app.py (Flask dashboard)

Covers:
  - GET /health → 200 with status ok
  - GET / (dashboard) → 200, HTML contains expected content
  - GET /pending → 200, renders unsubscribe and delete sections
  - GET /stats → 200, renders triage counts
  - GET /history → 200, renders run table
  - POST /api/confirm → updates confirmed flag, returns JSON
  - POST /api/confirm missing fields → 400
  - POST /api/watch/renew missing topic → 400
  - Auth: DASHBOARD_SECRET set → requests without token rejected (401)
  - Auth: DASHBOARD_SECRET set → requests with correct token accepted
  - Auth: no secret set → all requests pass through
  - POST /webhook/pubsub → processes valid notification
  - POST /webhook/pubsub → 400 for empty body
  - POST /webhook/pubsub → malformed notification returns 200 (acknowledged)
"""

import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from web.app import create_app


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    """Create a test Flask app with a temp DB and no auth."""
    cfg = {
        "database": {"path": str(tmp_path / "test.db")},
        "triage": {"enabled": False},
        "digest": {"enabled": False},
        "pubsub": {"topic_name": ""},
        "scan": {"max_emails": 100, "lookback_days": 7, "query_filter": "in:inbox"},
        "unsubscribe": {"enabled": True, "min_email_count": 2},
        "bulk_cleanup": {"enabled": True, "min_group_size": 5},
        "confirm_first": True,
        "blocked_senders": [],
    }
    application = create_app(config=cfg)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def app_with_auth(tmp_path):
    """App with DASHBOARD_SECRET set."""
    cfg = {
        "database": {"path": str(tmp_path / "test.db")},
        "triage": {"enabled": False},
        "pubsub": {"topic_name": ""},
    }
    with patch.dict(os.environ, {"DASHBOARD_SECRET": "test-secret-xyz"}):
        application = create_app(config=cfg)
        application.config["TESTING"] = True
        yield application


# ── Health check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_returns_ok(self, client):
        data = resp = client.get("/health").get_json()
        assert data["status"] == "ok"

    def test_health_no_auth_required(self, app_with_auth):
        """Health check must work without auth (Cloud Run probe)."""
        c = app_with_auth.test_client()
        resp = c.get("/health")
        assert resp.status_code == 200


# ── Dashboard ─────────────────────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_dashboard_contains_gmailcleaner(self, client):
        resp = client.get("/")
        assert b"GmailCleaner" in resp.data

    def test_dashboard_contains_stats_section(self, client):
        resp = client.get("/")
        assert b"Needs Reply" in resp.data or b"Pending" in resp.data

    def test_dashboard_contains_watch_status(self, client):
        resp = client.get("/")
        assert b"Pub/Sub" in resp.data or b"watch" in resp.data.lower()


# ── Pending actions page ──────────────────────────────────────────────────────

class TestPendingPage:
    def test_pending_returns_200(self, client, tmp_path, app):
        # No pending_actions.json → should render empty state
        resp = client.get("/pending")
        assert resp.status_code == 200

    def test_pending_with_report_shows_unsubscribe(self, client, app, tmp_path):
        report = {
            "run_id": "r1",
            "unsubscribe": [
                {
                    "domain": "news.example.com",
                    "display_name": "Example Newsletter",
                    "sender_email": "hi@news.example.com",
                    "email_count": 10,
                    "most_recent_date": "2024-05-16",
                    "representative_subject": "Weekly update",
                    "method": "HTTP",
                    "http_url": "https://example.com/unsub",
                    "mailto": None,
                    "supports_one_click": False,
                    "sample_message_ids": [],
                    "confirmed": False,
                }
            ],
            "delete_groups": [],
            "triage": {},
            "summary": {},
        }
        # Write report to output/pending_actions.json
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        Path("output/pending_actions.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        with patch("web.app.load_pending_actions", return_value=report):
            resp = client.get("/pending")
        assert resp.status_code == 200
        assert b"Example Newsletter" in resp.data


# ── Stats page ────────────────────────────────────────────────────────────────

class TestStatsPage:
    def test_stats_returns_200(self, client):
        resp = client.get("/stats")
        assert resp.status_code == 200

    def test_stats_contains_triage_section(self, client):
        resp = client.get("/stats")
        assert b"Triage" in resp.data

    def test_stats_contains_sender_section(self, client):
        resp = client.get("/stats")
        assert b"Sender" in resp.data


# ── History page ──────────────────────────────────────────────────────────────

class TestHistoryPage:
    def test_history_returns_200(self, client):
        resp = client.get("/history")
        assert resp.status_code == 200

    def test_history_shows_empty_state(self, client):
        resp = client.get("/history")
        assert b"No runs" in resp.data or b"Run History" in resp.data

    def test_history_shows_run_data(self, client, app):
        # Insert a run into the DB
        with app.app_context():
            db = app.config["DB"]
            db.create_run("test-run-001", "scan")
            db.complete_run("test-run-001", 100, 5, 0, 0)
        resp = client.get("/history")
        assert resp.status_code == 200
        assert b"test-run-001" in resp.data


# ── API: confirm action ───────────────────────────────────────────────────────

class TestApiConfirm:
    def _report(self):
        return {
            "run_id": "r1",
            "unsubscribe": [
                {"domain": "a.com", "display_name": "A", "confirmed": False,
                 "sender_email": "x@a.com", "email_count": 5,
                 "most_recent_date": "2024-05-16", "representative_subject": "Hi",
                 "method": "HTTP", "http_url": "https://a.com/u",
                 "mailto": None, "supports_one_click": False, "sample_message_ids": []}
            ],
            "delete_groups": [],
            "triage": {},
            "summary": {},
        }

    def test_confirm_returns_200(self, client):
        with patch("web.app.load_pending_actions", return_value=self._report()):
            with patch("pathlib.Path.write_text"):
                resp = client.post(
                    "/api/confirm",
                    json={"action_type": "unsubscribe", "target": "a.com", "confirmed": True},
                )
        assert resp.status_code == 200

    def test_confirm_returns_ok_status(self, client):
        with patch("web.app.load_pending_actions", return_value=self._report()):
            with patch("pathlib.Path.write_text"):
                resp = client.post(
                    "/api/confirm",
                    json={"action_type": "unsubscribe", "target": "a.com", "confirmed": True},
                )
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["confirmed"] is True

    def test_confirm_missing_fields_returns_400(self, client):
        resp = client.post("/api/confirm", json={"action_type": "unsubscribe"})
        assert resp.status_code == 400

    def test_confirm_unknown_target_returns_404(self, client):
        with patch("web.app.load_pending_actions", return_value=self._report()):
            resp = client.post(
                "/api/confirm",
                json={"action_type": "unsubscribe", "target": "unknown.com", "confirmed": True},
            )
        assert resp.status_code == 404

    def test_confirm_no_report_returns_404(self, client):
        with patch("web.app.load_pending_actions", return_value=None):
            resp = client.post(
                "/api/confirm",
                json={"action_type": "unsubscribe", "target": "a.com", "confirmed": True},
            )
        assert resp.status_code == 404


# ── API: watch renew ──────────────────────────────────────────────────────────

class TestApiWatchRenew:
    def test_renew_no_topic_returns_400(self, client):
        resp = client.post("/api/watch/renew")
        assert resp.status_code == 400

    def test_renew_with_topic_calls_register(self, client, app):
        app.config["GMAILCLEANER"]["pubsub"] = {"topic_name": "projects/p/topics/t"}
        with patch("web.app.get_credentials"):
            with patch("web.app.GmailClient"):
                with patch("web.app.WatchManager") as mock_wm:
                    mock_wm.return_value.register.return_value = MagicMock(
                        history_id="1234", expiration_ms="9999999999000"
                    )
                    resp = client.post("/api/watch/renew")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "renewed"


# ── Webhook ───────────────────────────────────────────────────────────────────

class TestWebhookPubsub:
    def _body(self, email: str = "u@gmail.com", history_id: str = "1000") -> dict:
        payload = json.dumps({"emailAddress": email, "historyId": history_id})
        data = base64.b64encode(payload.encode()).decode()
        return {"message": {"data": data, "messageId": "x"}, "subscription": "s"}

    def test_empty_body_returns_400(self, client):
        resp = client.post("/webhook/pubsub", content_type="application/json", data="")
        assert resp.status_code == 400

    def test_non_json_returns_400(self, client):
        resp = client.post("/webhook/pubsub", data="plain text",
                           content_type="text/plain")
        assert resp.status_code == 400

    def test_malformed_notification_returns_200_acknowledged(self, client):
        # Malformed but valid JSON → should acknowledge (200), not fail
        resp = client.post(
            "/webhook/pubsub",
            json={"message": {"data": "!!!bad!!!", "messageId": "x"}},
        )
        assert resp.status_code == 200

    def test_valid_notification_processed(self, client):
        mock_handler = MagicMock()
        mock_handler.handle.return_value = {"status": "processed", "new_messages": 2}

        with patch("web.app.get_credentials"):
            with patch("web.app.GmailClient"):
                with patch("web.app.WebhookHandler", return_value=mock_handler):
                    with patch("web.app.UnsubscribeEngine"):
                        resp = client.post(
                            "/webhook/pubsub",
                            json=self._body(),
                        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "processed"

    def test_webhook_exception_returns_200_not_crash(self, client):
        """Webhook must always return 200 to prevent Pub/Sub infinite retry."""
        with patch("web.app.get_credentials", side_effect=Exception("auth failed")):
            resp = client.post("/webhook/pubsub", json=self._body())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "error"


# ── Auth ──────────────────────────────────────────────────────────────────────

class TestDashboardAuth:
    def test_no_secret_allows_all(self, client):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHBOARD_SECRET", None)
            resp = client.get("/")
        assert resp.status_code == 200

    def test_with_secret_no_token_returns_401(self, app_with_auth):
        c = app_with_auth.test_client()
        with patch.dict(os.environ, {"DASHBOARD_SECRET": "test-secret-xyz"}):
            resp = c.get("/")
        assert resp.status_code == 401

    def test_with_secret_correct_token_passes(self, app_with_auth):
        c = app_with_auth.test_client()
        with patch.dict(os.environ, {"DASHBOARD_SECRET": "test-secret-xyz"}):
            resp = c.get("/?token=test-secret-xyz")
        assert resp.status_code == 200

    def test_with_secret_wrong_token_returns_401(self, app_with_auth):
        c = app_with_auth.test_client()
        with patch.dict(os.environ, {"DASHBOARD_SECRET": "test-secret-xyz"}):
            resp = c.get("/?token=wrong")
        assert resp.status_code == 401

    def test_bearer_token_accepted(self, app_with_auth):
        c = app_with_auth.test_client()
        with patch.dict(os.environ, {"DASHBOARD_SECRET": "test-secret-xyz"}):
            resp = c.get("/", headers={"Authorization": "Bearer test-secret-xyz"})
        assert resp.status_code == 200