"""
Tests for engines/unsubscribe.py

Covers:
  - Basic candidate detection
  - Deduplication by domain
  - Filtering of already-unsubscribed domains
  - Filtering of blocked domains
  - Minimum email count threshold
  - Ranking by email count
  - Display name selection
  - Best target selection (one-click > HTTP > mailto)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from engines.unsubscribe import UnsubscribeEngine, _best_display_name, _pick_best_target
from gmail.parser import GmailMessage, UnsubscribeTarget


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_message(
    msg_id: str = "msg1",
    domain: str = "newsletter.example.com",
    sender_email: str = "news@newsletter.example.com",
    sender_name: str = "Example Newsletter",
    subject: str = "Weekly roundup",
    unsubscribe_http: str | None = "https://example.com/unsub",
    unsubscribe_mailto: str | None = None,
    supports_one_click: bool = False,
    date: datetime | None = None,
) -> GmailMessage:
    unsub = None
    if unsubscribe_http or unsubscribe_mailto:
        unsub = UnsubscribeTarget(
            http_url=unsubscribe_http,
            mailto=unsubscribe_mailto,
            supports_one_click=supports_one_click,
        )
    return GmailMessage(
        id=msg_id,
        thread_id=f"thread_{msg_id}",
        sender_raw=f"{sender_name} <{sender_email}>",
        sender_name=sender_name,
        sender_email=sender_email,
        sender_domain=domain,
        subject=subject,
        date=date or datetime(2024, 5, 16, tzinfo=timezone.utc),
        labels=["INBOX"],
        snippet="...",
        unsubscribe=unsub,
    )


def _make_db(
    unsubscribed_domains: set[str] | None = None,
    blocked_domains: set[str] | None = None,
) -> MagicMock:
    db = MagicMock()
    db.get_unsubscribed_domains.return_value = unsubscribed_domains or set()
    db.get_blocked_domains.return_value = blocked_domains or set()
    return db


# ── Detection ─────────────────────────────────────────────────────────────────

class TestUnsubscribeEngine:
    def test_detects_single_candidate(self):
        messages = [_make_message("m1"), _make_message("m2")]
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert len(candidates) == 1
        assert candidates[0].domain == "newsletter.example.com"

    def test_deduplicates_by_domain(self):
        # 5 messages from the same domain → 1 candidate
        messages = [_make_message(f"m{i}") for i in range(5)]
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert len(candidates) == 1
        assert candidates[0].email_count == 5

    def test_multiple_domains(self):
        messages = [
            _make_message("m1", domain="a.com", sender_email="x@a.com"),
            _make_message("m2", domain="a.com", sender_email="x@a.com"),
            _make_message("m3", domain="b.com", sender_email="y@b.com"),
            _make_message("m4", domain="b.com", sender_email="y@b.com"),
        ]
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert len(candidates) == 2
        domains = {c.domain for c in candidates}
        assert domains == {"a.com", "b.com"}

    def test_ranked_by_email_count_descending(self):
        # Domain A: 3 emails, Domain B: 6 emails → B should be first
        messages = (
            [_make_message(f"a{i}", domain="a.com", sender_email="x@a.com") for i in range(3)]
            + [_make_message(f"b{i}", domain="b.com", sender_email="y@b.com") for i in range(6)]
        )
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert candidates[0].domain == "b.com"
        assert candidates[1].domain == "a.com"

    def test_min_email_count_filters_small_domains(self):
        messages = [_make_message("m1")]  # Only 1 email
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=2)
        candidates = engine.detect(messages)
        assert candidates == []

    def test_min_email_count_exact_threshold_passes(self):
        messages = [_make_message("m1"), _make_message("m2")]  # Exactly 2
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=2)
        candidates = engine.detect(messages)
        assert len(candidates) == 1

    def test_messages_without_unsubscribe_header_excluded(self):
        messages = [
            _make_message("m1", unsubscribe_http=None, unsubscribe_mailto=None),
            _make_message("m2", unsubscribe_http=None, unsubscribe_mailto=None),
        ]
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert candidates == []

    def test_already_unsubscribed_domains_marked(self):
        messages = [_make_message("m1"), _make_message("m2")]
        db = _make_db(unsubscribed_domains={"newsletter.example.com"})
        engine = UnsubscribeEngine(db=db, min_email_count=1)
        candidates = engine.detect(messages)
        assert len(candidates) == 1
        assert candidates[0].already_unsubscribed is True

    def test_get_new_candidates_excludes_already_unsubscribed(self):
        messages = [_make_message("m1"), _make_message("m2")]
        db = _make_db(unsubscribed_domains={"newsletter.example.com"})
        engine = UnsubscribeEngine(db=db, min_email_count=1)
        new = engine.get_new_candidates(messages)
        assert new == []

    def test_db_blocked_domains_skipped(self):
        messages = [_make_message("m1"), _make_message("m2")]
        db = _make_db(blocked_domains={"newsletter.example.com"})
        engine = UnsubscribeEngine(db=db, min_email_count=1)
        candidates = engine.detect(messages)
        assert candidates == []

    def test_config_blocked_senders_skipped(self):
        messages = [_make_message("m1"), _make_message("m2")]
        engine = UnsubscribeEngine(
            db=_make_db(),
            min_email_count=1,
            blocked_senders=["newsletter.example.com"],
        )
        candidates = engine.detect(messages)
        assert candidates == []

    def test_sample_message_ids_limited_to_five(self):
        messages = [_make_message(f"m{i}") for i in range(10)]
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect(messages)
        assert len(candidates[0].sample_message_ids) <= 5

    def test_empty_messages_list(self):
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect([])
        assert candidates == []

    def test_messages_with_empty_domain_skipped(self):
        msg = _make_message("m1", domain="", sender_email="noemail")
        engine = UnsubscribeEngine(db=_make_db(), min_email_count=1)
        candidates = engine.detect([msg])
        assert candidates == []


# ── Helper functions ──────────────────────────────────────────────────────────

class TestPickBestTarget:
    def test_prefers_one_click(self):
        one_click = UnsubscribeTarget(http_url="https://a.com", supports_one_click=True)
        http = UnsubscribeTarget(http_url="https://b.com")
        msgs = [
            _make_message("m1"),
            _make_message("m2"),
        ]
        msgs[0].unsubscribe = http
        msgs[1].unsubscribe = one_click
        result = _pick_best_target(msgs)
        assert result == one_click

    def test_prefers_http_over_mailto(self):
        http = UnsubscribeTarget(http_url="https://a.com")
        mailto = UnsubscribeTarget(mailto="unsub@example.com")
        msgs = [_make_message("m1"), _make_message("m2")]
        msgs[0].unsubscribe = mailto
        msgs[1].unsubscribe = http
        result = _pick_best_target(msgs)
        assert result == http

    def test_falls_back_to_mailto(self):
        mailto = UnsubscribeTarget(mailto="unsub@example.com")
        msgs = [_make_message("m1")]
        msgs[0].unsubscribe = mailto
        result = _pick_best_target(msgs)
        assert result == mailto


class TestBestDisplayName:
    def test_most_common_name(self):
        msgs = [
            _make_message("m1", sender_name="Example Weekly"),
            _make_message("m2", sender_name="Example Weekly"),
            _make_message("m3", sender_name="Different Name"),
        ]
        assert _best_display_name(msgs) == "Example Weekly"

    def test_falls_back_to_email_when_no_names(self):
        msgs = [_make_message("m1", sender_name="", sender_email="x@example.com")]
        result = _best_display_name(msgs)
        assert result == "x@example.com"

    def test_empty_list_returns_empty(self):
        assert _best_display_name([]) == ""