"""
Tests for engines/triage.py

Covers:
  - Basic classification and bucket assignment
  - Low-confidence enforcement (below threshold → UNCLASSIFIED)
  - Excluded IDs skipped (cleanup group messages)
  - DB cache hit: cached messages not re-sent to Gemini
  - DB cache miss: uncached messages sent to Gemini
  - Sort order: unread first, then by date
  - TriageSummary bucket counts and .total
  - Empty input → empty summary
  - All excluded → empty summary (no Gemini call)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engines.triage import TriageEngine, TriageSummary, TriageResult, _build_summary, _to_metadata
from ai.gemini import GeminiClassification, GeminiClient
from gmail.parser import GmailMessage


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_msg(
    msg_id: str = "m1",
    sender_email: str = "x@example.com",
    sender_name: str = "Example",
    subject: str = "Hello",
    snippet: str = "snippet text",
    unread: bool = True,
    date: datetime | None = None,
) -> GmailMessage:
    return GmailMessage(
        id=msg_id,
        thread_id=f"t_{msg_id}",
        sender_raw=f"{sender_name} <{sender_email}>",
        sender_name=sender_name,
        sender_email=sender_email,
        sender_domain=sender_email.split("@")[1],
        subject=subject,
        date=date or datetime(2024, 5, 16, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"] if unread else ["INBOX"],
        snippet=snippet,
        unsubscribe=None,
    )


def _make_classification(
    msg_id: str = "m1",
    bucket: str = "FYI",
    confidence: float = 0.9,
    reason: str = "ok",
) -> GeminiClassification:
    return GeminiClassification(
        message_id=msg_id,
        bucket=bucket,
        confidence=confidence,
        reason=reason,
    )


def _make_engine(
    classifications: list[GeminiClassification] | None = None,
    cached: list[dict] | None = None,
    threshold: float = 0.75,
) -> tuple[TriageEngine, MagicMock, MagicMock]:
    mock_gemini = MagicMock(spec=GeminiClient)
    mock_gemini.classify_messages.return_value = classifications or []

    mock_db = MagicMock()
    mock_db.get_recent_triage_results.return_value = cached or []

    engine = TriageEngine(
        gemini_client=mock_gemini,
        db=mock_db,
        confidence_threshold=threshold,
    )
    return engine, mock_gemini, mock_db


# ── Basic classification ──────────────────────────────────────────────────────

class TestTriageEngine:
    def test_classifies_message_into_fyi(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.9)])
        summary = engine.classify(msgs)
        assert len(summary.fyi) == 1
        assert summary.fyi[0].message_id == "m1"

    def test_classifies_message_into_needs_reply(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "NEEDS_REPLY", 0.95)])
        summary = engine.classify(msgs)
        assert len(summary.needs_reply) == 1

    def test_classifies_message_into_promotions(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "PROMOTIONS", 0.88)])
        summary = engine.classify(msgs)
        assert len(summary.promotions) == 1

    def test_classifies_message_into_junk(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "JUNK", 0.80)])
        summary = engine.classify(msgs)
        assert len(summary.junk) == 1

    def test_low_confidence_moved_to_unclassified(self):
        msgs = [_make_msg("m1")]
        # Confidence 0.5 < threshold 0.75 → UNCLASSIFIED
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.5)], threshold=0.75)
        summary = engine.classify(msgs)
        assert len(summary.unclassified) == 1
        assert len(summary.fyi) == 0

    def test_exactly_at_threshold_not_downgraded(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.75)], threshold=0.75)
        summary = engine.classify(msgs)
        # 0.75 is NOT < 0.75 → stays in FYI
        assert len(summary.fyi) == 1

    def test_excluded_ids_not_sent_to_gemini(self):
        msgs = [_make_msg("m1"), _make_msg("m2")]
        engine, gemini, db = _make_engine([_make_classification("m2", "FYI", 0.9)])
        summary = engine.classify(msgs, excluded_ids={"m1"})
        # Gemini called with only m2
        call_args = gemini.classify_messages.call_args[0][0]
        assert all(m["message_id"] != "m1" for m in call_args)

    def test_all_excluded_no_gemini_call(self):
        msgs = [_make_msg("m1"), _make_msg("m2")]
        engine, gemini, db = _make_engine()
        summary = engine.classify(msgs, excluded_ids={"m1", "m2"})
        gemini.classify_messages.assert_not_called()
        assert summary.total == 0

    def test_empty_messages_returns_empty_summary(self):
        engine, gemini, db = _make_engine()
        summary = engine.classify([])
        assert summary.total == 0
        gemini.classify_messages.assert_not_called()

    def test_gemini_results_saved_to_db(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.9)])
        engine.classify(msgs)
        db.save_triage_results.assert_called_once()
        saved = db.save_triage_results.call_args[0][0]
        assert any(r["message_id"] == "m1" for r in saved)

    def test_cached_messages_not_sent_to_gemini(self):
        msgs = [_make_msg("m1"), _make_msg("m2")]
        cached = [
            {"message_id": "m1", "bucket": "FYI", "confidence": 0.9,
             "reason": "cached", "model": "gemini-1.5-flash"}
        ]
        engine, gemini, db = _make_engine(
            classifications=[_make_classification("m2", "JUNK", 0.85)],
            cached=cached,
        )
        summary = engine.classify(msgs)
        # Only m2 sent to Gemini
        call_args = gemini.classify_messages.call_args[0][0]
        assert all(m["message_id"] != "m1" for m in call_args)
        # Both m1 (cached) and m2 (fresh) appear in summary
        all_ids = {r.message_id for bucket in [summary.fyi, summary.junk,
                   summary.needs_reply, summary.promotions, summary.unclassified]
                   for r in bucket}
        assert "m1" in all_ids
        assert "m2" in all_ids

    def test_triage_result_has_message_context(self):
        msgs = [_make_msg("m1", sender_name="Alice", subject="Meeting")]
        engine, gemini, db = _make_engine([_make_classification("m1", "NEEDS_REPLY", 0.95)])
        summary = engine.classify(msgs)
        result = summary.needs_reply[0]
        assert result.sender_name == "Alice"
        assert result.subject == "Meeting"
        assert result.is_unread is True

    def test_low_confidence_flag_set(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.5)], threshold=0.75)
        summary = engine.classify(msgs)
        assert summary.unclassified[0].low_confidence is True

    def test_high_confidence_flag_not_set(self):
        msgs = [_make_msg("m1")]
        engine, gemini, db = _make_engine([_make_classification("m1", "FYI", 0.95)], threshold=0.75)
        summary = engine.classify(msgs)
        assert summary.fyi[0].low_confidence is False


# ── TriageSummary ─────────────────────────────────────────────────────────────

class TestTriageSummary:
    def _make_result(self, msg_id: str, bucket: str, unread: bool = True,
                     date: str = "2024-05-16") -> TriageResult:
        return TriageResult(
            message_id=msg_id, bucket=bucket, confidence=0.9, reason="ok",
            sender_name="S", sender_email="s@x.com", subject="Sub",
            snippet="snip", date=date, is_unread=unread, low_confidence=False,
        )

    def test_total_counts_all_buckets(self):
        summary = TriageSummary(
            needs_reply=[self._make_result("m1", "NEEDS_REPLY")],
            fyi=[self._make_result("m2", "FYI"), self._make_result("m3", "FYI")],
            junk=[self._make_result("m4", "JUNK")],
        )
        assert summary.total == 4

    def test_unread_sorted_first(self):
        results = [
            self._make_result("m1", "FYI", unread=False, date="2024-05-16"),
            self._make_result("m2", "FYI", unread=True, date="2024-05-14"),
        ]
        summary = _build_summary(results)
        # m2 is unread so should come first even though older
        assert summary.fyi[0].message_id == "m2"

    def test_within_same_read_status_sorted_by_date_desc(self):
        results = [
            self._make_result("m1", "FYI", unread=False, date="2024-05-10"),
            self._make_result("m2", "FYI", unread=False, date="2024-05-16"),
        ]
        summary = _build_summary(results)
        # m2 is newer — should come first (date desc)
        assert summary.fyi[0].message_id == "m2"

    def test_empty_summary_total_zero(self):
        assert TriageSummary().total == 0


# ── _to_metadata ──────────────────────────────────────────────────────────────

class TestToMetadata:
    def test_includes_required_fields(self):
        msg = _make_msg("m1", sender_email="x@example.com", subject="Test", snippet="snip")
        meta = _to_metadata(msg)
        assert meta["message_id"] == "m1"
        assert meta["sender"] == "x@example.com"
        assert meta["subject"] == "Test"
        assert meta["snippet"] == "snip"
        assert "labels" in meta

    def test_no_body_field(self):
        msg = _make_msg()
        meta = _to_metadata(msg)
        assert "body" not in meta
        assert "content" not in meta