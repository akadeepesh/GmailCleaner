"""
Tests for engines/bulk_cleanup.py

Covers:
  - Domain group detection and minimum size filtering
  - Subject prefix normalization and clustering
  - Scoring logic (volume, unread, unsubscribe header, group type)
  - Blocked domain filtering
  - claimed_ids: subject groups don't double-count domain-grouped messages
  - Edge cases: empty input, single-message domains, all-read messages
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from engines.bulk_cleanup import (
    BulkCleanupEngine,
    CleanupGroup,
    _normalize_subject,
    _score_group,
)
from gmail.parser import GmailMessage, UnsubscribeTarget


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_msg(
    msg_id: str = "m1",
    domain: str = "news.example.com",
    sender_email: str = "hi@news.example.com",
    subject: str = "Weekly digest",
    unread: bool = True,
    has_unsubscribe: bool = False,
    date: datetime | None = None,
) -> GmailMessage:
    unsub = UnsubscribeTarget(http_url="https://example.com/unsub") if has_unsubscribe else None
    return GmailMessage(
        id=msg_id,
        thread_id=f"t_{msg_id}",
        sender_raw=f"Sender <{sender_email}>",
        sender_name="Sender",
        sender_email=sender_email,
        sender_domain=domain,
        subject=subject,
        date=date or datetime(2024, 5, 10, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"] if unread else ["INBOX"],
        snippet="...",
        unsubscribe=unsub,
    )


def _make_db(blocked_domains: set[str] | None = None) -> MagicMock:
    db = MagicMock()
    db.get_blocked_domains.return_value = blocked_domains or set()
    return db


def _make_engine(min_group_size: int = 2, blocked: list[str] | None = None) -> BulkCleanupEngine:
    return BulkCleanupEngine(db=_make_db(), min_group_size=min_group_size,
                             blocked_senders=blocked)


# ── Domain groups ─────────────────────────────────────────────────────────────

class TestDomainGroups:
    def test_basic_domain_group(self):
        msgs = [_make_msg(f"m{i}") for i in range(5)]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert len(groups) == 1
        assert groups[0].group_type == "domain"
        assert groups[0].domain == "news.example.com"
        assert groups[0].email_count == 5

    def test_below_min_size_excluded(self):
        msgs = [_make_msg("m1")]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups == []

    def test_exact_min_size_included(self):
        msgs = [_make_msg("m1"), _make_msg("m2")]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert len(groups) == 1

    def test_multiple_domains_produce_separate_groups(self):
        msgs = (
            [_make_msg(f"a{i}", domain="a.com", sender_email=f"x{i}@a.com") for i in range(3)]
            + [_make_msg(f"b{i}", domain="b.com", sender_email=f"y{i}@b.com") for i in range(3)]
        )
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        domains = {g.domain for g in groups}
        assert domains == {"a.com", "b.com"}

    def test_blocked_domain_skipped(self):
        msgs = [_make_msg(f"m{i}") for i in range(5)]
        engine = _make_engine(blocked=["news.example.com"])
        groups = engine.detect(msgs)
        assert groups == []

    def test_db_blocked_domain_skipped(self):
        msgs = [_make_msg(f"m{i}") for i in range(5)]
        engine = BulkCleanupEngine(db=_make_db(blocked_domains={"news.example.com"}), min_group_size=2)
        groups = engine.detect(msgs)
        assert groups == []

    def test_all_message_ids_captured(self):
        msgs = [_make_msg(f"m{i}") for i in range(6)]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert set(groups[0].message_ids) == {f"m{i}" for i in range(6)}

    def test_sample_subjects_limited_to_three(self):
        msgs = [_make_msg(f"m{i}", subject=f"Subject {i}") for i in range(10)]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert len(groups[0].sample_subjects) <= 3

    def test_all_unread_flag(self):
        msgs = [_make_msg(f"m{i}", unread=True) for i in range(3)]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups[0].all_unread is True

    def test_mixed_read_unread_flag(self):
        msgs = [
            _make_msg("m1", unread=True),
            _make_msg("m2", unread=False),
            _make_msg("m3", unread=True),
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups[0].all_unread is False

    def test_has_unsubscribe_flag(self):
        msgs = [
            _make_msg("m1", has_unsubscribe=True),
            _make_msg("m2", has_unsubscribe=False),
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups[0].has_unsubscribe is True

    def test_oldest_newest_dates(self):
        msgs = [
            _make_msg("m1", date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            _make_msg("m2", date=datetime(2024, 5, 16, tzinfo=timezone.utc)),
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups[0].oldest_date == "2024-01-01"
        assert groups[0].newest_date == "2024-05-16"

    def test_empty_messages_returns_empty(self):
        engine = _make_engine()
        assert engine.detect([]) == []


# ── Subject groups ────────────────────────────────────────────────────────────

class TestSubjectGroups:
    def test_same_subject_prefix_clusters(self):
        # 3 messages from different domains but same subject root.
        msgs = [
            _make_msg("m1", domain="a.com", sender_email="x@a.com",
                      subject="Weekly Digest #1"),
            _make_msg("m2", domain="b.com", sender_email="x@b.com",
                      subject="Weekly Digest #2"),
            _make_msg("m3", domain="c.com", sender_email="x@c.com",
                      subject="Weekly Digest #3"),
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        subject_groups = [g for g in groups if g.group_type == "subject"]
        assert len(subject_groups) == 1
        assert subject_groups[0].email_count == 3

    def test_domain_group_messages_not_double_counted(self):
        # 5 messages from same domain (form a domain group).
        # They should NOT also appear in a subject-prefix group.
        msgs = [_make_msg(f"m{i}", subject="Weekly digest") for i in range(5)]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        # Should only have 1 domain group, no subject group.
        assert len(groups) == 1
        assert groups[0].group_type == "domain"

    def test_subject_group_below_min_excluded(self):
        msgs = [
            _make_msg("m1", domain="a.com", sender_email="x@a.com", subject="Newsletter #1"),
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs)
        assert groups == []


# ── Subject normalization ─────────────────────────────────────────────────────

class TestNormalizeSubject:
    def test_strips_issue_number(self):
        assert _normalize_subject("Weekly Digest #42") == "Weekly Digest"

    def test_strips_re_prefix(self):
        assert _normalize_subject("Re: Your weekly update") == "Your weekly update"

    def test_strips_fwd_prefix(self):
        assert _normalize_subject("Fwd: Newsletter") == "Newsletter"

    def test_strips_date(self):
        result = _normalize_subject("Digest — May 2024")
        assert "2024" not in result

    def test_too_short_returns_empty(self):
        assert _normalize_subject("Hi") == ""

    def test_empty_returns_empty(self):
        assert _normalize_subject("") == ""

    def test_strips_issue_word(self):
        result = _normalize_subject("Your Digest Issue 7 is here")
        assert "Issue 7" not in result
        assert "Your Digest" in result

    def test_no_modification_needed(self):
        result = _normalize_subject("Plain Subject Line")
        assert result == "Plain Subject Line"


# ── Scoring ───────────────────────────────────────────────────────────────────

class TestScoring:
    def _make_group(
        self,
        group_type: str = "domain",
        email_count: int = 10,
        all_unread: bool = False,
        has_unsubscribe: bool = False,
    ) -> CleanupGroup:
        return CleanupGroup(
            group_id="test",
            group_type=group_type,
            label="test",
            domain="test.com",
            email_count=email_count,
            oldest_date="2024-01-01",
            newest_date="2024-05-16",
            all_unread=all_unread,
            has_unsubscribe=has_unsubscribe,
            score=0.0,
        )

    def test_score_in_valid_range(self):
        group = self._make_group()
        score = _score_group(group)
        assert 0.0 <= score <= 1.0

    def test_higher_email_count_increases_score(self):
        low = _score_group(self._make_group(email_count=5))
        high = _score_group(self._make_group(email_count=50))
        assert high > low

    def test_all_unread_increases_score(self):
        read = _score_group(self._make_group(all_unread=False))
        unread = _score_group(self._make_group(all_unread=True))
        assert unread > read

    def test_has_unsubscribe_increases_score(self):
        no_unsub = _score_group(self._make_group(has_unsubscribe=False))
        unsub = _score_group(self._make_group(has_unsubscribe=True))
        assert unsub > no_unsub

    def test_domain_group_scores_higher_than_subject(self):
        domain = _score_group(self._make_group(group_type="domain"))
        subject = _score_group(self._make_group(group_type="subject"))
        assert domain > subject

    def test_max_score_conditions(self):
        group = self._make_group(
            email_count=100, all_unread=True, has_unsubscribe=True, group_type="domain"
        )
        score = _score_group(group)
        assert score == pytest.approx(1.0)

    def test_min_score_conditions(self):
        group = self._make_group(
            email_count=0, all_unread=False, has_unsubscribe=False, group_type="subject"
        )
        score = _score_group(group)
        assert score == pytest.approx(0.05)  # 0.0*0.4 + 0.0*0.3 + 0.0*0.2 + 0.5*0.1

    def test_groups_sorted_by_score_descending(self):
        msgs_noisy = [
            _make_msg(f"a{i}", domain="noisy.com", sender_email=f"x{i}@noisy.com",
                      unread=True, has_unsubscribe=True)
            for i in range(20)
        ]
        msgs_quiet = [
            _make_msg(f"b{i}", domain="quiet.com", sender_email=f"y{i}@quiet.com",
                      unread=False, has_unsubscribe=False)
            for i in range(3)
        ]
        engine = _make_engine(min_group_size=2)
        groups = engine.detect(msgs_noisy + msgs_quiet)
        assert len(groups) >= 2
        assert groups[0].score >= groups[1].score

    def test_score_label_high(self):
        group = self._make_group(email_count=100, all_unread=True, has_unsubscribe=True)
        group.score = _score_group(group)
        assert group.score_label == "high"

    def test_score_label_low(self):
        group = self._make_group(email_count=1, all_unread=False, has_unsubscribe=False)
        group.score = _score_group(group)
        assert group.score_label == "low"