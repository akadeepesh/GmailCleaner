"""
Tests for the desktop-mode dashboard: security, accounts, sign-in, settings,
scan/execute jobs (web/common.py, web/accounts.py, web/auth_routes.py,
web/settings_routes.py and the new routes in web/app.py).

Gmail, the OAuth flow, keyring and HTTP are mocked; everything else is real.

Covers:
  - Desktop mode requires DASHBOARD_SECRET; create_app() works with no home
  - Host allowlist (DNS rebinding) and the X-GC-Request / JSON CSRF checks
  - The webhook is exempt from the CSRF header; locked page without a cookie
  - Account naming, listing, switching cookies, unknown accounts ignored
  - Sign-in: state mismatch → 400, success saves the token and picks the
    account, missing scopes rejected, Cloud Run mode → 404
  - Sign out revokes and deletes; remove data needs explicit confirmation
  - Settings: whitelist, ranges, bare "*" rule, confirm_first round-trip and
    context invalidation, Gemini key never echoed, credentials validation,
    read-only when headless
  - POST /api/scan and background /api/execute run as jobs
"""

import json
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auth.oauth import SCOPES
from gmail.parser import GmailMessage, UnsubscribeTarget
from web import auth_routes
from web.accounts import LEGACY_KEY, account_name_for_email
from web.app import create_app
from web.common import host_allowed

SECRET = "desktop-secret-123"
H = {"X-GC-Request": "1"}

_TOKEN = {
    "token": "access",
    "refresh_token": "refresh",
    "client_id": "id.apps.googleusercontent.com",
    "client_secret": "secret",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": SCOPES,
}
_CLIENT = {"installed": {
    "client_id": "123-abc.apps.googleusercontent.com",
    "client_secret": "s",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}}


class FakeKeyring(types.ModuleType):
    def __init__(self):
        super().__init__("keyring")
        self.store = {}

    def get_password(self, s, u):
        return self.store.get((s, u))

    def set_password(self, s, u, v):
        self.store[(s, u)] = v

    def delete_password(self, s, u):
        self.store.pop((s, u), None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("GMAILCLEANER_ACCOUNT", "GMAILCLEANER_HEADLESS", "K_SERVICE", "CLOUD_RUN_JOB",
                "DATABASE_PATH", "PENDING_ACTIONS_PATH", "GOOGLE_TOKEN_PATH", "GOOGLE_TOKEN_JSON",
                "GOOGLE_CREDENTIALS_PATH", "GEMINI_API_KEY", "GMAILCLEANER_PORT", "DIGEST_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    h = tmp_path / "app home"
    monkeypatch.setenv("GMAILCLEANER_HOME", str(h))
    monkeypatch.setenv("DASHBOARD_SECRET", SECRET)
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
    return h


def _raw_config() -> dict:
    return {
        "triage": {"enabled": False, "model": "gemini-3.5-flash", "confidence_threshold": 0.75},
        "digest": {"enabled": False},
        "pubsub": {"topic_name": ""},
        "scan": {"max_emails": 100, "lookback_days": 7, "query_filter": "in:inbox"},
        "unsubscribe": {"enabled": True, "min_email_count": 2},
        "bulk_cleanup": {"enabled": True, "min_group_size": 5},
        "confirm_first": True,
        "trusted_rules": [],
        "blocked_senders": [],
    }


@pytest.fixture
def app(home):
    application = create_app(config=_raw_config())
    application.config["TESTING"] = True
    yield application
    application.config["JOBS"].shutdown(timeout=5)


@pytest.fixture
def client(app):
    c = app.test_client()
    c.set_cookie("gc_token", SECRET)
    return c


def add_token(home: Path, name=None, data=None) -> Path:
    path = home / "token.json" if name is None else home / "tokens" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data or _TOKEN), encoding="utf-8")
    return path


# ── App factory ───────────────────────────────────────────────────────────────

class TestFactory:
    def test_desktop_requires_secret(self, home, monkeypatch):
        monkeypatch.delenv("DASHBOARD_SECRET")
        with pytest.raises(RuntimeError):
            create_app(config=_raw_config())

    def test_create_app_without_home(self, monkeypatch, tmp_path):
        for var in ("GMAILCLEANER_HOME", "DASHBOARD_SECRET", "GMAILCLEANER_ACCOUNT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
        app = create_app()
        assert app.config["DESKTOP"] is False
        assert app.test_client().get("/health").status_code == 200

    def test_templates_from_resource_path(self, app):
        assert Path(app.template_folder).joinpath("base.html").exists()


# ── Security ──────────────────────────────────────────────────────────────────

class TestSecurity:
    @pytest.mark.parametrize("host,port,ok", [
        ("127.0.0.1:8765", None, True),
        ("localhost:8765", None, True),
        ("localhost", None, True),
        ("127.0.0.1:8765", 8765, True),
        ("127.0.0.1:9999", 8765, False),
        ("evil.com:8765", None, False),
        ("127.0.0.1.evil.com", None, False),
        ("", None, False),
    ])
    def test_host_allowed(self, host, port, ok):
        assert host_allowed(host, port) is ok

    def test_foreign_host_rejected(self, client):
        assert client.get("/", headers={"Host": "attacker.example:80"}).status_code == 400

    def test_port_enforced(self, home, monkeypatch):
        monkeypatch.setenv("GMAILCLEANER_PORT", "8765")
        c = create_app(config=_raw_config()).test_client()
        auth = {"Authorization": f"Bearer {SECRET}"}
        assert c.get("/", headers={"Host": "127.0.0.1:9999", **auth}).status_code == 400
        assert c.get("/", headers={"Host": "127.0.0.1:8765", **auth}).status_code == 200

    def test_post_without_header_forbidden(self, client):
        assert client.post("/api/confirm", json={}).status_code == 403
        assert client.post("/api/scan", json={}).status_code == 403

    def test_post_form_forbidden_even_with_header(self, client):
        resp = client.post("/api/settings", data="confirm_first=false", headers=H,
                           content_type="application/x-www-form-urlencoded")
        assert resp.status_code == 403

    def test_post_without_secret_unauthorized(self, app):
        c = app.test_client()
        assert c.post("/api/scan", json={}, headers=H).status_code == 401

    def test_locked_page_without_cookie(self, app):
        resp = app.test_client().get("/settings")
        assert resp.status_code == 401
        assert b"Open GmailCleaner from the tray" in resp.data
        assert b"Settings" not in resp.data  # nothing leaks

    def test_webhook_exempt_from_header(self, client):
        # Reaches the handler (400 = empty body), not the CSRF check (403).
        assert client.post("/webhook/pubsub", data="", content_type="application/json").status_code == 400

    def test_token_cookie_strict(self, app):
        resp = app.test_client().get(f"/?token={SECRET}")
        cookie = resp.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


# ── Accounts ──────────────────────────────────────────────────────────────────

class TestAccounts:
    @pytest.mark.parametrize("email,name", [
        ("Jane.Doe@gmail.com", "jane.doe_gmail.com"),
        ("a+tag@x.co.uk", "a_tag_x.co.uk"),
        ("_x@y.com", "x_y.com"),
    ])
    def test_account_name(self, email, name):
        assert account_name_for_email(email) == name

    def test_account_name_rejects_garbage(self):
        with pytest.raises(ValueError):
            account_name_for_email("@@@")

    def test_list_accounts(self, app, home):
        reg = app.config["ACCOUNTS"]
        assert reg.list_accounts() == []
        add_token(home)
        add_token(home, "jane_gmail.com")
        add_token(home, "bad_token.com", {"nope": 1})
        reg.remember_email("jane_gmail.com", "jane@gmail.com")
        accounts = {a.key: a for a in reg.list_accounts()}
        assert set(accounts) == {LEGACY_KEY, "jane_gmail.com", "bad_token.com"}
        assert accounts["jane_gmail.com"].label == "jane@gmail.com"
        assert accounts["bad_token.com"].status == "needs_sign_in"
        assert (home / "tokens" / "_index.json").exists()  # not listed as an account

    def test_first_signed_in_account_is_default(self, client, home):
        add_token(home, "jane_gmail.com")
        resp = client.get("/")
        assert b"jane_gmail.com" in resp.data

    def test_switch_sets_cookie(self, client, home, app):
        add_token(home, "a_x.com")
        add_token(home, "b_x.com")
        resp = client.post("/api/account/switch", json={"account": "b_x.com"}, headers=H)
        assert resp.status_code == 200
        assert "gc_account=b_x.com" in resp.headers["Set-Cookie"]
        page = client.get("/settings")
        assert b'value="b_x.com" selected' in page.data

    def test_switch_unknown_account(self, client):
        assert client.post("/api/account/switch", json={"account": "../x"}, headers=H).status_code == 404

    def test_unknown_cookie_ignored(self, client, home, app):
        add_token(home, "a_x.com")
        client.set_cookie("gc_account", "ghost_x.com")
        client.get("/")
        assert not (home / "data" / "ghost_x.com").exists()

    def test_accounts_are_isolated(self, client, home, app):
        add_token(home, "a_x.com")
        add_token(home, "b_x.com")
        reg = app.config["ACCOUNTS"]
        reg.get("b_x.com").db.upsert_sender("only-b.example", "B", 3)
        client.post("/api/account/switch", json={"account": "a_x.com"}, headers=H)
        assert b"only-b.example" not in client.get("/export/senders.csv").data
        client.post("/api/account/switch", json={"account": "b_x.com"}, headers=H)
        assert b"only-b.example" in client.get("/export/senders.csv").data

    def test_pinned_account(self, home, monkeypatch):
        monkeypatch.setenv("GMAILCLEANER_ACCOUNT", "work")
        app = create_app(config=_raw_config())
        assert app.config["ACCOUNTS"].get(None).name == "work"
        c = app.test_client()
        c.set_cookie("gc_token", SECRET)
        assert c.post("/api/account/switch", json={"account": "x"}, headers=H).status_code == 400

    def test_accounts_page(self, client, home):
        add_token(home, "jane_gmail.com")
        resp = client.get("/accounts")
        assert resp.status_code == 200
        assert b"Add a Gmail account" in resp.data
        assert b"Signed in" in resp.data


# ── Sign-in ───────────────────────────────────────────────────────────────────

def _flow(granted=None):
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?x=1", "STATE123")
    flow.code_verifier = "VERIFIER"
    creds = MagicMock()
    creds.to_json.return_value = json.dumps(_TOKEN)
    creds.granted_scopes = granted if granted is not None else SCOPES
    flow.credentials = creds
    return flow


class TestSignIn:
    @pytest.fixture(autouse=True)
    def client_config(self):
        with patch.object(auth_routes, "resolve_client_config", return_value=_CLIENT):
            yield

    def _start(self, client, flow):
        with patch.object(auth_routes, "build_flow", return_value=flow) as build:
            resp = client.get("/auth/start")
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith("https://accounts.google.com/")
        assert build.call_args.args[1] == "http://localhost/auth/callback"
        return build

    def test_no_client_config(self, client):
        with patch.object(auth_routes, "resolve_client_config", return_value=None):
            resp = client.get("/auth/start")
        assert resp.status_code == 200
        assert b"credentials.json" in resp.data

    def test_state_mismatch(self, client):
        flow = _flow()
        self._start(client, flow)
        with patch.object(auth_routes, "build_flow", return_value=flow):
            resp = client.get("/auth/callback?state=WRONG&code=abc")
        assert resp.status_code == 400
        flow.fetch_token.assert_not_called()

    def test_callback_without_session(self, app):
        resp = app.test_client().get("/auth/callback?state=STATE123&code=abc")
        assert resp.status_code == 400

    def test_success_saves_token_and_selects_account(self, client, home):
        flow = _flow()
        self._start(client, flow)
        with patch.object(auth_routes, "build_flow", return_value=flow) as build, \
             patch.object(auth_routes, "fetch_profile_email", return_value="Jane.Doe@gmail.com"):
            resp = client.get("/auth/callback?state=STATE123&code=abc")
        assert resp.status_code == 200
        assert b"/?welcome=1" in resp.data            # first account → welcome
        assert "gc_account=jane.doe_gmail.com" in resp.headers["Set-Cookie"]
        flow.fetch_token.assert_called_once_with(code="abc")
        assert build.call_args.kwargs["code_verifier"] == "VERIFIER"
        token = home / "tokens" / "jane.doe_gmail.com.json"
        assert json.loads(token.read_text(encoding="utf-8"))["refresh_token"] == "refresh"
        # State is single-use.
        with patch.object(auth_routes, "build_flow", return_value=flow):
            assert client.get("/auth/callback?state=STATE123&code=abc").status_code == 400

    def test_second_account_goes_to_accounts(self, client, home):
        add_token(home, "first_x.com")
        flow = _flow()
        self._start(client, flow)
        with patch.object(auth_routes, "build_flow", return_value=flow), \
             patch.object(auth_routes, "fetch_profile_email", return_value="two@x.com"):
            resp = client.get("/auth/callback?state=STATE123&code=abc")
        assert b"/accounts" in resp.data and b"welcome" not in resp.data

    def test_missing_scopes_rejected(self, client, home):
        flow = _flow(granted=[SCOPES[0]])
        self._start(client, flow)
        with patch.object(auth_routes, "build_flow", return_value=flow), \
             patch.object(auth_routes, "fetch_profile_email", return_value="a@x.com"):
            resp = client.get("/auth/callback?state=STATE123&code=abc")
        assert resp.status_code == 400
        assert not (home / "tokens").exists()

    def test_user_denied(self, client):
        flow = _flow()
        self._start(client, flow)
        resp = client.get("/auth/callback?state=STATE123&error=access_denied")
        assert resp.status_code == 400
        assert b"cancelled" in resp.data

    def test_exchange_failure(self, client):
        flow = _flow()
        flow.fetch_token.side_effect = RuntimeError("invalid_grant")
        self._start(client, flow)
        with patch.object(auth_routes, "build_flow", return_value=flow):
            resp = client.get("/auth/callback?state=STATE123&code=abc")
        assert resp.status_code == 400
        assert b"invalid_grant" not in resp.data

    def test_cloud_run_404(self, home, monkeypatch):
        monkeypatch.setenv("K_SERVICE", "dashboard")
        monkeypatch.delenv("GMAILCLEANER_HOME")
        app = create_app(config=_raw_config())
        c = app.test_client()
        c.set_cookie("gc_token", SECRET)
        assert c.get("/auth/start").status_code == 404
        assert c.get("/auth/callback?state=x&code=y").status_code == 404
        assert c.post("/api/accounts/signout", json={"account": LEGACY_KEY}).status_code == 404


class TestSignOutAndRemove:
    def test_sign_out(self, client, home, app):
        token = add_token(home, "jane_gmail.com")
        with patch("auth.oauth.httpx.post", return_value=MagicMock(status_code=200)) as post:
            resp = client.post("/api/accounts/signout", json={"account": "jane_gmail.com"}, headers=H)
        assert resp.status_code == 200
        assert post.call_args.kwargs["data"] == {"token": "refresh"}
        assert not token.exists()

    def test_remove_requires_confirmation(self, client, home):
        add_token(home, "jane_gmail.com")
        resp = client.post("/api/accounts/remove", json={"account": "jane_gmail.com"}, headers=H)
        assert resp.status_code == 400
        assert (home / "tokens" / "jane_gmail.com.json").exists()

    def test_remove_deletes_data(self, client, home, app):
        add_token(home, "jane_gmail.com")
        add_token(home, "keep_x.com")
        app.config["ACCOUNTS"].get("jane_gmail.com").db.upsert_sender("a.com", "A", 1)
        app.config["ACCOUNTS"].get("keep_x.com").db.upsert_sender("b.com", "B", 1)
        data_dir = home / "data" / "jane_gmail.com"
        assert data_dir.exists()
        with patch("auth.oauth.httpx.post", return_value=MagicMock(status_code=200)):
            resp = client.post("/api/accounts/remove",
                               json={"account": "jane_gmail.com", "confirm": "REMOVE"}, headers=H)
        assert resp.status_code == 200, resp.get_json()
        assert not data_dir.exists()
        assert (home / "data" / "keep_x.com").exists()
        assert not (home / "tokens" / "jane_gmail.com.json").exists()


# ── Settings ──────────────────────────────────────────────────────────────────

class TestSettingsRoutes:
    def test_page_renders_values(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Ask me before doing anything" in resp.data
        assert b'data-key="scan.lookback_days"' in resp.data

    def test_unknown_key_rejected(self, client, home):
        resp = client.post("/api/settings", json={"database.path": "/tmp/x"}, headers=H)
        assert resp.status_code == 400
        assert "database.path" in resp.get_json()["errors"]
        assert not (home / "settings.user.yaml").exists()

    def test_range_error(self, client):
        resp = client.post("/api/settings", json={"scan.max_emails": 999999}, headers=H)
        assert resp.status_code == 400
        assert "at most" in resp.get_json()["errors"]["scan.max_emails"]

    def test_bare_wildcard_rule_rejected(self, client):
        resp = client.post("/api/settings",
                           json={"trusted_rules": [{"action": "both", "domain": "*"}]}, headers=H)
        assert resp.status_code == 400
        assert "trusted_rules" in resp.get_json()["errors"]

    def test_confirm_first_round_trip_applies_without_restart(self, client, app):
        assert app.config["ACCOUNTS"].get(None).cfg["confirm_first"] is True
        resp = client.post("/api/settings", json={
            "confirm_first": False,
            "trusted_rules": [{"action": "delete", "domain": "news.example.com"}],
        }, headers=H)
        assert resp.status_code == 200
        assert resp.get_json()["values"]["confirm_first"] is False
        cfg = app.config["ACCOUNTS"].get(None).cfg
        assert cfg["confirm_first"] is False
        assert cfg["trusted_rules"] == [{"action": "delete", "domain": "news.example.com"}]
        assert app.config["GMAILCLEANER"]["confirm_first"] is False
        tag = re.search(r'<input[^>]*id="confirm-first"[^>]*>', client.get("/settings").get_data(as_text=True))
        assert tag and "checked" not in tag.group(0)
        client.post("/api/settings", json={"confirm_first": True}, headers=H)
        assert app.config["ACCOUNTS"].get(None).cfg["confirm_first"] is True

    def test_gemini_key_never_echoed(self, client):
        key = "AIzaSy-super-secret-value-WXYZ"
        resp = client.post("/api/settings/gemini-key", json={"key": key}, headers=H)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "super-secret" not in body and resp.get_json()["last4"] == "WXYZ"
        page = client.get("/settings").get_data(as_text=True)
        assert "super-secret" not in page and "WXYZ" in page
        client.post("/api/settings/gemini-key", json={"key": None}, headers=H)
        assert "WXYZ" not in client.get("/settings").get_data(as_text=True)

    def test_test_gemini_uses_stored_key(self, client):
        client.post("/api/settings/gemini-key", json={"key": "AIza-stored-1234"}, headers=H)
        with patch("web.settings_routes.check_api_key", return_value=(True, "The key works.")) as check:
            resp = client.post("/api/settings/test-gemini", json={}, headers=H)
        assert resp.get_json() == {"ok": True, "message": "The key works."}
        assert check.call_args.args[0] == "AIza-stored-1234"

    def test_credentials_upload(self, client, home):
        bad = client.post("/api/settings/credentials", json={"client": {"web": {}}}, headers=H)
        assert bad.status_code == 400
        assert not (home / "credentials.json").exists()
        ok = client.post("/api/settings/credentials", json={"client": _CLIENT}, headers=H)
        assert ok.status_code == 200
        assert json.loads((home / "credentials.json").read_text(encoding="utf-8")) == _CLIENT

    def test_read_only_when_headless(self, home, monkeypatch):
        monkeypatch.setenv("GMAILCLEANER_HEADLESS", "1")
        c = create_app(config=_raw_config()).test_client()
        c.set_cookie("gc_token", SECRET)
        assert c.post("/api/settings", json={"confirm_first": False}).status_code == 403
        assert c.post("/api/settings/gemini-key", json={"key": "x"}).status_code == 403
        assert b"can't be changed here" in c.get("/settings").data


# ── Scan / execute jobs ───────────────────────────────────────────────────────

def _msg(i):
    return GmailMessage(
        id=f"m{i}", thread_id=f"t{i}", sender_raw="News <hi@news.example.com>",
        sender_name="News", sender_email="hi@news.example.com", sender_domain="news.example.com",
        subject=f"Weekly #{i}", date=datetime(2024, 5, 10, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"], snippet="...",
        unsubscribe=UnsubscribeTarget(http_url="https://news.example.com/u", supports_one_click=True),
    )


@pytest.fixture
def gmail():
    g = MagicMock()
    with patch("web.app.get_credentials") as creds, \
         patch("web.app.GmailClient", return_value=g), \
         patch("core.pipeline.GmailFetcher.fetch_inbox", return_value=[_msg(i) for i in range(6)]), \
         patch("gmail.executor.time.sleep"), \
         patch("httpx.Client.send", return_value=httpx.Response(200)):
        g.creds = creds
        yield g


class TestJobsRoutes:
    def test_scan_job(self, client, app, gmail):
        resp = client.post("/api/scan", json={"execute_trusted": False}, headers=H)
        assert resp.status_code == 202
        job_id = resp.get_json()["job_id"]
        app.config["JOBS"].wait(job_id, 10)
        job = client.get(f"/api/jobs/{job_id}").get_json()
        assert job["status"] == "done", job
        assert job["result"]["emails_scanned"] == 6
        assert job["result"]["unsubscribe_count"] == 1
        # Never an interactive consent flow from a job.
        assert gmail.creds.call_args.kwargs == {"interactive": False}
        assert client.get("/api/jobs").get_json()["jobs"][0]["id"] == job_id
        assert b"Stop emails from News" in client.get("/pending").data

    def test_scan_rejects_non_bool(self, client):
        assert client.post("/api/scan", json={"execute_trusted": "yes"}, headers=H).status_code == 400

    def test_unknown_job(self, client):
        assert client.get("/api/jobs/nope").status_code == 404

    def test_select_all_then_background_execute(self, client, app, gmail):
        app.config["JOBS"].wait(client.post("/api/scan", json={}, headers=H).get_json()["job_id"], 10)
        report = json.loads(Path(app.config["GMAILCLEANER"]["output"]["pending_actions_path"])
                            .read_text(encoding="utf-8"))
        groups = [g["group_id"] for g in report["delete_groups"]]
        resp = client.post("/api/confirm", json={"action_type": "delete", "targets": groups,
                                                 "confirmed": True}, headers=H)
        assert resp.status_code == 200 and resp.get_json()["targets"] == groups

        resp = client.post("/api/execute", json={"background": True}, headers=H)
        assert resp.status_code == 202
        job = app.config["JOBS"].wait(resp.get_json()["job_id"], 10)
        assert job["status"] == "done", job
        assert job["result"] == {"status": "executed", "unsubscribed": 0, "trashed": 6, "failed": 0}
        gmail.batch_trash.assert_called_once()

    def test_scan_failure_is_friendly(self, client, app):
        from auth.oauth import SignInRequired
        with patch("web.app.get_credentials", side_effect=SignInRequired("expired")):
            job_id = client.post("/api/scan", json={}, headers=H).get_json()["job_id"]
            job = app.config["JOBS"].wait(job_id, 10)
        assert job["status"] == "failed" and job["error"] == "Please sign in again."

    def test_dashboard_pages_render_in_desktop(self, client, home):
        add_token(home, "jane_gmail.com")
        for path in ("/", "/pending", "/stats", "/history", "/settings", "/accounts", "/help", "/?welcome=1"):
            assert client.get(path).status_code == 200, path
        assert b"Scan my inbox now" in client.get("/").data
        assert b"Welcome to GmailCleaner" in client.get("/?welcome=1").data
        assert b"Pub/Sub" not in client.get("/").data

    def test_sign_in_prompt_without_accounts(self, client):
        assert b"Sign in with Google" in client.get("/").data
