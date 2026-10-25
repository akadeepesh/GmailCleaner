"""
Tests for engines/rules.py (trusted rules engine)

Covers:
  - Rules are inert while confirm_first is true (the default)
  - Exact-domain and wildcard-subdomain matching
  - String shorthand expands to both actions
  - Invalid rules (bad action, missing domain, bare "*") are ignored
  - blocked_senders always override trusted rules
  - apply(): auto-confirms matching items only, flags auto_confirmed,
    never touches subject groups or already-confirmed items
"""

from engines.rules import TrustedRule, TrustedRules, parse_rules


def _cfg(rules, confirm_first=False, blocked=None) -> dict:
    return {"trusted_rules": rules, "confirm_first": confirm_first, "blocked_senders": blocked or []}


def _report() -> dict:
    return {
        "unsubscribe": [
            {"domain": "news.example.com", "confirmed": False},
            {"domain": "friend.org", "confirmed": False},
        ],
        "delete_groups": [
            {"group_id": "domain:deals.shop.com", "group_type": "domain",
             "domain": "deals.shop.com", "confirmed": False},
            {"group_id": "subject:weekly digest", "group_type": "subject",
             "domain": None, "confirmed": False},
        ],
    }


class TestMatching:
    def test_exact_domain(self):
        rule = TrustedRule("unsubscribe", "news.example.com")
        assert rule.matches("unsubscribe", "news.example.com")
        assert rule.matches("unsubscribe", "NEWS.example.com")
        assert not rule.matches("unsubscribe", "other.example.com")
        assert not rule.matches("delete", "news.example.com")

    def test_wildcard_matches_apex_and_subdomains(self):
        rule = TrustedRule("delete", "*.shop.com")
        assert rule.matches("delete", "shop.com")
        assert rule.matches("delete", "deals.shop.com")
        assert not rule.matches("delete", "evilshop.com")

    def test_none_domain_never_matches(self):
        assert not TrustedRule("delete", "a.com").matches("delete", None)


class TestParsing:
    def test_shorthand_string_expands_to_both_actions(self):
        rules = parse_rules(["promo.example.org"])
        assert {r.action for r in rules} == {"unsubscribe", "delete"}

    def test_invalid_rules_ignored(self):
        rules = parse_rules([
            {"action": "nuke", "domain": "a.com"},
            {"action": "delete"},
            {"action": "delete", "domain": "*"},
            42,
            {"action": "Delete", "domain": " B.com "},
        ])
        assert rules == [TrustedRule("delete", "b.com")]


class TestActivation:
    def test_inactive_when_confirm_first_true(self):
        rules = TrustedRules.from_config(_cfg(["news.example.com"], confirm_first=True))
        assert not rules.active
        report = _report()
        assert rules.apply(report) == 0
        assert not report["unsubscribe"][0]["confirmed"]

    def test_inactive_without_rules(self):
        assert not TrustedRules.from_config(_cfg([])).active

    def test_missing_confirm_first_defaults_to_safe(self):
        rules = TrustedRules.from_config({"trusted_rules": ["a.com"]})
        assert not rules.active

    def test_blocked_sender_wins(self):
        rules = TrustedRules.from_config(_cfg(["news.example.com"], blocked=["news.example.com"]))
        assert not rules.matches("unsubscribe", "news.example.com")


class TestApply:
    def test_auto_confirms_matching_items(self):
        rules = TrustedRules.from_config(_cfg([
            {"action": "unsubscribe", "domain": "news.example.com"},
            {"action": "delete", "domain": "*.shop.com"},
        ]))
        report = _report()
        assert rules.apply(report) == 2

        unsub = {i["domain"]: i for i in report["unsubscribe"]}
        assert unsub["news.example.com"]["confirmed"] is True
        assert unsub["news.example.com"]["auto_confirmed"] is True
        assert unsub["friend.org"]["confirmed"] is False

        groups = {g["group_id"]: g for g in report["delete_groups"]}
        assert groups["domain:deals.shop.com"]["confirmed"] is True
        assert groups["subject:weekly digest"]["confirmed"] is False

    def test_already_confirmed_not_counted_or_flagged(self):
        rules = TrustedRules.from_config(_cfg(["news.example.com"]))
        report = _report()
        report["unsubscribe"][0]["confirmed"] = True
        assert rules.apply(report) == 0
        assert "auto_confirmed" not in report["unsubscribe"][0]
