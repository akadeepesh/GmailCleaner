"""
Tests for core/secrets.py (keyring is always mocked)

Covers:
  - GEMINI_API_KEY env wins over stored keys
  - Keychain store / read / delete
  - File fallback (0600) when the keychain is unavailable
  - Saving to the keychain removes a stale plaintext copy
  - Headless runs never touch the keychain
  - key_hint never contains the key
"""

import json
import sys
import types

import pytest

from core import secrets


class FakeKeyring(types.ModuleType):
    def __init__(self, broken: bool = False):
        super().__init__("keyring")
        self.store: dict = {}
        self.broken = broken
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.broken:
            raise RuntimeError("No recommended backend")

    def get_password(self, service, user):
        self._check()
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self._check()
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self._check()
        self.store.pop((service, user), None)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ("GEMINI_API_KEY", "GMAILCLEANER_HEADLESS", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path))
    return tmp_path


def _install(monkeypatch, broken=False) -> FakeKeyring:
    fake = FakeKeyring(broken)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_env_wins(monkeypatch):
    fake = _install(monkeypatch)
    fake.store[("GmailCleaner", "gemini_api_key")] = "stored-key-1234"
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-abcd")
    assert secrets.get_gemini_key() == "env-key-abcd"
    assert secrets.key_source() == "env"


def test_keychain_round_trip(monkeypatch, env):
    fake = _install(monkeypatch)
    assert secrets.set_gemini_key("AIza-secret-9876") == "keychain"
    assert secrets.get_gemini_key() == "AIza-secret-9876"
    assert not (env / "secrets.json").exists()
    assert secrets.set_gemini_key(None) == "removed"
    assert secrets.get_gemini_key() == ""
    assert fake.store == {}


def test_file_fallback(monkeypatch, env):
    _install(monkeypatch, broken=True)
    assert secrets.set_gemini_key("AIza-secret-9876") == "file"
    path = env / "secrets.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"gemini_api_key": "AIza-secret-9876"}
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert secrets.get_gemini_key() == "AIza-secret-9876"
    assert secrets.key_source() == "file"
    secrets.set_gemini_key("")
    assert not path.exists()


def test_keychain_save_removes_plaintext(monkeypatch, env):
    _install(monkeypatch, broken=True)
    secrets.set_gemini_key("old-file-key-0000")
    _install(monkeypatch, broken=False)
    assert secrets.set_gemini_key("new-chain-key-1111") == "keychain"
    assert not (env / "secrets.json").exists()


def test_headless_skips_keychain(monkeypatch, env):
    fake = _install(monkeypatch)
    monkeypatch.setenv("GMAILCLEANER_HEADLESS", "1")
    assert secrets.set_gemini_key("AIza-secret-9876") == "file"
    secrets.get_gemini_key()
    assert fake.calls == 0


def test_hint_never_contains_key(monkeypatch):
    _install(monkeypatch)
    secrets.set_gemini_key("AIza-very-secret-key-WXYZ")
    hint = secrets.key_hint()
    assert hint == {"has_key": True, "last4": "WXYZ", "source": "keychain"}
    assert "very-secret" not in json.dumps(hint)
