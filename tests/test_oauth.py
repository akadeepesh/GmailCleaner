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


class TestNonInteractive:
    def test_no_token_raises_sign_in_required(self, tmp_path):
        with patch.object(oauth, "_run_consent_flow") as flow:
            with pytest.raises(oauth.SignInRequired):
                oauth.get_credentials(str(tmp_path / "c.json"), str(tmp_path / "t.json"),
                                      interactive=False)
        flow.assert_not_called()

    def test_load_credentials_passes_flag(self):
        cfg = {"auth": {"credentials_path": "a.json", "token_path": "b.json"}}
        with patch.object(oauth, "get_credentials") as gc:
            oauth.load_credentials(cfg, interactive=False)
        gc.assert_called_once_with("a.json", "b.json", interactive=False)


_CLIENT = {"installed": {
    "client_id": "123-abc.apps.googleusercontent.com",
    "client_secret": "s",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}


class TestClientConfig:
    @pytest.fixture(autouse=True)
    def home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH", raising=False)
        monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)  # no config/credentials.json here
        monkeypatch.setattr(oauth, "resource_path", lambda rel: tmp_path / "bundle" / rel)
        return tmp_path

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_none_when_missing(self):
        assert oauth.resolve_client_config() is None

    def test_lookup_order(self, home, monkeypatch):
        def client(n):
            return {"installed": dict(_CLIENT["installed"], client_id=f"{n}.apps.googleusercontent.com")}

        self._write(home / "bundle" / "bundled" / "oauth_client.json", client("bundled"))
        assert oauth.resolve_client_config()["installed"]["client_id"].startswith("bundled")
        self._write(home / "config" / "credentials.json", client("dev"))
        assert oauth.resolve_client_config()["installed"]["client_id"].startswith("dev")
        self._write(home / "home" / "credentials.json", client("user"))
        assert oauth.resolve_client_config()["installed"]["client_id"].startswith("user")
        self._write(home / "env.json", client("env"))
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(home / "env.json"))
        assert oauth.resolve_client_config()["installed"]["client_id"].startswith("env")

    def test_invalid_file_skipped(self, home):
        self._write(home / "home" / "credentials.json", {"nope": 1})
        self._write(home / "bundle" / "bundled" / "oauth_client.json", _CLIENT)
        assert oauth.resolve_client_config() == _CLIENT

    @pytest.mark.parametrize("bad", [
        [], {}, {"web": _CLIENT["installed"]},
        {"installed": {"client_id": "x"}},
        {"installed": dict(_CLIENT["installed"], client_id="evil.example.com")},
    ])
    def test_upload_validation(self, bad):
        with pytest.raises(ValueError):
            oauth.save_user_client_config(bad)

    def test_upload_saved(self, home):
        path = oauth.save_user_client_config(_CLIENT)
        assert path == home / "home" / "credentials.json"
        assert json.loads(path.read_text(encoding="utf-8")) == _CLIENT


class TestTokenHelpers:
    def test_token_status(self, tmp_path):
        assert oauth.token_status(str(tmp_path / "none.json")) == "needs_sign_in"
        path = tmp_path / "t.json"
        path.write_text(json.dumps(_TOKEN), encoding="utf-8")
        assert oauth.token_status(str(path)) == "signed_in"

    def test_save_token_atomic(self, tmp_path):
        creds = MagicMock()
        creds.to_json.return_value = json.dumps(_TOKEN)
        oauth.save_token(creds, str(tmp_path / "tokens" / "a.json"))
        assert [p.name for p in (tmp_path / "tokens").iterdir()] == ["a.json"]

    def test_revoke(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(_TOKEN), encoding="utf-8")
        with patch.object(oauth.httpx, "post", return_value=MagicMock(status_code=200)) as post:
            assert oauth.revoke_token(str(path)) is True
        assert post.call_args.kwargs["data"] == {"token": "refresh"}

    def test_revoke_network_error(self, tmp_path):
        import httpx
        path = tmp_path / "t.json"
        path.write_text(json.dumps(_TOKEN), encoding="utf-8")
        with patch.object(oauth.httpx, "post", side_effect=httpx.ConnectError("x")):
            assert oauth.revoke_token(str(path)) is False
