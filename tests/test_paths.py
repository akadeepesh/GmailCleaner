"""
Tests for core/paths.py

Covers:
  - Legacy mode: no GMAILCLEANER_HOME and not frozen → app_home() is None
  - GMAILCLEANER_HOME wins
  - Frozen builds use the platformdirs per-user directory
  - resource_path() uses sys._MEIPASS when frozen, else the repo root
  - is_desktop_mode() needs an app home and a non-headless environment
"""

import sys
from pathlib import Path

import pytest

from core import paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GMAILCLEANER_HOME", "GMAILCLEANER_HEADLESS", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


def test_legacy_mode_has_no_home():
    assert paths.app_home() is None
    assert not paths.is_desktop_mode()


def test_env_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path / "my home"))
    assert paths.app_home() == tmp_path / "my home"
    assert paths.is_desktop_mode()


def test_frozen_uses_platformdirs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_data_path",
                        lambda name, appauthor=None: tmp_path / name)
    assert paths.app_home() == tmp_path / "GmailCleaner"


def test_env_beats_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path))
    assert paths.app_home() == tmp_path


def test_resource_path_dev():
    assert paths.resource_path("config/settings.yaml") == paths.REPO_ROOT / "config/settings.yaml"
    assert paths.resource_path("config/settings.yaml").exists()


def test_resource_path_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_path("web/base.html") == Path(tmp_path) / "web/base.html"


def test_headless_disables_desktop_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILCLEANER_HOME", str(tmp_path))
    monkeypatch.setenv("K_SERVICE", "svc")
    assert paths.is_headless()
    assert not paths.is_desktop_mode()
