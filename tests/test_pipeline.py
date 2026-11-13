"""
Tests for core/pipeline.py

Gmail is mocked; engines, reporter, DB and executor run for real on temp files.

Covers:
  - run_scan writes the report and returns accurate counts and progress steps
  - Missing sign-in → AuthRequired (never an interactive consent flow)
  - Fetch failure → FetchFailed and the run is closed
  - Empty inbox → zero result
  - Trusted rules: dry run only auto-confirms; dry_run=False executes
  - Gemini quota exhaustion → friendly warning, scan still completes
  - Per-account lock: a second scan while one holds the lock → Busy
  - run_execute: NoReport, NothingConfirmed, success, report saved on failure
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from auth.oauth import SignInRequired
from core import pipeline
from engines.triage import TriageSummary
from gmail.parser import GmailMessage, UnsubscribeTarget
from store.db import Database


def _msg(i: int, domain: str = "news.example.com") -> GmailMessage:
    return GmailMessage(
        id=f"m{i}", thread_id=f"t{i}",
        sender_raw=f"News <hi@{domain}>", sender_name="News",
        sender_email=f"hi@{domain}", sender_domain=domain,
        subject=f"Weekly digest #{i}",
        date=datetime(2024, 5, 10, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"], snippet="...",
        unsubscribe=UnsubscribeTarget(http_url=f"https://{domain}/u", supports_one_click=True),
    )


MESSAGES = [_msg(i) for i in range(6)]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("DATABASE_PATH", "PENDING_ACTIONS_PATH", "GMAILCLEANER_ACCOUNT",
                "GEMINI_API_KEY", "GMAILCLEANER_HOME"):
        monkeypatch.delenv(var, raising=False)
    raw = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    raw["triage"]["enabled"] = False
    raw["digest"]["enabled"] = False
    raw["database"]["path"] = str(tmp_path / "db" / "gc.db")
    raw["output"]["pending_actions_path"] = str(tmp_path / "out" / "pending.json")
    from core.settings import resolve_config
    return resolve_config(raw)


@pytest.fixture
def gmail():
    g = MagicMock()
    g.get_profile.return_value = {"emailAddress": "me@example.com"}
    with patch.object(pipeline.GmailFetcher, "fetch_inbox", return_value=MESSAGES), \
         patch("gmail.executor.time.sleep"), \
         patch("httpx.Client.send", return_value=httpx.Response(200)) as http:
        g.http = http
        yield g


def _report(cfg) -> dict:
    with open(cfg["output"]["pending_actions_path"], encoding="utf-8") as f:
        return json.load(f)


class TestRunScan:
    def test_scan_counts_and_progress(self, cfg, gmail):
        steps = []
        result = pipeline.run_scan(cfg, client=gmail, progress=lambda s, f: steps.append((s, f)))
        assert result.emails_scanned == 6
        assert result.unsubscribe_count == 1
        assert result.cleanup_count == 1
        assert result.auto_confirmed == 0 and result.executed == 0
        assert result.warnings == []
        names = [s for s, _ in steps]
        assert names[0] == "Signing in" and names[-1] == "Done"
        assert "Fetching emails" in names and "Saving report" in names
        fractions = [f for _, f in steps]
        assert fractions == sorted(fractions)
        assert _report(cfg)["run_id"] == result.run_id
        gmail.batch_trash.assert_not_called()

    def test_to_dict_is_json_safe(self, cfg, gmail):
        json.dumps(pipeline.run_scan(cfg, client=gmail).to_dict())

    def test_no_sign_in_raises_auth_required(self, cfg):
        with patch.object(pipeline, "load_credentials", side_effect=SignInRequired("x")) as lc:
            with pytest.raises(pipeline.AuthRequired):
                pipeline.run_scan(cfg)
        lc.assert_called_once_with(cfg, interactive=False)

    def test_fetch_failure(self, cfg):
        with patch.object(pipeline.GmailFetcher, "fetch_inbox", side_effect=RuntimeError("boom")):
            with pytest.raises(pipeline.FetchFailed):
                pipeline.run_scan(cfg, client=MagicMock())
        runs = Database(cfg["database"]["path"]).get_recent_runs()
        assert runs[0]["completed_at"]

    def test_empty_inbox(self, cfg):
        with patch.object(pipeline.GmailFetcher, "fetch_inbox", return_value=[]):
            result = pipeline.run_scan(cfg, client=MagicMock())
        assert result.emails_scanned == 0

    def test_trusted_rules_dry_run(self, cfg, gmail):
        cfg.update(confirm_first=False, trusted_rules=["news.example.com"])
        result = pipeline.run_scan(cfg, client=gmail, dry_run=True)
        assert result.auto_confirmed == 2 and result.executed == 0
        gmail.batch_trash.assert_not_called()

    def test_trusted_rules_execute(self, cfg, gmail):
        cfg.update(confirm_first=False, trusted_rules=["news.example.com"])
        announced, results = [], []
        result = pipeline.run_scan(
            cfg, client=gmail, dry_run=False,
            on_auto_execute=announced.append,
            on_result=lambda t, i, r: results.append(t),
        )
        assert announced == [2]
        assert results == ["unsubscribe", "delete"]
        assert result.executed == 2 and result.failed == 0
        assert _report(cfg)["delete_groups"][0]["executed_at"]

    def test_confirm_first_blocks_rules_even_without_dry_run(self, cfg, gmail):
        cfg.update(confirm_first=True, trusted_rules=["news.example.com"])
        result = pipeline.run_scan(cfg, client=gmail, dry_run=False)
        assert result.auto_confirmed == 0
        gmail.batch_trash.assert_not_called()

    def test_triage_without_key_warns(self, cfg, gmail):
        cfg["triage"]["enabled"] = True
        with patch.object(pipeline, "get_gemini_key", return_value=""):
            result = pipeline.run_scan(cfg, client=gmail)
        assert any("Gemini key" in w for w in result.warnings)

    def test_gemini_quota_warning(self, cfg, gmail):
        cfg["triage"]["enabled"] = True
        fake_gemini = MagicMock(rate_limited=True)
        fake_gemini.__enter__.return_value = fake_gemini
        engine = MagicMock()
        engine.classify.return_value = TriageSummary()
        with patch.object(pipeline, "get_gemini_key", return_value="k"), \
             patch.object(pipeline, "GeminiClient", return_value=fake_gemini), \
             patch.object(pipeline, "TriageEngine", return_value=engine):
            result = pipeline.run_scan(cfg, client=gmail)
        assert pipeline.TRIAGE_QUOTA_WARNING in result.warnings
        assert result.triage_counts["needs_reply"] == 0

    def test_busy_when_locked(self, cfg, gmail):
        with pipeline.account_lock(cfg):
            with pytest.raises(pipeline.Busy):
                pipeline.run_scan(cfg, client=gmail)
        # Released afterwards.
        pipeline.run_scan(cfg, client=gmail)


class TestRunExecute:
    def test_no_report(self, cfg):
        with pytest.raises(pipeline.NoReport):
            pipeline.run_execute(cfg, client=MagicMock())

    def test_nothing_confirmed(self, cfg, gmail):
        pipeline.run_scan(cfg, client=gmail)
        with pytest.raises(pipeline.NothingConfirmed):
            pipeline.run_execute(cfg, client=gmail)

    def test_executes_confirmed(self, cfg, gmail):
        pipeline.run_scan(cfg, client=gmail)
        report = _report(cfg)
        report["delete_groups"][0]["confirmed"] = True
        with open(cfg["output"]["pending_actions_path"], "w", encoding="utf-8") as f:
            json.dump(report, f)

        steps = []
        summary = pipeline.run_execute(cfg, client=gmail, progress=lambda s, f: steps.append(f))
        assert pipeline.execution_result(summary) == {
            "status": "executed", "unsubscribed": 0, "trashed": 6, "failed": 0,
        }
        assert steps[-1] == 1.0
        assert _report(cfg)["delete_groups"][0]["executed_at"]

    def test_report_saved_even_on_failure(self, cfg, gmail):
        pipeline.run_scan(cfg, client=gmail)
        report = _report(cfg)
        report["unsubscribe"][0]["confirmed"] = True
        report["delete_groups"][0]["confirmed"] = True
        with open(cfg["output"]["pending_actions_path"], "w", encoding="utf-8") as f:
            json.dump(report, f)

        gmail.batch_trash.side_effect = KeyboardInterrupt  # dies part-way through
        with pytest.raises(KeyboardInterrupt):
            pipeline.run_execute(cfg, client=gmail)
        saved = _report(cfg)
        assert saved["unsubscribe"][0]["executed_at"]   # the finished item is stamped
        assert not saved["delete_groups"][0].get("executed_at")
