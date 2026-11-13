"""
Tests for core/user_settings.py and the overrides layer in core/settings.py

Covers:
  - Unknown keys are rejected (whitelist)
  - Type and range checks with plain-language messages
  - Trusted rules: bare "*", "*.com" and junk rejected; "both" → shorthand
  - Blocked senders accept domains and emails only
  - triage.model can't inject path characters
  - Merge order: shipped defaults < settings.user.yaml < environment
  - save_user_overrides writes atomically and only valid input
  - Legacy paths are unchanged when no app home is set
"""

import pytest
import yaml

from core import settings
from core.user_settings import (
    SettingsValidationError,
    current_values,
    rules_to_rows,
    to_nested,
    validate_changes,
)

_ENV_VARS = (
    "GMAILCLEANER_ACCOUNT", "DATABASE_PATH", "PENDING_ACTIONS_PATH", "GMAILCLEANER_CONFIG",
    "GOOGLE_CREDENTIALS_PATH", "GOOGLE_TOKEN_PATH", "PUBSUB_TOPIC", "DIGEST_RECIPIENT",
    "GMAILCLEANER_HOME",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path))
    return tmp_path


class TestValidation:
    def test_unknown_key_rejected(self):
        clean, errors = validate_changes({"database.path": "/etc/passwd", "auth.token_path": "x"})
        assert clean == {}
        assert set(errors) == {"database.path", "auth.token_path"}

    def test_valid_values(self):
        clean, errors = validate_changes({
            "scan.lookback_days": 14,
            "scan.max_emails": 1000,
            "triage.confidence_threshold": 0.8,
            "unsubscribe.enabled": False,
            "digest.recipient": "",
        })
        assert errors == {}
        assert clean["scan.lookback_days"] == 14

    @pytest.mark.parametrize("key,value", [
        ("scan.lookback_days", 0),
        ("scan.lookback_days", 366),
        ("scan.max_emails", 10),
        ("scan.lookback_days", "30"),
        ("scan.lookback_days", True),
        ("scan.lookback_days", 2.5),
        ("triage.confidence_threshold", 1.5),
        ("unsubscribe.enabled", "yes"),
        ("digest.recipient", "not-an-email"),
        ("triage.model", "../../evil"),
        ("triage.model", "a/b"),
        ("scan.query_filter", "x" * 501),
    ])
    def test_invalid_values(self, key, value):
        clean, errors = validate_changes({key: value})
        assert key in errors and key not in clean

    def test_not_a_dict(self):
        assert validate_changes(["x"])[1]


class TestTrustedRules:
    @pytest.mark.parametrize("domain", ["*", "*.", "*.com", "", "com", "a b.com", "http://x.com"])
    def test_bad_domains_rejected(self, domain):
        _, errors = validate_changes({"trusted_rules": [{"action": "delete", "domain": domain}]})
        assert "trusted_rules" in errors

    def test_bad_action_rejected(self):
        _, errors = validate_changes({"trusted_rules": [{"action": "nuke", "domain": "a.com"}]})
        assert "trusted_rules" in errors

    def test_normalized(self):
        clean, errors = validate_changes({"trusted_rules": [
            {"action": "both", "domain": "Promo.Example.org"},
            {"action": "delete", "domain": "*.deals.example"},
            {"action": "delete", "domain": "*.deals.example"},
        ]})
        assert errors == {}
        assert clean["trusted_rules"] == [
            "promo.example.org",
            {"action": "delete", "domain": "*.deals.example"},
        ]

    def test_rows_round_trip(self):
        rows = rules_to_rows(["a.com", {"action": "unsubscribe", "domain": "b.com"}])
        assert rows == [{"action": "both", "domain": "a.com"},
                        {"action": "unsubscribe", "domain": "b.com"}]
        clean, errors = validate_changes({"trusted_rules": rows})
        assert not errors
        assert clean["trusted_rules"] == ["a.com", {"action": "unsubscribe", "domain": "b.com"}]


class TestBlockedSenders:
    def test_domains_and_emails(self):
        clean, errors = validate_changes({"blocked_senders": ["Bank.com", "boss@work.org", " ", "bank.com"]})
        assert errors == {}
        assert clean["blocked_senders"] == ["bank.com", "boss@work.org"]

    @pytest.mark.parametrize("bad", ["*", "*.bank.com", "nodot", 42])
    def test_bad_entries(self, bad):
        _, errors = validate_changes({"blocked_senders": [bad]})
        assert "blocked_senders" in errors


def test_to_nested():
    assert to_nested({"scan.lookback_days": 3, "confirm_first": False}) == {
        "scan": {"lookback_days": 3}, "confirm_first": False,
    }


def test_current_values_defaults_confirm_first():
    values = current_values({})
    assert values["confirm_first"] is True
    assert values["trusted_rules"] == []


class TestOverridesLayer:
    def test_merge_order_and_env_wins(self, home, monkeypatch):
        settings.save_user_overrides({"scan.lookback_days": 7, "digest.recipient": "a@b.com"})
        cfg = settings.load_config()
        assert cfg["scan"]["lookback_days"] == 7
        assert cfg["scan"]["max_emails"] == 500          # untouched default
        assert cfg["digest"]["recipient"] == "a@b.com"
        monkeypatch.setenv("DIGEST_RECIPIENT", "env@b.com")
        assert settings.load_config()["digest"]["recipient"] == "env@b.com"

    def test_save_merges_with_existing(self, home):
        settings.save_user_overrides({"scan.lookback_days": 7})
        settings.save_user_overrides({"scan.max_emails": 100})
        data = yaml.safe_load((home / "settings.user.yaml").read_text(encoding="utf-8"))
        assert data == {"scan": {"lookback_days": 7, "max_emails": 100}}

    def test_invalid_save_writes_nothing(self, home):
        with pytest.raises(SettingsValidationError) as info:
            settings.save_user_overrides({"scan.lookback_days": 9999, "confirm_first": False})
        assert "scan.lookback_days" in info.value.errors
        assert not (home / "settings.user.yaml").exists()

    def test_no_temp_files_left(self, home):
        settings.save_user_overrides({"confirm_first": False})
        assert [p.name for p in home.iterdir()] == ["settings.user.yaml"]

    def test_explicit_config_ignores_overrides(self, home):
        settings.save_user_overrides({"scan.lookback_days": 7})
        cfg = settings.load_config("config/settings.yaml")
        assert cfg["scan"]["lookback_days"] == 30

    def test_confirm_first_round_trip(self, home):
        settings.save_user_overrides({"confirm_first": False})
        assert settings.load_config()["confirm_first"] is False
        settings.save_user_overrides({"confirm_first": True})
        assert settings.load_config()["confirm_first"] is True

    def test_corrupt_overrides_ignored(self, home):
        (home / "settings.user.yaml").write_text("{not: [valid", encoding="utf-8")
        assert settings.load_config()["scan"]["lookback_days"] == 30


class TestAppHomePaths:
    def test_home_paths(self, home):
        cfg = settings.resolve_config({"database": {"path": "data/gmailcleaner.db"}})
        assert cfg["database"]["path"] == str(home / "data" / "gmailcleaner.db")
        assert cfg["output"]["pending_actions_path"] == str(home / "output" / "pending_actions.json")
        assert cfg["auth"]["token_path"] == str(home / "token.json")
        assert cfg["auth"]["credentials_path"] == str(home / "credentials.json")
        assert cfg["output"]["exports_dir"] == str(home / "exports")
        assert cfg["logging"]["file_path"] == str(home / "logs" / "gmailcleaner.log")

    def test_home_account_paths(self, home):
        cfg = settings.resolve_config({}, account="jane_gmail.com")
        assert cfg["auth"]["token_path"] == str(home / "tokens" / "jane_gmail.com.json")
        assert cfg["database"]["path"] == str(home / "data" / "jane_gmail.com" / "gmailcleaner.db")

    def test_home_loads_bundled_defaults_from_any_cwd(self, home, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert settings.load_config()["confirm_first"] is True

    def test_env_still_wins_in_home(self, home, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
        assert settings.resolve_config({})["database"]["path"] == str(tmp_path / "x.db")

    def test_legacy_paths_unchanged(self):
        cfg = settings.resolve_config({})
        assert cfg["database"]["path"] == "data/gmailcleaner.db"
        assert cfg["auth"]["token_path"] == "config/token.json"
        assert cfg["output"]["exports_dir"] == "exports"
        assert "file_path" not in cfg.get("logging", {})


# ── Schedule / realtime / updates (Phase 2) ──────────────────────────────────

class TestScheduleFields:
    def test_days_normalized_and_ordered(self):
        from core.user_settings import validate_changes
        clean, errors = validate_changes({"schedule.days": ["Friday", "mon", "fri"]})
        assert errors == {} and clean["schedule.days"] == ["mon", "fri"]

    def test_time_trimmed(self):
        from core.user_settings import validate_changes
        clean, errors = validate_changes({"schedule.time": " 23:59 "})
        assert errors == {} and clean["schedule.time"] == "23:59"

    def test_poll_zero_allowed(self):
        from core.user_settings import validate_changes
        assert validate_changes({"realtime.poll_minutes": 0}) == ({"realtime.poll_minutes": 0}, {})

    def test_defaults_when_config_lacks_keys(self):
        from core.user_settings import current_values
        values = current_values({})
        assert values["schedule.enabled"] is False and values["schedule.time"] == "08:00"
        assert values["realtime.poll_minutes"] == 0 and values["updates.check"] is True

    def test_shipped_defaults(self):
        import yaml
        cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
        assert cfg["schedule"]["enabled"] is False
        assert cfg["realtime"]["poll_minutes"] == 0
        assert cfg["updates"]["check"] is True
