"""
GmailCleaner — Trusted Rules Engine
Lets users pre-approve specific actions so they run without manual confirmation.

Rules are only honoured when `confirm_first: false` in settings.yaml. With the
default `confirm_first: true`, this engine never confirms anything.

Rule format (settings.yaml):

    trusted_rules:
      - action: unsubscribe        # unsubscribe | delete
        domain: news.example.com   # exact sender domain
      - action: delete
        domain: "*.shop.example"   # the domain itself and any subdomain
      - promo.example.org          # shorthand: both actions for this domain

Safety rules:
  - blocked_senders always win: a blocked domain is never auto-confirmed.
  - A bare "*" wildcard is rejected — every rule must name a domain.
  - Delete rules only match domain groups; subject-pattern groups span many
    senders and always require manual confirmation.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("unsubscribe", "delete")


@dataclass(frozen=True)
class TrustedRule:
    action: str   # "unsubscribe" | "delete"
    domain: str   # "example.com" or "*.example.com"

    def matches(self, action: str, domain: Optional[str]) -> bool:
        if action != self.action or not domain:
            return False
        domain = domain.lower()
        if self.domain.startswith("*."):
            base = self.domain[2:]
            return domain == base or domain.endswith("." + base)
        return domain == self.domain


class TrustedRules:
    """
    Parsed set of trusted rules plus the safety switches that gate them.

    Usage:
        rules = TrustedRules.from_config(cfg)
        if rules.active:
            count = rules.apply(report)   # marks matching items confirmed
    """

    def __init__(
        self,
        rules: list[TrustedRule],
        confirm_first: bool = True,
        blocked_senders: Optional[list[str]] = None,
    ) -> None:
        self.rules = rules
        self.confirm_first = confirm_first
        self._blocked = {d.lower() for d in (blocked_senders or [])}

    @classmethod
    def from_config(cls, cfg: dict) -> "TrustedRules":
        return cls(
            rules=parse_rules(cfg.get("trusted_rules") or []),
            confirm_first=cfg.get("confirm_first", True) is not False,
            blocked_senders=cfg.get("blocked_senders") or [],
        )

    @property
    def active(self) -> bool:
        """Rules only take effect when confirm_first is off and at least one rule exists."""
        return not self.confirm_first and bool(self.rules)

    def matches(self, action: str, domain: Optional[str]) -> bool:
        if not self.active or not domain or domain.lower() in self._blocked:
            return False
        return any(rule.matches(action, domain) for rule in self.rules)

    def apply(self, report: dict) -> int:
        """
        Mark every report item covered by a trusted rule as confirmed.

        Items get `"auto_confirmed": true` so the dashboard and logs can show
        they were approved by a rule rather than by hand.

        Returns:
            Number of items newly auto-confirmed.
        """
        if not self.active:
            return 0

        count = 0
        for item in report.get("unsubscribe", []):
            if not item.get("confirmed") and self.matches("unsubscribe", item.get("domain")):
                item["confirmed"] = True
                item["auto_confirmed"] = True
                count += 1

        for item in report.get("delete_groups", []):
            if item.get("group_type") != "domain":
                continue
            if not item.get("confirmed") and self.matches("delete", item.get("domain")):
                item["confirmed"] = True
                item["auto_confirmed"] = True
                count += 1

        if count:
            logger.info("Trusted rules auto-confirmed %d action(s)", count)
        return count


def parse_rules(raw_rules: list) -> list[TrustedRule]:
    """
    Parse the trusted_rules config list, skipping (and logging) invalid entries.
    """
    rules: list[TrustedRule] = []
    for raw in raw_rules:
        if isinstance(raw, str):
            entries = [{"action": a, "domain": raw} for a in VALID_ACTIONS]
        elif isinstance(raw, dict):
            entries = [raw]
        else:
            logger.warning("Ignoring trusted rule %r: expected a string or mapping", raw)
            continue

        for entry in entries:
            action = str(entry.get("action", "")).strip().lower()
            domain = str(entry.get("domain", "")).strip().lower()
            if action not in VALID_ACTIONS:
                logger.warning("Ignoring trusted rule %r: action must be one of %s", raw, VALID_ACTIONS)
                continue
            if not domain or domain in ("*", "*."):
                logger.warning("Ignoring trusted rule %r: a specific domain is required", raw)
                continue
            rules.append(TrustedRule(action=action, domain=domain))
    return rules
