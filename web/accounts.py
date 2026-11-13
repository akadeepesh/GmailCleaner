"""
GmailCleaner — accounts for the multi-account dashboard.

The dashboard picks the account per request (cookie gc_account, sidebar
switcher) instead of being bound to one account at startup. Each account has
a cached AccountContext (resolved config + open Database); the cache is
cleared when settings change so new values apply without a restart.

Account names:
  * Accounts added through the dashboard are named after their address:
    jane.doe@gmail.com → jane.doe_gmail.com (valid for core.settings._ACCOUNT_RE).
  * The legacy unnamed account (config/token.json, or <home>/token.json) is
    listed as "default" and addressed with the key LEGACY_KEY, which can never
    collide with a real account name.
  * When GMAILCLEANER_ACCOUNT is set (Cloud Run), the dashboard is pinned to
    that one account, exactly as before.
"""

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from auth.oauth import token_status
from core.fsutil import atomic_write_text
from core.paths import app_home
from core.settings import _ACCOUNT_RE, deep_merge, load_config, load_user_overrides, resolve_config
from store.db import Database

LEGACY_KEY = "_default"          # leading "_" is not a valid account name
_INDEX_NAME = "_index.json"      # name → email map, kept beside the tokens


def account_name_for_email(email: str) -> str:
    """jane.doe@gmail.com → jane.doe_gmail.com"""
    name = (email or "").strip().lower().replace("@", "_")
    name = re.sub(r"[^a-z0-9_.-]", "_", name).lstrip("._-")[:64]
    if not name or not _ACCOUNT_RE.match(name):
        raise ValueError(f"Cannot derive an account name from {email!r}")
    return name


def key_for(name: Optional[str]) -> str:
    return name or LEGACY_KEY


def name_for(key: Optional[str]) -> Optional[str]:
    return None if not key or key == LEGACY_KEY else key


@dataclass
class AccountContext:
    name: Optional[str]
    cfg: dict
    db: Database

    @property
    def key(self) -> str:
        return key_for(self.name)


@dataclass
class AccountInfo:
    name: Optional[str]
    email: Optional[str]
    token_path: str
    status: str                     # signed_in | needs_sign_in

    @property
    def key(self) -> str:
        return key_for(self.name)

    @property
    def label(self) -> str:
        return self.email or self.name or "default"

    @property
    def signed_in(self) -> bool:
        return self.status == "signed_in"


class AccountRegistry:
    """
    Args:
        raw_config: A raw settings dict (tests, embedding) or None to load
                    settings.yaml (+ user overrides) from disk.
    """

    def __init__(self, raw_config: Optional[dict] = None) -> None:
        self._raw = raw_config
        self._cache: dict[str, AccountContext] = {}
        self._lock = threading.RLock()
        self.pinned: Optional[str] = os.getenv("GMAILCLEANER_ACCOUNT") or None

    # ── Contexts ──────────────────────────────────────────────────────────────

    def get(self, name: Optional[str]) -> AccountContext:
        name = self.pinned or name
        key = key_for(name)
        with self._lock:
            ctx = self._cache.get(key)
            if ctx is None:
                cfg = self.resolve(name)
                ctx = AccountContext(name=name, cfg=cfg, db=Database(cfg["database"]["path"]))
                self._cache[key] = ctx
            return ctx

    def resolve(self, name: Optional[str]) -> dict:
        if self._raw is None:
            return load_config(account=name)
        raw = self._raw
        if app_home() is not None:
            raw = deep_merge(raw, load_user_overrides())
        return resolve_config(raw, account=name)

    def invalidate(self) -> None:
        """Drop every cached context (after a settings change)."""
        with self._lock:
            self._cache.clear()

    def evict(self, name: Optional[str]) -> None:
        with self._lock:
            self._cache.pop(key_for(name), None)

    # ── Listing ───────────────────────────────────────────────────────────────

    def legacy_token_path(self) -> Path:
        return Path(self.resolve(None)["auth"]["token_path"])

    def tokens_dir(self) -> Path:
        """Directory holding <account>.json tokens (config/tokens or <home>/tokens)."""
        sample = self.resolve("x")
        return Path(sample["auth"]["token_path"]).parent

    def list_accounts(self) -> list[AccountInfo]:
        if self.pinned:
            ctx = self.get(self.pinned)
            path = ctx.cfg["auth"]["token_path"]
            status = "signed_in" if os.getenv("GOOGLE_TOKEN_JSON") else token_status(path)
            return [AccountInfo(self.pinned, self.emails().get(self.pinned), path, status)]

        index = self.emails()
        accounts: list[AccountInfo] = []
        legacy = self.legacy_token_path()
        if legacy.is_file():
            accounts.append(AccountInfo(None, index.get(LEGACY_KEY), str(legacy),
                                        token_status(str(legacy))))
        tokens = self.tokens_dir()
        # GOOGLE_TOKEN_PATH points every account at one file: only "default" exists.
        if tokens.is_dir() and not os.getenv("GOOGLE_TOKEN_PATH"):
            for path in sorted(tokens.glob("*.json")):
                name = path.stem
                if not _ACCOUNT_RE.match(name):
                    continue
                accounts.append(AccountInfo(name, index.get(name), str(path),
                                            token_status(str(path))))
        return accounts

    def find(self, key: Optional[str]) -> Optional[AccountInfo]:
        return next((a for a in self.list_accounts() if a.key == key), None)

    # ── Email index ───────────────────────────────────────────────────────────

    def _index_path(self) -> Path:
        return self.tokens_dir() / _INDEX_NAME

    def emails(self) -> dict:
        try:
            data = json.loads(self._index_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember_email(self, name: Optional[str], email: str) -> None:
        with self._lock:
            data = self.emails()
            data[key_for(name)] = email
            atomic_write_text(self._index_path(), json.dumps(data, indent=2, sort_keys=True))

    def forget_email(self, name: Optional[str]) -> None:
        with self._lock:
            data = self.emails()
            if data.pop(key_for(name), None) is not None:
                atomic_write_text(self._index_path(), json.dumps(data, indent=2, sort_keys=True))
