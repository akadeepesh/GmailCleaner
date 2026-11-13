"""
Tests for the desktop app's Phase 2 pieces: the desktop-only routes
(web/desktop_routes.py), the Settings schedule card, the update banner,
background services (desktop/services.py), the tray (desktop/tray.py, with a
fake pystray), single-instance handling (desktop/instance.py) and the
launcher (desktop/__main__.py).

Gmail, keyring, pystray, the browser and the GitHub API are mocked.
"""

import json
import os
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auth.oauth import SCOPES
from core.scheduler import PAUSED_KEY
from desktop import __main__ as launcher
from desktop import tray as tray_mod
from desktop.instance import SingleInstance
from desktop.services import Services
from web.app import create_app

SECRET = "desktop-secret-123"
H = {"X-GC-Request": "1"}
_TOKEN = {
    "token": "access", "refresh_token": "refresh",
    "client_id": "id.apps.googleusercontent.com", "client_secret": "secret",
    "token_uri": "https://oauth2.googleapis.com/token", "scopes": SCOPES,
}

_ENV = ("GMAILCLEANER_ACCOUNT", "GMAILCLEANER_HEADLESS", "K_SERVICE", "CLOUD_RUN_JOB",
        "DATABASE_PATH", "PENDING_ACTIONS_PATH", "GOOGLE_TOKEN_PATH", "GOOGLE_TOKEN_JSON",
        "GOOGLE_CREDENTIALS_PATH", "GEMINI_API_KEY", "GMAILCLEANER_PORT", "DIGEST_RECIPIENT",
        "GMAILCLEANER_CONFIG")


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
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    h = tmp_path / "app home"
    monkeypatch.setenv("GMAILCLEANER_HOME", str(h))
    monkeypatch.setenv("DASHBOARD_SECRET", SECRET)
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
    return h


def raw_config(**overrides) -> dict:
    cfg = {
        "triage": {"enabled": False, "model": "gemini-3.5-flash", "confidence_threshold": 0.75},
        "digest": {"enabled": False},
        "pubsub": {"topic_name": ""},
        "scan": {"max_emails": 100, "lookback_days": 7, "query_filter": "in:inbox"},
        "unsubscribe": {"enabled": True, "min_email_count": 2},
        "bulk_cleanup": {"enabled": True, "min_group_size": 5},
        "confirm_first": True,
        "trusted_rules": [],
        "blocked_senders": [],
        "schedule": {"enabled": False, "time": "08:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                     "run_trusted_rules": True, "catch_up": True},
        "realtime": {"poll_minutes": 0},
        "updates": {"check": True},
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def app(home):
    application = create_app(config=raw_config())
    application.config["TESTING"] = True
    yield application
    application.config["JOBS"].shutdown(timeout=5)


@pytest.fixture
def client(app):
    c = app.test_client()
    c.set_cookie("gc_token", SECRET)
    return c


def add_token(home: Path, name=None) -> Path:
    path = home / "token.json" if name is None else home / "tokens" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_TOKEN), encoding="utf-8")
    return path


# ── Desktop-only routes ──────────────────────────────────────────────────────

class TestQuit:
    def test_unavailable_without_launcher(self, client):
        assert client.post("/api/app/quit", json={}, headers=H).status_code == 400

    def test_quits(self, app, client):
        done = threading.Event()
        app.config["ON_QUIT"] = done.set
        resp = client.post("/api/app/quit", json={}, headers=H)
        assert resp.status_code == 200 and resp.get_json()["status"] == "quitting"
        assert done.wait(3)

    def test_asks_when_job_running(self, app, client):
        quit_called = threading.Event()
        app.config["ON_QUIT"] = quit_called.set
        release = threading.Event()
        app.config["JOBS"].submit(None, "scan", lambda p: release.wait(5))
        try:
            resp = client.post("/api/app/quit", json={}, headers=H)
            assert resp.status_code == 409 and resp.get_json()["running"] is True
            assert client.post("/api/app/quit", json={"force": True}, headers=H).status_code == 200
            assert quit_called.wait(3)
        finally:
            release.set()

    def test_needs_csrf_header_and_secret(self, app):
        app.config["ON_QUIT"] = MagicMock()
        c = app.test_client()
        assert c.post("/api/app/quit", json={}, headers=H).status_code == 401
        c.set_cookie("gc_token", SECRET)
        assert c.post("/api/app/quit", json={}).status_code == 403
        app.config["ON_QUIT"].assert_not_called()

    def test_quit_button_only_with_launcher(self, app, client):
        assert b"Quit GmailCleaner" not in client.get("/help").data
        app.config["ON_QUIT"] = MagicMock()
        assert b"Quit GmailCleaner" in client.get("/help").data


def test_desktop_routes_404_outside_desktop(monkeypatch, tmp_path):
    for var in ("GMAILCLEANER_HOME", "DASHBOARD_SECRET", "GMAILCLEANER_ACCOUNT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    c = create_app(config=raw_config()).test_client()
    for url in ("/api/app/quit", "/api/app/open-logs", "/api/updates/dismiss",
                "/api/schedule/pause", "/api/settings/autostart"):
        assert c.post(url, json={}).status_code == 404, url


def test_web_works_without_desktop_package(monkeypatch, tmp_path):
    """The Docker images don't ship desktop/: the web app must not need it."""
    for var in ("GMAILCLEANER_HOME", "DASHBOARD_SECRET", "GMAILCLEANER_ACCOUNT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    monkeypatch.setitem(sys.modules, "desktop", None)
    monkeypatch.setitem(sys.modules, "desktop.autostart", None)
    c = create_app(config=raw_config()).test_client()
    assert c.get("/settings").status_code == 200
    assert c.get("/help").status_code == 200


def test_open_logs(app, client, home):
    opened = []
    app.config["OPEN_FOLDER"] = opened.append
    assert client.post("/api/app/open-logs", json={}, headers=H).status_code == 200
    assert opened == [home / "logs"]


def test_open_logs_error(app, client):
    def fail(path):
        raise OSError("no file manager")
    app.config["OPEN_FOLDER"] = fail
    resp = client.post("/api/app/open-logs", json={}, headers=H)
    assert resp.status_code == 500 and "logs" in resp.get_json()["error"]


class TestUpdateBanner:
    def test_shown_and_dismissed(self, app, client):
        app.config["STATE"].set("updates", {"latest": "99.0.0",
                                            "url": "https://github.com/akadeepesh/GmailCleaner/releases/tag/v99.0.0"})
        page = client.get("/").data
        assert b"A new version of GmailCleaner (99.0.0)" in page
        assert client.post("/api/updates/dismiss", json={}, headers=H).status_code == 200
        assert b"A new version" not in client.get("/").data

    def test_hidden_when_checks_off(self, home):
        app = create_app(config=raw_config(updates={"check": False}))
        app.config["STATE"].set("updates", {"latest": "99.0.0"})
        c = app.test_client()
        c.set_cookie("gc_token", SECRET)
        assert b"A new version" not in c.get("/").data

    def test_state_file_in_home(self, app, home):
        app.config["STATE"].set("x", 1)
        assert json.loads((home / "state.json").read_text(encoding="utf-8")) == {"x": 1}


class TestSchedulePause:
    def test_pause_and_resume(self, app, client):
        assert client.post("/api/schedule/pause", json={"paused": True}, headers=H).status_code == 200
        assert app.config["STATE"].get(PAUSED_KEY) is True
        assert b"Automatic scans and new-mail checks are paused" in client.get("/settings").data
        client.post("/api/schedule/pause", json={"paused": False}, headers=H)
        assert app.config["STATE"].get(PAUSED_KEY) is False

    def test_bad_body(self, client):
        assert client.post("/api/schedule/pause", json={"paused": "yes"}, headers=H).status_code == 400


class TestAutostartRoute:
    def test_dev_checkout_refused(self, client, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        resp = client.post("/api/settings/autostart", json={"enabled": True}, headers=H)
        assert resp.status_code == 400 and "installed app" in resp.get_json()["error"]

    def test_frozen(self, client, monkeypatch):
        from desktop import autostart
        monkeypatch.setattr(autostart, "is_supported", lambda: True)
        calls = []
        monkeypatch.setattr(autostart, "set_enabled", lambda on: calls.append(on) or on)
        resp = client.post("/api/settings/autostart", json={"enabled": True}, headers=H)
        assert resp.get_json() == {"status": "ok", "enabled": True} and calls == [True]

    def test_bad_body(self, client):
        assert client.post("/api/settings/autostart", json={"enabled": 1}, headers=H).status_code == 400


class TestScheduleSettings:
    def test_card_shown(self, client):
        page = client.get("/settings").data
        assert b"Automatic scans" in page and b"Check for new mail" in page
        assert b"Available in the installed app" in page

    def test_save_roundtrip(self, app, client, home):
        changes = {"schedule.enabled": True, "schedule.time": "07:30", "schedule.days": ["mon", "fri"],
                   "schedule.catch_up": False, "realtime.poll_minutes": 15, "updates.check": False}
        resp = client.post("/api/settings", json=changes, headers=H)
        assert resp.status_code == 200, resp.get_json()
        values = resp.get_json()["values"]
        assert values["schedule.time"] == "07:30" and values["schedule.days"] == ["mon", "fri"]
        cfg = app.config["ACCOUNTS"].get(None).cfg
        assert cfg["schedule"]["enabled"] is True and cfg["realtime"]["poll_minutes"] == 15
        assert "schedule:" in (home / "settings.user.yaml").read_text(encoding="utf-8")

    @pytest.mark.parametrize("key,value", [
        ("schedule.time", "25:00"),
        ("schedule.days", []),
        ("schedule.days", ["someday"]),
        ("realtime.poll_minutes", 2),
        ("realtime.poll_minutes", 5000),
        ("schedule.enabled", "yes"),
    ])
    def test_rejected(self, client, key, value):
        resp = client.post("/api/settings", json={key: value}, headers=H)
        assert resp.status_code == 400 and key in resp.get_json()["errors"]


# ── Services ─────────────────────────────────────────────────────────────────

class TestServices:
    def test_accounts_only_signed_in(self, app, home):
        add_token(home, "jane_gmail.com")
        (home / "tokens" / "bob_gmail.com.json").write_text("{}", encoding="utf-8")
        app.config["ACCOUNTS"].remember_email("jane_gmail.com", "jane@gmail.com")
        accounts = Services(app).accounts()
        assert [(a.key, a.label) for a in accounts] == [("jane_gmail.com", "jane@gmail.com")]
        assert accounts[0].cfg["database"]["path"].startswith(str(home))
        assert accounts[0].db is not None

    def test_scheduled_submit_runs_a_scan_job(self, app, home):
        add_token(home, "jane_gmail.com")
        services = Services(app)
        account = services.accounts()[0]
        finished = []
        with patch("web.app._credentials"), patch("web.app.GmailClient"), \
             patch("web.app.run_scan") as run_scan:
            run_scan.return_value.to_dict.return_value = {"unsubscribe_count": 1}
            job_id, created = services._submit(account, True, finished.append)
            job = app.config["JOBS"].wait(job_id, 5)
        assert created and job["status"] == "done" and job["kind"] == "scan"
        assert run_scan.call_args.kwargs["dry_run"] is False      # run_trusted_rules
        assert finished and finished[0]["id"] == job_id

    def test_scan_now_without_accounts_notifies(self, app):
        notes = []
        assert Services(app, notify=lambda t, m: notes.append(m)).scan_now() == 0
        assert "Sign in" in notes[0]

    def test_scan_now_notifies_when_done(self, app, home):
        add_token(home, "jane_gmail.com")
        notes = []
        services = Services(app, notify=lambda t, m: notes.append(m))
        with patch("web.app._credentials"), patch("web.app.GmailClient"), \
             patch("web.app.run_scan") as run_scan:
            run_scan.return_value.to_dict.return_value = {"unsubscribe_count": 2, "cleanup_count": 1}
            assert services.scan_now() == 1
            app.config["JOBS"].shutdown(5)
        assert run_scan.call_args.kwargs["dry_run"] is True       # Scan now never acts
        assert any("found 3 suggestions" in n for n in notes)

    def test_check_updates_respects_setting(self, home):
        app = create_app(config=raw_config(updates={"check": False}))
        with patch("core.updates.maybe_check") as check:
            Services(app).check_updates()
        assert check.call_args.kwargs["enabled"] is False

    def test_busy(self, app):
        services = Services(app)
        release = threading.Event()
        app.config["JOBS"].submit("jane_gmail.com", "scan", lambda p: release.wait(5))
        try:
            acct = types.SimpleNamespace(name="jane_gmail.com")
            assert services._busy(acct) is True
        finally:
            release.set()

    def test_notify_errors_contained(self, app):
        def boom(t, m):
            raise RuntimeError("no notifications here")
        Services(app, notify=boom).notify("t", "m")   # no exception

    def test_pause_shared_with_routes(self, app, client):
        services = Services(app)
        client.post("/api/schedule/pause", json={"paused": True}, headers=H)
        assert services.paused is True
        assert services.poller._paused() is True


# ── Tray ─────────────────────────────────────────────────────────────────────

class FakeItem:
    def __init__(self, text, action, checked=None, enabled=True, default=False):
        self.text, self.action, self.checked, self.enabled, self.default = text, action, checked, enabled, default


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.notes, self.stopped, self.updated = [], False, 0

    def notify(self, message, title=None):
        self.notes.append((title, message))

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.updated += 1

    def run(self):
        pass


@pytest.fixture
def fake_pystray(monkeypatch):
    mod = types.ModuleType("pystray")
    mod.Menu, mod.MenuItem, mod.Icon = FakeMenu, FakeItem, FakeIcon
    monkeypatch.setitem(sys.modules, "pystray", mod)
    monkeypatch.setattr(tray_mod, "load_icon_image", lambda: "image")
    return mod


class FakeController:
    def __init__(self):
        self.calls = []
        self.paused = False
        self.autostart_supported = False
        self.autostart_enabled = False

    def open_browser(self):
        self.calls.append("open")

    def scan_now(self):
        self.calls.append("scan")

    def toggle_pause(self):
        self.paused = not self.paused

    def toggle_autostart(self):
        self.calls.append("autostart")

    def request_quit(self):
        self.calls.append("quit")


class TestTray:
    def test_menu(self, fake_pystray):
        c = FakeController()
        tray = tray_mod.try_create(c)
        items = [i for i in tray.icon.menu.items if i is not FakeMenu.SEPARATOR]
        texts = [i.text for i in items]
        assert texts == ["Open GmailCleaner", "Scan now", "Pause automatic scans",
                         "Start when I log in (installed app only)", "Quit"]
        assert items[0].default is True
        assert items[3].enabled is False
        for item in items:
            item.action()
        assert c.calls == ["open", "scan", "autostart", "quit"]
        assert c.paused is True and items[2].checked(None) is True

    def test_notify(self, fake_pystray):
        tray = tray_mod.try_create(FakeController())
        tray.notify("GmailCleaner", "hello")
        assert tray.icon.notes == [("GmailCleaner", "hello")]
        tray.icon.HAS_NOTIFICATION = False
        tray.notify("GmailCleaner", "dropped")
        assert len(tray.icon.notes) == 1

    def test_no_pystray(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)
        assert tray_mod.try_create(FakeController()) is None

    def test_icon_asset_exists(self):
        from core.paths import resource_path
        assert resource_path(tray_mod.ICON_PATH).is_file()
        assert resource_path("desktop/assets/icon.ico").is_file()
        assert resource_path("desktop/assets/icon.icns").is_file()


# ── Single instance ──────────────────────────────────────────────────────────

class TestSingleInstance:
    def test_second_instance_refused(self, tmp_path):
        home = tmp_path / "Ünïcode home"
        first, second = SingleInstance(home), SingleInstance(home)
        assert first.acquire() is True
        try:
            assert second.acquire() is False
            first.write_runtime(8765, "s3cret")
            assert second.running_url() == "http://127.0.0.1:8765/?token=s3cret"
        finally:
            first.release()
        assert not first.runtime_path.exists()
        assert second.acquire() is True
        second.release()

    @pytest.mark.parametrize("content", ["", "[]", '{"port": "x", "secret": "s"}',
                                         '{"port": 70000, "secret": "s"}', '{"port": 1, "secret": ""}'])
    def test_bad_runtime(self, tmp_path, content):
        inst = SingleInstance(tmp_path)
        inst.runtime_path.write_text(content, encoding="utf-8")
        assert inst.running_url() is None

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
    def test_runtime_private(self, tmp_path):
        inst = SingleInstance(tmp_path)
        inst.acquire()
        inst.write_runtime(1, "s")
        assert (inst.runtime_path.stat().st_mode & 0o777) == 0o600
        inst.release()


# ── Launcher ─────────────────────────────────────────────────────────────────

@pytest.fixture
def restore_env():
    names = ("GMAILCLEANER_HOME", "DASHBOARD_SECRET", "GMAILCLEANER_PORT")
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class TestLauncher:
    def test_second_launch_opens_running_app(self, tmp_path, restore_env):
        running = SingleInstance(tmp_path)
        running.acquire()
        running.write_runtime(9999, "abc")
        try:
            with patch.object(launcher.webbrowser, "open") as browser:
                assert launcher.main(["--home", str(tmp_path)]) == 0
            browser.assert_called_once_with("http://127.0.0.1:9999/?token=abc")
            with patch.object(launcher.webbrowser, "open") as browser:
                assert launcher.main(["--home", str(tmp_path), "--background"]) == 0
            browser.assert_not_called()
        finally:
            running.release()

    def test_second_launch_while_starting(self, tmp_path, restore_env):
        running = SingleInstance(tmp_path)
        running.acquire()
        try:
            assert launcher.main(["--home", str(tmp_path), "--no-browser"]) == 1
        finally:
            running.release()

    def test_parse_background(self):
        args = launcher.parse_args(["--background", "--no-tray"])
        assert args.background and args.no_tray

    def test_request_quit_asks_in_browser_when_busy(self, tmp_path):
        d = launcher.DesktopApp(tmp_path, 8765, "s")
        jobs = MagicMock()
        jobs.any_active.return_value = True
        d.app = types.SimpleNamespace(config={"JOBS": jobs})
        with patch.object(launcher.webbrowser, "open", return_value=True) as browser:
            d.request_quit()
        browser.assert_called_once_with("http://127.0.0.1:8765/?token=s&confirm_quit=1")
        assert not d.done.is_set()

    def test_quit_idempotent(self, tmp_path):
        d = launcher.DesktopApp(tmp_path, 8765, "s")
        d.services, d.server, d.tray, d.instance = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        d.app = types.SimpleNamespace(config={"JOBS": MagicMock(any_active=MagicMock(return_value=False))})
        d.quit()
        d.quit()
        d.services.stop.assert_called_once()
        d.server.close.assert_called_once()
        d.instance.release.assert_called_once()
        d.tray.stop.assert_called_once()
        assert d.done.is_set()

    def test_start_serves_and_quits(self, tmp_path, monkeypatch, restore_env):
        """Real waitress on a free port: /health answers, quit shuts it all down."""
        for var in _ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
        home = tmp_path / "home with space"
        port = launcher.pick_port(0)
        secret = launcher.prepare_environment(home, port)
        d = launcher.DesktopApp(home, port, secret)
        with patch("core.updates.maybe_check"):
            d.start()
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
                assert resp.status_code == 200
                assert d.services.scheduler.running and d.services.poller.running
                locked = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)
                assert locked.status_code == 401        # no token: locked page
            finally:
                d.quit()
        assert d.done.is_set()
        assert not d.services.scheduler.running
