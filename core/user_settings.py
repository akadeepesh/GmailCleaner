"""
GmailCleaner — settings the dashboard is allowed to change.

The Settings page sends a flat dict keyed by dotted names
({"scan.lookback_days": 14, "trusted_rules": [...]}). Only the keys in FIELDS
are accepted; everything else is rejected so the GUI can never write paths,
tokens or other settings that would weaken the safety model.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from engines.rules import parse_rules

_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_LIST = 500

RULE_ACTIONS = ("unsubscribe", "delete", "both")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
POLL_CHOICES = (0, 15, 30, 60)   # what the Settings page offers; any 5–1440 is accepted


class SettingsValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class Field:
    kind: str                                  # bool | int | float | str | list
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_len: int = 500
    check: Optional[Callable[[Any], Any]] = None   # extra validator: returns cleaned value or raises ValueError


def _check_model(value: str) -> str:
    if not _MODEL_RE.match(value):
        raise ValueError("Use a model name like gemini-3.5-flash")
    return value


def _check_recipient(value: str) -> str:
    value = value.strip()
    if value and not _EMAIL_RE.match(value.lower()):
        raise ValueError("Enter an email address, or leave it blank to send to yourself")
    return value


def _check_time(value: str) -> str:
    value = value.strip()
    if not _TIME_RE.match(value):
        raise ValueError("Use a time like 08:00 (24-hour clock)")
    return value


def _check_days(values: list) -> list[str]:
    if not all(isinstance(v, str) for v in values):
        raise ValueError("Unknown day")
    picked = {v.strip().lower()[:3] for v in values}
    if not picked <= set(DAYS):
        raise ValueError("Unknown day")
    if not picked:
        raise ValueError("Pick at least one day")
    return [d for d in DAYS if d in picked]


def _check_poll(value: int) -> int:
    if value != 0 and value < 5:
        raise ValueError("Use 0 (off) or at least 5 minutes")
    return value


def _check_blocked(values: list) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("Each entry must be text")
        entry = raw.strip().lower()
        if not entry:
            continue
        if not (_DOMAIN_RE.match(entry) and not entry.startswith("*.")) and not _EMAIL_RE.match(entry):
            raise ValueError(f"{raw!r} is not a domain (example.com) or email address")
        if entry not in cleaned:
            cleaned.append(entry)
    return cleaned


def _check_rules(values: list) -> list:
    """
    Accept rows of {"action": unsubscribe|delete|both, "domain": "..."} and
    return entries in the settings.yaml trusted_rules format.

    A rule must name a real domain: bare "*" and single-label wildcards like
    "*.com" are rejected so one rule can never cover everyone.
    """
    cleaned: list = []
    for row in values:
        if isinstance(row, str):
            row = {"action": "both", "domain": row}
        if not isinstance(row, dict):
            raise ValueError("Each rule needs an action and a domain")
        action = str(row.get("action", "")).strip().lower()
        domain = str(row.get("domain", "")).strip().lower()
        if action not in RULE_ACTIONS:
            raise ValueError(f"Unknown action {action!r}")
        if not domain or not _DOMAIN_RE.match(domain):
            raise ValueError(
                f"{domain or '(empty)'!r} is not a specific domain. "
                "Use example.com, or *.example.com for all its subdomains."
            )
        entry: Any = domain if action == "both" else {"action": action, "domain": domain}
        # Reuse the engine's own parser as the final word on validity.
        expected = 2 if action == "both" else 1
        if len(parse_rules([entry])) != expected:
            raise ValueError(f"{domain!r} is not a valid rule")
        if entry not in cleaned:
            cleaned.append(entry)
    return cleaned


FIELDS: dict[str, Field] = {
    "scan.lookback_days": Field("int", 1, 365),
    "scan.max_emails": Field("int", 50, 5000),
    "scan.query_filter": Field("str", max_len=500),
    "unsubscribe.enabled": Field("bool"),
    "unsubscribe.min_email_count": Field("int", 1, 100),
    "bulk_cleanup.enabled": Field("bool"),
    "bulk_cleanup.min_group_size": Field("int", 2, 1000),
    "triage.enabled": Field("bool"),
    "triage.confidence_threshold": Field("float", 0.5, 0.99),
    "triage.model": Field("str", max_len=64, check=_check_model),
    "digest.enabled": Field("bool"),
    "digest.recipient": Field("str", max_len=254, check=_check_recipient),
    "confirm_first": Field("bool"),
    "trusted_rules": Field("list", check=_check_rules),
    "blocked_senders": Field("list", check=_check_blocked),
    "schedule.enabled": Field("bool"),
    "schedule.time": Field("str", max_len=16, check=_check_time),
    "schedule.days": Field("list", check=_check_days),
    "schedule.run_trusted_rules": Field("bool"),
    "schedule.catch_up": Field("bool"),
    "realtime.poll_minutes": Field("int", 0, 1440, check=_check_poll),
    "updates.check": Field("bool"),
}


def validate_changes(changes: Any) -> tuple[dict, dict[str, str]]:
    """
    Validate a flat {dotted_key: value} dict.

    Returns:
        (clean, errors). clean holds normalized values for the valid keys;
        errors maps each rejected key to a plain-language message.
    """
    if not isinstance(changes, dict):
        return {}, {"_": "Expected an object of settings"}
    clean: dict = {}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        spec = FIELDS.get(key)
        if spec is None:
            errors[key] = "This setting can't be changed here"
            continue
        try:
            clean[key] = _validate(spec, value)
        except ValueError as exc:
            errors[key] = str(exc)
    return clean, errors


def _validate(spec: Field, value: Any) -> Any:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("Must be on or off")
        return value
    if spec.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Must be a number")
        if spec.kind == "int":
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("Must be a whole number")
            value = int(value)
        else:
            value = float(value)
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"Must be at least {_fmt(spec.minimum)}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"Must be at most {_fmt(spec.maximum)}")
        return spec.check(value) if spec.check else value
    if spec.kind == "str":
        if not isinstance(value, str):
            raise ValueError("Must be text")
        if len(value) > spec.max_len:
            raise ValueError(f"Must be at most {spec.max_len} characters")
        return spec.check(value) if spec.check else value
    if spec.kind == "list":
        if not isinstance(value, list):
            raise ValueError("Must be a list")
        if len(value) > _MAX_LIST:
            raise ValueError(f"At most {_MAX_LIST} entries")
        return spec.check(value) if spec.check else value
    raise ValueError("Unsupported setting")


def to_nested(flat: dict) -> dict:
    """{"scan.lookback_days": 3} → {"scan": {"lookback_days": 3}}"""
    nested: dict = {}
    for key, value in flat.items():
        node = nested
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return nested


def current_values(cfg: dict) -> dict:
    """The current value of every editable setting, flat, for the Settings page."""
    values = {}
    for key in FIELDS:
        node: Any = cfg
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        values[key] = node
    values["trusted_rules"] = rules_to_rows(cfg.get("trusted_rules") or [])
    values["blocked_senders"] = list(cfg.get("blocked_senders") or [])
    if values["confirm_first"] is None:
        values["confirm_first"] = True
    for key, default in SCHEDULE_DEFAULTS.items():
        if values[key] is None:
            values[key] = default
    return values


# Used when settings.yaml predates these keys (e.g. a custom --config file).
SCHEDULE_DEFAULTS = {
    "schedule.enabled": False,
    "schedule.time": "08:00",
    "schedule.days": list(DAYS),
    "schedule.run_trusted_rules": True,
    "schedule.catch_up": True,
    "realtime.poll_minutes": 0,
    "updates.check": True,
}


def rules_to_rows(rules: list) -> list[dict]:
    """settings.yaml trusted_rules → [{"action": ..., "domain": ...}] rows for the editor."""
    rows = []
    for rule in rules:
        if isinstance(rule, str):
            rows.append({"action": "both", "domain": rule})
        elif isinstance(rule, dict):
            rows.append({"action": str(rule.get("action", "")), "domain": str(rule.get("domain", ""))})
    return rows


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)
