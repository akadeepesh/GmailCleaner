"""
GmailCleaner — app home and bundled resources.

Two layouts are supported:

  * Legacy (developer) mode: no GMAILCLEANER_HOME and not frozen. Every path
    stays relative to the current directory / repo root exactly as before
    (config/token.json, data/…, output/…).

  * App-home mode: GMAILCLEANER_HOME is set, or the app runs as a PyInstaller
    build. All writable state lives in one per-user directory:

        Windows  %LOCALAPPDATA%\\GmailCleaner
        macOS    ~/Library/Application Support/GmailCleaner
        Linux    ~/.local/share/GmailCleaner

Read-only resources (templates, default settings, bundled OAuth client) are
located with resource_path(), which also works inside a PyInstaller bundle.
"""

import os
import sys
from pathlib import Path
from typing import Optional, Union

APP_NAME = "GmailCleaner"
REPO_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_home() -> Optional[Path]:
    """
    The per-user data directory, or None in legacy repo-relative mode.
    """
    env = os.getenv("GMAILCLEANER_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if is_frozen():
        import platformdirs

        return Path(platformdirs.user_data_path(APP_NAME, appauthor=False))
    return None


def resource_path(rel: Union[str, Path]) -> Path:
    """Absolute path to a bundled, read-only resource."""
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT)) if is_frozen() else REPO_ROOT
    return base / rel


def is_headless() -> bool:
    """True when running in a container / CI where a browser flow is impossible."""
    flag = os.getenv("GMAILCLEANER_HEADLESS", "").lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    # Cloud Run sets K_SERVICE (services) or CLOUD_RUN_JOB (jobs).
    return bool(os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"))


def is_desktop_mode() -> bool:
    """The local GUI app: an app home is in use and a browser is available."""
    return app_home() is not None and not is_headless()
