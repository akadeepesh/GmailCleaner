"""
Tests for desktop/autostart.py

Covers:
  - Only supported in the frozen app
  - Windows: the HKCU Run value is written, read and deleted (fake winreg)
  - macOS: LaunchAgent plist with RunAtLoad and --background
  - Linux: ~/.config/autostart .desktop entry, with Exec quoting
"""

import plistlib
import sys
import types

import pytest

from desktop import autostart

EXE = r"C:\Users\Jöhn Doe\AppData\Local\Programs\GmailCleaner\GmailCleaner.exe"


@pytest.fixture
def exe(monkeypatch):
    monkeypatch.setattr(sys, "executable", EXE)
    return EXE


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        assert (root, path) == ("HKCU", autostart.RUN_KEY)
        return self._Key()

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_supported_only_when_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert autostart.is_supported() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert autostart.is_supported() is True


def test_windows(monkeypatch, exe):
    reg = FakeWinreg()
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    assert autostart.is_enabled(platform="win32") is False
    autostart.enable(platform="win32")
    assert reg.values["GmailCleaner"] == f'"{EXE}" --background'
    assert autostart.is_enabled(platform="win32") is True
    autostart.disable(platform="win32")
    assert autostart.is_enabled(platform="win32") is False
    autostart.disable(platform="win32")   # already off: no error


def test_mac(tmp_path, exe):
    assert autostart.set_enabled(True, platform="darwin", home=tmp_path) is True
    plist = tmp_path / "Library" / "LaunchAgents" / "com.gmailcleaner.app.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [EXE, "--background"]
    assert data["RunAtLoad"] is True and data["Label"] == "com.gmailcleaner.app"
    assert autostart.set_enabled(False, platform="darwin", home=tmp_path) is False
    assert not plist.exists()


def test_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", '/opt/Gmail Cleaner/$weird"name')
    autostart.enable(platform="linux", home=tmp_path)
    entry = tmp_path / ".config" / "autostart" / "gmailcleaner.desktop"
    text = entry.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in text and "Type=Application" in text
    assert 'Exec="/opt/Gmail Cleaner/\\$weird\\"name" "--background"' in text
    assert autostart.is_enabled(platform="linux", home=tmp_path)
    autostart.disable(platform="linux", home=tmp_path)
    assert not autostart.is_enabled(platform="linux", home=tmp_path)


def test_linux_respects_xdg_config_home(tmp_path, monkeypatch, exe):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(autostart.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    autostart.enable(platform="linux")
    assert (tmp_path / "xdg" / "autostart" / "gmailcleaner.desktop").is_file()
