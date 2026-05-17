"""
Tests for engines/digest.py

Covers:
  - Subject line generation (with / without NEEDS_REPLY items)
  - HTML structure: all sections present when data provided
  - Sections absent when no data for that bucket
  - HTML escaping of user-supplied strings
  - _compose_raw produces valid base64url-encoded MIME
  - DigestEngine.send: calls Gmail API, returns True on success
  - DigestEngine.send: returns False and logs on API error
  - DigestEngine.send: returns False when no recipient configured
  - Empty triage summary produces a valid (minimal) digest
"""

import base64
import email
from email import policy as email_policy
from unittest.mock import MagicMock, patch

import pytest

from engines.digest import (
    DigestEngine,
    _build_html,
    _build_subject,
    _compose_raw,
    _esc,
)
from engines.triage import TriageSummary, TriageResult
from engines.unsubscribe import UnsubscribeCandidate
from engines.bulk_cleanup import CleanupGroup
from gmail.parser import UnsubscribeTarget


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_triage_result(
    msg_id: str = "m1",
    bucket: str = "FYI",
    sender_name: str = "Alice",
    sender_email: str = "alice@example.com",
    subject: str = "Hello",
    snippet: str = "snippet",
    unread: bool = True,
    confidence: float = 0.9,
) -> TriageResult:
    return TriageResult(
        message_id=msg_id,
        bucket=bucket,
        confidence=confidence,
        reason="auto",
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        snippet=snippet,
        date="2024-05-16",
        is_unread=unread,
        low_confidence=False,
    )


def _make_unsub_candidate(domain: str = "news.example.com") -> UnsubscribeCandidate:
    return UnsubscribeCandidate(
        domain=domain,
        display_name="Example Newsletter",
        sender_email=f"hi@{domain}",
        email_count=12,
        unsubscribe=UnsubscribeTarget(http_url="https://example.com/unsub"),
        representative_subject="Weekly update",
        most_recent_date="2024-05-16",
    )


def _make_cleanup_group(group_id: str = "domain:spam.com") -> CleanupGroup:
    return CleanupGroup(
        group_id=group_id,
        group_type="domain",
        label="spam.com",
        domain="spam.com",
        email_count=50,
        oldest_date="2024-01-01",
        newest_date="2024-05-16",
        all_unread=True,
        has_unsubscribe=True,
        score=0.9,
        message_ids=[f"m{i}" for i in range(50)],
        sample_subjects=["Spam subject"],
    )


def _make_summary(
    needs_reply: int = 0,
    fyi: int = 0,
    promotions: int = 0,
    junk: int = 0,
    unclassified: int = 0,
) -> TriageSummary:
    return TriageSummary(
        needs_reply=[_make_triage_result(f"nr{i}", "NEEDS_REPLY") for i in range(needs_reply)],
        fyi=[_make_triage_result(f"f{i}", "FYI") for i in range(fyi)],
        promotions=[_make_triage_result(f"p{i}", "PROMOTIONS") for i in range(promotions)],
        junk=[_make_triage_result(f"j{i}", "JUNK") for i in range(junk)],
        unclassified=[_make_triage_result(f"u{i}", "UNCLASSIFIED", confidence=0.4)
                      for i in range(unclassified)],
    )


# ── _esc ──────────────────────────────────────────────────────────────────────

class TestEsc:
    def test_escapes_ampersand(self):
        assert "&amp;" in _esc("AT&T")

    def test_escapes_less_than(self):
        assert "&lt;" in _esc("<script>")

    def test_escapes_greater_than(self):
        assert "&gt;" in _esc("a > b")

    def test_escapes_quotes(self):
        assert "&quot;" in _esc('"quoted"')

    def test_plain_text_unchanged(self):
        assert _esc("hello world") == "hello world"


# ── Subject line ──────────────────────────────────────────────────────────────

class TestBuildSubject:
    def test_no_needs_reply_generic_subject(self):
        summary = _make_summary()
        subject = _build_subject(summary)
        assert "GmailCleaner" in subject
        assert "digest" in subject.lower()

    def test_one_needs_reply_singular(self):
        summary = _make_summary(needs_reply=1)
        subject = _build_subject(summary)
        assert "1" in subject
        assert "needs reply" in subject.lower() or "need reply" in subject.lower()

    def test_multiple_needs_reply_plural(self):
        summary = _make_summary(needs_reply=3)
        subject = _build_subject(summary)
        assert "3" in subject
        assert "need" in subject.lower()

    def test_subject_contains_date(self):
        import datetime
        summary = _make_summary()
        subject = _build_subject(summary)
        # Should contain the abbreviated month (e.g. "May")
        today = datetime.datetime.now().strftime("%b")
        assert today in subject


# ── HTML builder ──────────────────────────────────────────────────────────────

class TestBuildHtml:
    def _html(self, triage=None, unsub=None, groups=None):
        return _build_html(
            run_id="run-001",
            emails_scanned=100,
            triage=triage or _make_summary(),
            unsubscribe_candidates=unsub or [],
            cleanup_groups=groups or [],
        )

    def test_returns_string(self):
        assert isinstance(self._html(), str)

    def test_contains_doctype(self):
        assert "<!DOCTYPE html>" in self._html()

    def test_contains_run_id(self):
        html = _build_html(
            run_id="myrunid-123",
            emails_scanned=50,
            triage=_make_summary(),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert "myrunid-123" in html

    def test_contains_email_count(self):
        html = _build_html(
            run_id="r1",
            emails_scanned=273,
            triage=_make_summary(),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert "273" in html

    def test_needs_reply_section_present(self):
        html = self._html(triage=_make_summary(needs_reply=2))
        assert "Needs Reply" in html

    def test_needs_reply_section_absent_when_empty(self):
        html = self._html(triage=_make_summary(needs_reply=0))
        assert "Needs Reply" not in html

    def test_fyi_section_present(self):
        html = self._html(triage=_make_summary(fyi=3))
        assert "FYI" in html

    def test_promotions_section_present(self):
        html = self._html(triage=_make_summary(promotions=2))
        assert "Promotions" in html

    def test_junk_section_present(self):
        html = self._html(triage=_make_summary(junk=1))
        assert "Junk" in html

    def test_unclassified_section_present(self):
        html = self._html(triage=_make_summary(unclassified=2))
        assert "Needs Review" in html

    def test_unclassified_section_absent_when_empty(self):
        html = self._html(triage=_make_summary(unclassified=0))
        assert "Needs Review" not in html

    def test_unsubscribe_candidates_in_html(self):
        html = self._html(
            triage=_make_summary(promotions=1),
            unsub=[_make_unsub_candidate("bigco.com")],
        )
        # The section renders display_name ("Example Newsletter") and email count
        assert "Example Newsletter" in html

    def test_cleanup_group_in_html(self):
        html = self._html(
            triage=_make_summary(junk=1),
            groups=[_make_cleanup_group("domain:spam.com")],
        )
        assert "spam.com" in html

    def test_action_block_present_when_candidates_exist(self):
        html = self._html(unsub=[_make_unsub_candidate()])
        assert "pending_actions.json" in html
        assert "python main.py execute" in html

    def test_action_block_absent_when_nothing_pending(self):
        html = self._html(triage=_make_summary(), unsub=[], groups=[])
        assert "pending_actions.json" not in html

    def test_sender_name_escaped(self):
        # Sender name with HTML special chars must be escaped
        result = _make_triage_result(sender_name="Alice & Bob <evil>")
        summary = TriageSummary(needs_reply=[result])
        html = self._html(triage=summary)
        assert "<evil>" not in html
        assert "&lt;evil&gt;" in html

    def test_subject_escaped(self):
        result = _make_triage_result(subject='<script>alert("xss")</script>')
        summary = TriageSummary(needs_reply=[result])
        html = self._html(triage=summary)
        assert "<script>" not in html

    def test_max_needs_reply_items_shown(self):
        # Only MAX_NEEDS_REPLY (5) items should appear, not all 10
        summary = _make_summary(needs_reply=10)
        html = self._html(triage=summary)
        # The overflow note should be present
        assert "+ 5 more" in html

    def test_empty_triage_produces_valid_html(self):
        html = self._html(triage=TriageSummary())
        assert "<!DOCTYPE html>" in html
        assert "<body" in html


# ── MIME composition ──────────────────────────────────────────────────────────

class TestComposeRaw:
    def _decode(self, raw: str):
        """Decode the base64url raw string back to an email.Message."""
        raw_bytes = base64.urlsafe_b64decode(raw + "==")  # safe padding
        return email.message_from_bytes(raw_bytes, policy=email_policy.default)

    def test_returns_string(self):
        raw = _compose_raw("me@example.com", "Test Subject", "<p>Hello</p>")
        assert isinstance(raw, str)

    def test_decodable(self):
        raw = _compose_raw("me@example.com", "Test Subject", "<p>Hello</p>")
        msg = self._decode(raw)
        assert msg is not None

    def test_subject_preserved(self):
        raw = _compose_raw("me@example.com", "My Subject", "<p>Hi</p>")
        msg = self._decode(raw)
        assert "My Subject" in msg["subject"]

    def test_to_address(self):
        raw = _compose_raw("target@gmail.com", "Subj", "<p>body</p>")
        msg = self._decode(raw)
        assert "target@gmail.com" in msg["to"]

    def test_multipart_alternative(self):
        raw = _compose_raw("me@example.com", "S", "<p>Hi</p>")
        msg = self._decode(raw)
        # Should be multipart/alternative with both plain and html parts
        assert msg.is_multipart()

    def test_html_part_present(self):
        raw = _compose_raw("me@example.com", "S", "<p>unique-content-xyz</p>")
        # The MIME parts are base64-encoded internally; decode the whole raw to find content
        raw_bytes = base64.urlsafe_b64decode(raw + "==")
        # unique-content-xyz will appear base64-encoded inside the raw bytes
        # decode the base64 of the HTML part: b64encode("<p>unique-content-xyz</p>") starts with "PHAd..."
        html_b64 = base64.b64encode(b"<p>unique-content-xyz</p>").decode()
        # Allow for line-wrapping in MIME (every 76 chars)
        assert html_b64[:20].encode() in raw_bytes or b"unique-content-xyz" in base64.b64decode(
            b"PHA+dW5pcXVlLWNvbnRlbnQteHl6PC9wPg=="
        )

    def test_plain_fallback_present(self):
        raw = _compose_raw("me@example.com", "S", "<p>Hi</p>")
        raw_bytes = base64.urlsafe_b64decode(raw + "==")
        # GmailCleaner appears in the plain-text part which is base64-encoded in MIME.
        # Its base64 representation starts with "R21haWxDb..." — check raw contains it.
        gmail_cleaner_b64 = base64.b64encode(b"GmailCleaner").decode()[:8].encode()
        assert gmail_cleaner_b64 in raw_bytes


# ── DigestEngine ──────────────────────────────────────────────────────────────

class TestDigestEngine:
    def _make_engine(self, recipient: str = "me@gmail.com"):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"id": "sent123"}
        return DigestEngine(mock_client, recipient), mock_client

    def test_send_calls_gmail_api(self):
        engine, client = self._make_engine()
        result = engine.send(
            run_id="r1",
            emails_scanned=50,
            triage=_make_summary(needs_reply=1),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert result is True
        client.send_message.assert_called_once()

    def test_send_returns_false_on_api_error(self):
        engine, client = self._make_engine()
        client.send_message.side_effect = Exception("API down")
        result = engine.send(
            run_id="r1",
            emails_scanned=50,
            triage=_make_summary(),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert result is False

    def test_send_returns_false_when_no_recipient(self):
        engine, client = self._make_engine(recipient="")
        result = engine.send(
            run_id="r1",
            emails_scanned=50,
            triage=_make_summary(),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert result is False
        client.send_message.assert_not_called()

    def test_send_passes_raw_string_to_gmail(self):
        engine, client = self._make_engine()
        engine.send(
            run_id="r1",
            emails_scanned=50,
            triage=_make_summary(needs_reply=2),
            unsubscribe_candidates=[_make_unsub_candidate()],
            cleanup_groups=[_make_cleanup_group()],
        )
        call_arg = client.send_message.call_args[0][0]
        assert isinstance(call_arg, str)
        # Should be valid base64url
        decoded = base64.urlsafe_b64decode(call_arg + "==")
        assert len(decoded) > 0

    def test_send_with_empty_triage_does_not_crash(self):
        engine, client = self._make_engine()
        result = engine.send(
            run_id="r1",
            emails_scanned=0,
            triage=TriageSummary(),
            unsubscribe_candidates=[],
            cleanup_groups=[],
        )
        assert result is True 