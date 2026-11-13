"""
GmailCleaner — "Start when I log in" (opt-in, no admin rights).

    Windows  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run  value "GmailCleaner"
    macOS    ~/Library/LaunchAgents/com.gmailcleaner.app.plist (RunAtLoad)
    Linux    ~/.config/autostart/gmailcleaner.desktop

Each starts the installed app with --background (tray only, no browser
window). Only offered in the installed (frozen) app: in a dev checkout there
is no stable program to point at.
"""

import os
import plistlib
import sys
from pathlib import Path
from typing import Optional

from core.fsutil import atomic_write_text
from core.paths import is_frozen

APP_NAME = "GmailCleaner"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_LABEL = "com.gmailcleaner.app"
BACKGROUND_FLAG = "--background"


def is_supported() -> bool:
    return is_frozen()


def command() -> list[str]:
    return [sys.executable, BACKGROUND_FLAG]


def _platform(platform: Optional[str]) -> str:
    p = platform or sys.platform
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "mac"
    return "linux"


def _home(home: Optional[Path]) -> Path:
    return Path(home) if home is not None else Path.home()


# ── Public API ───────────────────────────────────────────────────────────────

def is_enabled(*, platform: Optional[str] = None, home: Optional[Path] = None) -> bool:
    kind = _platform(platform)
    if kind == "windows":
        return _win_get() is not None
    return _file_path(kind, home).is_file()


def enable(*, platform: Optional[str] = None, home: Optional[Path] = None) -> None:
    kind = _platform(platform)
    cmd = command()
    if kind == "windows":
        _win_set(" ".join([f'"{cmd[0]}"', *cmd[1:]]))
    elif kind == "mac":
        data = plistlib.dumps({
            "Label": MAC_LABEL,
            "ProgramArguments": cmd,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }).decode("utf-8")
        atomic_write_text(_file_path(kind, home), data)
    else:
        atomic_write_text(_file_path(kind, home), _desktop_entry(cmd))


def disable(*, platform: Optional[str] = None, home: Optional[Path] = None) -> None:
    kind = _platform(platform)
    if kind == "windows":
        _win_delete()
    else:
        _file_path(kind, home).unlink(missing_ok=True)


def set_enabled(enabled: bool, **kwargs) -> bool:
    (enable if enabled else disable)(**kwargs)
    return is_enabled(**kwargs)


# ── macOS / Linux files ──────────────────────────────────────────────────────

def _file_path(kind: str, home: Optional[Path]) -> Path:
    h = _home(home)
    if kind == "mac":
        return h / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
    xdg = os.getenv("XDG_CONFIG_HOME") if home is None else None
    base = Path(xdg) if xdg else h / ".config"
    return base / "autostart" / "gmailcleaner.desktop"


def _desktop_quote(arg: str) -> str:
    """Quote an Exec= argument per the freedesktop Desktop Entry spec."""
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


def _desktop_entry(cmd: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Tidy your Gmail inbox\n"
        f"Exec={' '.join(_desktop_quote(c) for c in cmd)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


# ── Windows registry ─────────────────────────────────────────────────────────

def _winreg():
    import winreg  # noqa: PLC0415 - Windows only
    return winreg


def _win_get() -> Optional[str]:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return value
    except FileNotFoundError:
        return None


def _win_set(value: str) -> None:
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, value)


def _win_delete() -> None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
