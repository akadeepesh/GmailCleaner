"""
GmailCleaner — local secret storage (the Gemini API key).

Lookup order for get_gemini_key():
  1. GEMINI_API_KEY environment variable (Cloud Run, Docker, .env) — always wins
  2. The OS keychain via `keyring` (service "GmailCleaner", user "gemini_api_key")
  3. <home>/secrets.json (or config/secrets.json in legacy mode), mode 0600 —
     used only when no keychain backend is available

Headless runs (containers) never touch the keychain.

The key is never returned to the browser: the dashboard only learns whether
a key is stored and its last four characters (see key_hint()).
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from core.fsutil import atomic_write_text
from core.paths import app_home, is_headless

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "GmailCleaner"
KEYRING_USER = "gemini_api_key"
_FILE_FIELD = "gemini_api_key"


def secrets_file() -> Path:
    home = app_home()
    return home / "secrets.json" if home is not None else Path("config") / "secrets.json"


def get_gemini_key() -> str:
    env = os.getenv("GEMINI_API_KEY", "").strip()
    if env:
        return env
    if not is_headless():
        stored = _keyring_get()
        if stored:
            return stored
    return _file_get() or ""


def has_gemini_key() -> bool:
    return bool(get_gemini_key())


def key_source() -> Optional[str]:
    """Where the active key comes from: "env", "keychain", "file" or None."""
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "env"
    if not is_headless() and _keyring_get():
        return "keychain"
    if _file_get():
        return "file"
    return None


def key_hint() -> dict:
    """Safe-to-display info: never the key itself."""
    key = get_gemini_key()
    return {
        "has_key": bool(key),
        "last4": key[-4:] if len(key) >= 8 else "",
        "source": key_source(),
    }


def set_gemini_key(value: Optional[str]) -> str:
    """
    Store (or with None/"" remove) the Gemini key.

    Returns where it was stored: "keychain", "file" or "removed".
    """
    value = (value or "").strip()
    if not value:
        _keyring_delete()
        _file_set(None)
        return "removed"
    if not is_headless() and _keyring_set(value):
        _file_set(None)  # don't leave a stale plaintext copy behind
        return "keychain"
    _file_set(value)
    return "file"


# ──────────────────────────────────────────────────────────────────────────────
# Keychain
# ──────────────────────────────────────────────────────────────────────────────

def _keyring_get() -> Optional[str]:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as exc:  # no backend, locked keychain, DBus missing…
        logger.debug("Keychain unavailable for read: %s", type(exc).__name__)
        return None


def _keyring_set(value: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value)
        return True
    except Exception as exc:
        logger.info("Keychain unavailable (%s); storing the key in a private file instead.",
                    type(exc).__name__)
        return False


def _keyring_delete() -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# File fallback
# ──────────────────────────────────────────────────────────────────────────────

def _file_read() -> dict:
    path = secrets_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_get() -> Optional[str]:
    value = _file_read().get(_FILE_FIELD)
    return value if isinstance(value, str) and value else None


def _file_set(value: Optional[str]) -> None:
    data = _file_read()
    if value:
        data[_FILE_FIELD] = value
    elif _FILE_FIELD in data:
        del data[_FILE_FIELD]
    else:
        return
    path = secrets_file()
    if not data:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(path, json.dumps(data), mode=0o600)
