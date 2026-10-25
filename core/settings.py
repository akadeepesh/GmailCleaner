"""
GmailCleaner — Runtime Settings
Loads config/settings.yaml and resolves every runtime path in one place.

Resolution order for each setting (first match wins):
  1. Environment variable (used by Cloud Run, Docker, CI)
  2. Value in settings.yaml
  3. Built-in default

Multi-account support:
  Passing an account name (CLI --account, or GMAILCLEANER_ACCOUNT env var)
  namespaces the token, database and pending-actions file so that several
  Gmail accounts can be managed from one checkout without sharing state:

    config/tokens/<account>.json
    data/<account>/gmailcleaner.db
    output/<account>/pending_actions.json

  Explicit environment overrides still win over the namespaced defaults.
"""

import copy
import os
import re
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CONFIG_PATH = "config/settings.yaml"
DEFAULT_CREDENTIALS_PATH = "config/credentials.json"
DEFAULT_TOKEN_PATH = "config/token.json"
DEFAULT_DB_PATH = "data/gmailcleaner.db"
DEFAULT_PENDING_PATH = "output/pending_actions.json"

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def load_config(config_path: Optional[str] = None, account: Optional[str] = None) -> dict:
    """
    Load settings.yaml and return a fully resolved config dict.

    Args:
        config_path: Path to settings.yaml. Defaults to GMAILCLEANER_CONFIG or
                     config/settings.yaml.
        account:     Optional account name for multi-account namespacing.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError:        If the account name is invalid.
    """
    path = Path(config_path or os.getenv("GMAILCLEANER_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Ensure config/settings.yaml exists."
        )
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
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

    db_path = db_cfg.get("path") or DEFAULT_DB_PATH
    pending_path = out_cfg.get("pending_actions_path") or DEFAULT_PENDING_PATH
    token_path = auth_cfg.get("token_path") or DEFAULT_TOKEN_PATH
    if account:
        db_path = _namespaced(db_path, account)
        pending_path = _namespaced(pending_path, account)
        token_path = str(Path(token_path).parent / "tokens" / f"{account}.json")

    db_cfg["path"] = os.getenv("DATABASE_PATH") or db_path
    out_cfg["pending_actions_path"] = os.getenv("PENDING_ACTIONS_PATH") or pending_path
    auth_cfg["credentials_path"] = (
        os.getenv("GOOGLE_CREDENTIALS_PATH")
        or auth_cfg.get("credentials_path")
        or DEFAULT_CREDENTIALS_PATH
    )
    auth_cfg["token_path"] = os.getenv("GOOGLE_TOKEN_PATH") or token_path
    pubsub_cfg["topic_name"] = os.getenv("PUBSUB_TOPIC") or pubsub_cfg.get("topic_name") or ""
    digest_cfg["recipient"] = os.getenv("DIGEST_RECIPIENT") or digest_cfg.get("recipient") or ""

    cfg["account"] = account
    return cfg


def _namespaced(path: str, account: str) -> str:
    """data/gmailcleaner.db + 'work' → data/work/gmailcleaner.db"""
    p = Path(path)
    return str(p.parent / account / p.name)
