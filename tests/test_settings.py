"""
Tests for core/settings.py

Covers:
  - Defaults when settings.yaml leaves paths unset
  - settings.yaml values are respected
  - Environment variables override settings.yaml
  - Multi-account namespacing of token, DB and report paths
  - Invalid account names are rejected
  - load_config raises FileNotFoundError for a missing file
  - The shipped config/settings.yaml loads and has no hardcoded recipient
"""

import pytest

from core.settings import load_config, resolve_config

_ENV_VARS = (
    "GMAILCLEANER_ACCOUNT", "DATABASE_PATH", "PENDING_ACTIONS_PATH",
    "GOOGLE_CREDENTIALS_PATH", "GOOGLE_TOKEN_PATH", "PUBSUB_TOPIC", "DIGEST_RECIPIENT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestDefaults:
    def test_defaults(self):
        cfg = resolve_config({})
        assert cfg["database"]["path"] == "data/gmailcleaner.db"
        assert cfg["output"]["pending_actions_path"] == "output/pending_actions.json"
        assert cfg["auth"]["token_path"] == "config/token.json"
        assert cfg["auth"]["credentials_path"] == "config/credentials.json"
        assert cfg["pubsub"]["topic_name"] == ""
        assert cfg["account"] is None

    def test_yaml_values_kept(self):
        cfg = resolve_config({"database": {"path": "x/y.db"}, "pubsub": {"topic_name": "t"}})
        assert cfg["database"]["path"] == "x/y.db"
        assert cfg["pubsub"]["topic_name"] == "t"

    def test_input_not_mutated(self):
        raw = {"database": {"path": "x.db"}}
        resolve_config(raw, account="work")
        assert raw == {"database": {"path": "x.db"}}


class TestEnvOverrides:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("DATABASE_PATH", "/data/g.db")
        monkeypatch.setenv("PENDING_ACTIONS_PATH", "/data/p.json")
        monkeypatch.setenv("PUBSUB_TOPIC", "projects/p/topics/t")
        monkeypatch.setenv("DIGEST_RECIPIENT", "me@example.com")
        cfg = resolve_config({"database": {"path": "local.db"}, "digest": {"recipient": ""}})
        assert cfg["database"]["path"] == "/data/g.db"
        assert cfg["output"]["pending_actions_path"] == "/data/p.json"
        assert cfg["pubsub"]["topic_name"] == "projects/p/topics/t"
        assert cfg["digest"]["recipient"] == "me@example.com"


class TestAccounts:
    def test_account_namespaces_paths(self):
        cfg = resolve_config({}, account="work")
        assert cfg["account"] == "work"
        assert cfg["database"]["path"].replace("\\", "/") == "data/work/gmailcleaner.db"
        assert cfg["output"]["pending_actions_path"].replace("\\", "/") == "output/work/pending_actions.json"
        assert cfg["auth"]["token_path"].replace("\\", "/") == "config/tokens/work.json"

    def test_account_from_env(self, monkeypatch):
        monkeypatch.setenv("GMAILCLEANER_ACCOUNT", "personal")
        assert resolve_config({})["account"] == "personal"

    def test_env_override_beats_account(self, monkeypatch):
        monkeypatch.setenv("DATABASE_PATH", "/data/g.db")
        assert resolve_config({}, account="work")["database"]["path"] == "/data/g.db"

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", " x", "a" * 80])
    def test_invalid_account_rejected(self, bad):
        if not bad:
            assert resolve_config({}, account=bad)["account"] is None
            return
        with pytest.raises(ValueError):
            resolve_config({}, account=bad)


class TestLoadConfig:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nope.yaml"))

    def test_shipped_settings_load(self):
        cfg = load_config("config/settings.yaml")
        assert cfg["confirm_first"] is True
        assert cfg["trusted_rules"] == []
        assert cfg["digest"]["recipient"] == ""
