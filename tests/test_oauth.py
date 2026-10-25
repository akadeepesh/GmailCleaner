"""
Tests for auth/oauth.py

Covers:
  - GOOGLE_TOKEN_JSON (Cloud Run) takes precedence over files
  - Expired env token is refreshed in memory; unrefreshable token raises
  - Invalid GOOGLE_TOKEN_JSON raises a clear RuntimeError
  - Headless mode never launches the browser consent flow
  - Missing credentials file raises FileNotFoundError when consent is needed
  - Cloud Run is detected as headless automatically
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from auth import oauth

_TOKEN = {
    "token": "access",
    "refresh_token": "refresh",
    "client_id": "id.apps.googleusercontent.com",
    "client_secret": "secret",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": oauth.SCOPES,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GOOGLE_TOKEN_JSON", "GMAILCLEANER_HEADLESS", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(var, raising=False)


class TestEnvToken:
    def test_valid_env_token_used(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", json.dumps(_TOKEN))
        creds = MagicMock(valid=True, scopes=oauth.SCOPES)
        with patch.object(oauth.Credentials, "from_authorized_user_info", return_value=creds) as f, \
             patch.object(oauth, "_run_consent_flow") as flow:
            assert oauth.get_credentials(str(tmp_path / "c.json"), str(tmp_path / "t.json")) is creds
        f.assert_called_once_with(_TOKEN, oauth.SCOPES)
        flow.assert_not_called()

    def test_expired_env_token_refreshed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", json.dumps(_TOKEN))
        creds = MagicMock(valid=False, refresh_token="refresh", scopes=oauth.SCOPES)
        with patch.object(oauth.Credentials, "from_authorized_user_info", return_value=creds), \
             patch.object(oauth, "_refresh_token", return_value=creds) as refresh:
            assert oauth.get_credentials("c.json", str(tmp_path / "t.json")) is creds
        refresh.assert_called_once_with(creds, token_path=None)

    def test_unrefreshable_env_token_raises(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", json.dumps(_TOKEN))
        creds = MagicMock(valid=False, refresh_token="refresh")
        with patch.object(oauth.Credentials, "from_authorized_user_info", return_value=creds), \
             patch.object(oauth, "_refresh_token", return_value=None):
            with pytest.raises(RuntimeError, match="could not be refreshed"):
                oauth.get_credentials("c.json", "t.json")

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", "{not json")
        with pytest.raises(RuntimeError, match="GOOGLE_TOKEN_JSON"):
            oauth.get_credentials("c.json", "t.json")


class TestHeadless:
    def test_headless_never_opens_browser(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GMAILCLEANER_HEADLESS", "1")
        with patch.object(oauth, "_run_consent_flow") as flow:
            with pytest.raises(RuntimeError, match="headless"):
                oauth.get_credentials(str(tmp_path / "c.json"), str(tmp_path / "t.json"))
        flow.assert_not_called()

    def test_cloud_run_detected_as_headless(self, monkeypatch):
        monkeypatch.setenv("CLOUD_RUN_JOB", "gmail-cleaner")
        assert oauth._is_headless()

    def test_explicit_flag_overrides_detection(self, monkeypatch):
        monkeypatch.setenv("K_SERVICE", "svc")
        monkeypatch.setenv("GMAILCLEANER_HEADLESS", "false")
        assert not oauth._is_headless()


class TestLocal:
    def test_missing_credentials_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            oauth.get_credentials(str(tmp_path / "c.json"), str(tmp_path / "t.json"))

    def test_load_credentials_uses_config_paths(self):
        cfg = {"auth": {"credentials_path": "a.json", "token_path": "b.json"}}
        with patch.object(oauth, "get_credentials") as gc:
            oauth.load_credentials(cfg)
        gc.assert_called_once_with("a.json", "b.json")
