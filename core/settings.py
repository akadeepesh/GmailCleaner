"""
GmailCleaner — Runtime Settings
Loads config/settings.yaml and resolves every runtime path in one place.

Resolution order for each setting (first match wins):
  1. Environment variable (used by Cloud Run, Docker, CI)
  2. User overrides written by the dashboard (settings.user.yaml)
  3. Value in settings.yaml
  4. Built-in default

Multi-account support:
  Passing an account name (CLI --account, or GMAILCLEANER_ACCOUNT env var)
  namespaces the token, database and pending-actions file so that several
  Gmail accounts can be managed from one checkout without sharing state:

    config/tokens/<account>.json
    data/<account>/gmailcleaner.db
    output/<account>/pending_actions.json

  Explicit environment overrides still win over the namespaced defaults.

App-home mode (desktop app, see core/paths.py):
  When GMAILCLEANER_HOME is set (or the app is frozen), relative paths are
  anchored in that directory instead of the current directory, the unnamed
  account's token is <home>/token.json and named accounts use
  <home>/tokens/<account>.json.

User overrides:
  The GUI never rewrites settings.yaml. It writes only the keys the user
  changed to settings.user.yaml (<home>/settings.user.yaml, or
  config/settings.user.yaml in legacy mode), which load_config deep-merges on
  top of the shipped defaults.
"""

import copy
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from core.fsutil import atomic_write_text
from core.paths import app_home, resource_path

DEFAULT_CONFIG_PATH = "config/settings.yaml"
DEFAULT_CREDENTIALS_PATH = "config/credentials.json"
DEFAULT_TOKEN_PATH = "config/token.json"
DEFAULT_DB_PATH = "data/gmailcleaner.db"
DEFAULT_PENDING_PATH = "output/pending_actions.json"
DEFAULT_LOG_PATH = "logs/gmailcleaner.log"
DEFAULT_EXPORTS_DIR = "exports"
USER_OVERRIDES_NAME = "settings.user.yaml"

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def load_config(config_path: Optional[str] = None, account: Optional[str] = None) -> dict:
    """
    Load settings.yaml and return a fully resolved config dict.

    Args:
        config_path: Path to settings.yaml. Defaults to GMAILCLEANER_CONFIG or
                     config/settings.yaml.
        account:     Optional account name for multi-account namespacing.

    The user overrides file is merged only when the default settings file is
    used: an explicit config_path / GMAILCLEANER_CONFIG is taken as-is.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError:        If the account name is invalid.
    """
    explicit = config_path or os.getenv("GMAILCLEANER_CONFIG")
    path = Path(explicit or default_config_path())
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Ensure config/settings.yaml exists."
        )
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not explicit:
        raw = deep_merge(raw, load_user_overrides())
    return resolve_config(raw, account=account)


def resolve_config(raw: dict, account: Optional[str] = None) -> dict:
    """
    Apply environment overrides and account namespacing to a raw config dict.

    Returns a new dict; the input is not modified.
    """
    cfg = copy.deepcopy(raw or {})
    account = account or os.getenv("GMAILCLEANER_ACCOUNT") or None
    if account and not _ACCOUNT_RE.match(account):
        raise ValueError(
            f"Invalid account name {account!r}. Use letters, digits, '.', '_' or '-'."
        )

    db_cfg = cfg.setdefault("database", {})
    out_cfg = cfg.setdefault("output", {})
    auth_cfg = cfg.setdefault("auth", {})
    pubsub_cfg = cfg.setdefault("pubsub", {})
    digest_cfg = cfg.setdefault("digest", {})

    home = app_home()
    db_path = db_cfg.get("path") or DEFAULT_DB_PATH
    pending_path = out_cfg.get("pending_actions_path") or DEFAULT_PENDING_PATH
    token_path = auth_cfg.get("token_path") or ("token.json" if home else DEFAULT_TOKEN_PATH)
    credentials_path = auth_cfg.get("credentials_path") or (
        "credentials.json" if home else DEFAULT_CREDENTIALS_PATH
    )
    exports_dir = out_cfg.get("exports_dir") or DEFAULT_EXPORTS_DIR
    if account:
        db_path = _namespaced(db_path, account)
        pending_path = _namespaced(pending_path, account)
        token_path = str(Path(token_path).parent / "tokens" / f"{account}.json")

    if home is not None:
        db_path, pending_path, token_path, credentials_path, exports_dir = (
            str(_anchor(p, home))
            for p in (db_path, pending_path, token_path, credentials_path, exports_dir)
        )
        log_cfg = cfg.setdefault("logging", {})
        log_cfg["file_path"] = str(_anchor(log_cfg.get("file_path") or DEFAULT_LOG_PATH, home))

    db_cfg["path"] = os.getenv("DATABASE_PATH") or db_path
    out_cfg["pending_actions_path"] = os.getenv("PENDING_ACTIONS_PATH") or pending_path
    out_cfg["exports_dir"] = exports_dir
    auth_cfg["credentials_path"] = os.getenv("GOOGLE_CREDENTIALS_PATH") or credentials_path
    auth_cfg["token_path"] = os.getenv("GOOGLE_TOKEN_PATH") or token_path
    pubsub_cfg["topic_name"] = os.getenv("PUBSUB_TOPIC") or pubsub_cfg.get("topic_name") or ""
    digest_cfg["recipient"] = os.getenv("DIGEST_RECIPIENT") or digest_cfg.get("recipient") or ""

    cfg["account"] = account
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# User overrides (written by the dashboard's Settings page)
# ──────────────────────────────────────────────────────────────────────────────

def default_config_path() -> Path:
    """The shipped settings.yaml: a bundled resource in app-home mode, CWD-relative otherwise."""
    if app_home() is not None:
        return resource_path(DEFAULT_CONFIG_PATH)
    return Path(DEFAULT_CONFIG_PATH)


def user_overrides_path() -> Path:
    home = app_home()
    return home / USER_OVERRIDES_NAME if home is not None else Path("config") / USER_OVERRIDES_NAME


def load_user_overrides() -> dict:
    """Return the user overrides dict ({} if the file is missing or unreadable)."""
    path = user_overrides_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def save_user_overrides(changes: dict) -> dict:
    """
    Validate a partial settings update and merge it into the overrides file.

    Args:
        changes: Flat dict keyed by dotted setting names, e.g.
                 {"scan.lookback_days": 14, "confirm_first": True}.

    Returns:
        The full overrides dict as written.

    Raises:
        SettingsValidationError: with field-level messages; nothing is written.
    """
    from core.user_settings import SettingsValidationError, to_nested, validate_changes

    clean, errors = validate_changes(changes)
    if errors:
        raise SettingsValidationError(errors)
    merged = deep_merge(load_user_overrides(), to_nested(clean))
    atomic_write_text(
        user_overrides_path(),
        "# Written by the GmailCleaner app. Only the settings you changed are here;\n"
        "# everything else comes from the shipped settings.yaml.\n"
        + yaml.safe_dump(merged, sort_keys=True, allow_unicode=True),
    )
    return merged


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Lists and scalars are replaced."""
    result = copy.deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _anchor(path: str, home: Path) -> Path:
    """Relative paths live under the app home; absolute paths are kept."""
    p = Path(path)
    return p if p.is_absolute() else home / p


def _namespaced(path: str, account: str) -> str:
    """data/gmailcleaner.db + 'work' → data/work/gmailcleaner.db"""
    p = Path(path)
    return str(p.parent / account / p.name)
