"""
End-to-end tests for main.py (CLI)

Gmail is fully mocked; everything else (engines, reporter, DB, rules,
executor) runs for real against temp files.

Covers:
  - --version works without a config or credentials
  - scan writes the report, logs pending actions and supersedes older ones
  - scan never touches Gmail while confirm_first is true
  - trusted rules: dry run only auto-confirms; --no-dry-run executes
  - execute runs confirmed items once and persists executed_at
  - export writes CSV files
  - --account namespaces the report and DB
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

import main
from gmail.parser import GmailMessage, UnsubscribeTarget
from store.db import Database


def _msg(i: int, domain: str = "news.example.com") -> GmailMessage:
    return GmailMessage(
        id=f"m{i}",
        thread_id=f"t{i}",
        sender_raw=f"News <hi@{domain}>",
        sender_name="News",
        sender_email=f"hi@{domain}",
        sender_domain=domain,
        subject=f"Weekly digest #{i}",
        date=datetime(2024, 5, 10, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"],
        snippet="...",
        unsubscribe=UnsubscribeTarget(http_url=f"https://{domain}/u", supports_one_click=True),
    )


MESSAGES = [_msg(i) for i in range(6)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp config + paths, with Gmail and OAuth mocked out."""
    for var in ("DATABASE_PATH", "PENDING_ACTIONS_PATH", "GMAILCLEANER_ACCOUNT", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def write_config(**overrides) -> str:
        cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
        cfg["triage"]["enabled"] = False
        cfg["digest"]["enabled"] = False
        cfg["logging"]["file_enabled"] = False
        cfg["database"]["path"] = str(tmp_path / "db" / "gc.db")
        cfg["output"]["pending_actions_path"] = str(tmp_path / "out" / "pending.json")
        cfg.update(overrides)
        path = tmp_path / "settings.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return str(path)

    gmail = MagicMock()
    gmail.get_profile.return_value = {"emailAddress": "me@example.com"}
    with patch.object(main, "load_credentials"), \
         patch.object(main, "GmailClient", return_value=gmail), \
         patch.object(main.GmailFetcher, "fetch_inbox", return_value=MESSAGES), \
         patch("gmail.executor.time.sleep"), \
         patch("httpx.Client.post", return_value=MagicMock(status_code=200)) as http_post:
        yield {
            "tmp": tmp_path,
            "write_config": write_config,
            "gmail": gmail,
            "http_post": http_post,
            "report": tmp_path / "out" / "pending.json",
            "db": tmp_path / "db" / "gc.db",
        }


def _run(*args):
    result = CliRunner().invoke(main.cli, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def test_version():
    result = CliRunner().invoke(main.cli, ["--version"])
    assert result.exit_code == 0
    assert "GmailCleaner" in result.output


def test_scan_writes_report_and_changes_nothing(env):
    config = env["write_config"]()
    _run("--config", config, "scan")

    report = json.loads(env["report"].read_text(encoding="utf-8"))
    assert [i["domain"] for i in report["unsubscribe"]] == ["news.example.com"]
    assert report["delete_groups"][0]["group_id"] == "domain:news.example.com"
    assert all(not i["confirmed"] for i in report["unsubscribe"] + report["delete_groups"])

    env["gmail"].batch_trash.assert_not_called()
    env["http_post"].assert_not_called()

    db = Database(str(env["db"]))
    assert db.get_dashboard_summary()["pending_actions"] == 2


def test_rescan_supersedes_old_pending(env):
    config = env["write_config"]()
    _run("--config", config, "scan")
    _run("--config", config, "scan")
    db = Database(str(env["db"]))
    statuses = [a["status"] for a in db.list_actions()]
    assert statuses.count("pending") == 2
    assert statuses.count("superseded") == 2


def test_trusted_rules_require_no_dry_run(env):
    config = env["write_config"](confirm_first=False, trusted_rules=["news.example.com"])
    result = _run("--config", config, "scan")

    report = json.loads(env["report"].read_text(encoding="utf-8"))
    assert report["unsubscribe"][0]["auto_confirmed"] is True
    assert "executed_at" not in report["unsubscribe"][0]
    env["gmail"].batch_trash.assert_not_called()
    assert "--no-dry-run" in result.output


def test_trusted_rules_execute_with_no_dry_run(env):
    config = env["write_config"](confirm_first=False, trusted_rules=["news.example.com"])
    _run("--config", config, "scan", "--no-dry-run")

    report = json.loads(env["report"].read_text(encoding="utf-8"))
    assert report["unsubscribe"][0]["executed_at"]
    assert report["delete_groups"][0]["executed_at"]
    env["http_post"].assert_called_once()
    env["gmail"].batch_trash.assert_called_once_with([m.id for m in MESSAGES])


def test_confirm_first_blocks_trusted_rules(env):
    config = env["write_config"](confirm_first=True, trusted_rules=["news.example.com"])
    _run("--config", config, "scan", "--no-dry-run")
    env["gmail"].batch_trash.assert_not_called()
    env["http_post"].assert_not_called()


def test_execute_runs_confirmed_once(env):
    config = env["write_config"]()
    _run("--config", config, "scan")

    report = json.loads(env["report"].read_text(encoding="utf-8"))
    report["delete_groups"][0]["confirmed"] = True
    env["report"].write_text(json.dumps(report), encoding="utf-8")

    result = _run("--config", config, "execute", "--yes")
    assert "Moved to Trash" in result.output
    env["gmail"].batch_trash.assert_called_once()
    env["http_post"].assert_not_called()  # unsubscribe wasn't confirmed

    saved = json.loads(env["report"].read_text(encoding="utf-8"))
    assert saved["delete_groups"][0]["executed_at"]

    result = _run("--config", config, "execute", "--yes")
    assert "No confirmed actions" in result.output
    env["gmail"].batch_trash.assert_called_once()


def test_export(env):
    config = env["write_config"]()
    _run("--config", config, "scan")
    out = env["tmp"] / "exports"
    _run("--config", config, "export", "--out", str(out))
    assert sorted(p.name for p in out.iterdir()) == ["actions.csv", "runs.csv", "senders.csv", "summary.csv"]


def test_account_namespacing(env, tmp_path):
    cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    cfg["triage"]["enabled"] = False
    cfg["digest"]["enabled"] = False
    cfg["logging"]["file_enabled"] = False
    cfg["database"]["path"] = str(tmp_path / "data" / "gc.db")
    cfg["output"]["pending_actions_path"] = str(tmp_path / "output" / "pending.json")
    config = tmp_path / "acct.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _run("--config", str(config), "--account", "work", "scan")

    assert (tmp_path / "output" / "work" / "pending.json").exists()
    assert (tmp_path / "data" / "work" / "gc.db").exists()
    assert not (tmp_path / "output" / "pending.json").exists()
