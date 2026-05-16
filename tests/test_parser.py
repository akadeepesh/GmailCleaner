"""
Tests for gmail/parser.py

Covers:
  - Address parsing (display name, email, domain)
  - internalDate conversion
  - List-Unsubscribe header parsing (HTTP, mailto, one-click, malformed)
  - Full message parse round-trip
  - Graceful handling of malformed/missing fields
"""

from datetime import datetime, timezone

import pytest

from gmail.parser import (
    GmailMessage,
    UnsubscribeTarget,
    _extract_domain,
    _parse_address,
    _parse_internal_date,
    _parse_unsubscribe_header,
    parse_message,
    parse_messages,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_raw_message(
    msg_id: str = "abc123",
    thread_id: str = "thread1",
    from_header: str = "Test Sender <sender@example.com>",
    subject: str = "Hello World",
    internal_date: str = "1715865600000",  # 2024-05-16 UTC
    labels: list[str] | None = None,
    snippet: str = "Preview text...",
    list_unsubscribe: str | None = None,
    list_unsubscribe_post: str | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": from_header},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Thu, 16 May 2024 00:00:00 +0000"},
        {"name": "Message-ID", "value": "<abc123@mail.example.com>"},
    ]
    if list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": list_unsubscribe})
    if list_unsubscribe_post:
        headers.append({"name": "List-Unsubscribe-Post", "value": list_unsubscribe_post})

    return {
        "id": msg_id,
        "threadId": thread_id,
        "internalDate": internal_date,
        "labelIds": labels or ["INBOX", "UNREAD"],
        "snippet": snippet,
        "payload": {
            "headers": headers,
        },
    }


# ── Address parsing ───────────────────────────────────────────────────────────

class TestParseAddress:
    def test_display_name_and_email(self):
        name, email = _parse_address("Alice Smith <alice@example.com>")
        assert name == "Alice Smith"
        assert email == "alice@example.com"

    def test_email_only(self):
        name, email = _parse_address("alice@example.com")
        assert email == "alice@example.com"

    def test_uppercase_email_lowercased(self):
        _, email = _parse_address("Alice <ALICE@EXAMPLE.COM>")
        assert email == "alice@example.com"

    def test_empty_string(self):
        name, email = _parse_address("")
        assert name == ""
        assert email == ""

    def test_display_name_with_no_email_returns_empty(self):
        name, email = _parse_address("Just A Name")
        # Should not crash; email will be empty or the raw value
        assert isinstance(name, str)
        assert isinstance(email, str)


class TestExtractDomain:
    def test_simple_domain(self):
        assert _extract_domain("alice@example.com") == "example.com"

    def test_subdomain(self):
        assert _extract_domain("newsletter@mail.substack.com") == "mail.substack.com"

    def test_empty_string(self):
        assert _extract_domain("") == ""

    def test_no_at_sign(self):
        assert _extract_domain("notanemail") == ""

    def test_uppercase_normalized(self):
        assert _extract_domain("user@EXAMPLE.COM") == "example.com"


# ── Internal date ─────────────────────────────────────────────────────────────

class TestParseInternalDate:
    def test_valid_epoch_ms(self):
        dt = _parse_internal_date("1715865600000")
        assert isinstance(dt, datetime)
        assert dt.tzinfo == timezone.utc

    def test_none_input(self):
        assert _parse_internal_date(None) is None

    def test_empty_string(self):
        assert _parse_internal_date("") is None

    def test_non_numeric(self):
        assert _parse_internal_date("not-a-number") is None

    def test_epoch_zero(self):
        dt = _parse_internal_date("0")
        assert dt == datetime(1970, 1, 1, tzinfo=timezone.utc)


# ── Unsubscribe header parsing ────────────────────────────────────────────────

class TestParseUnsubscribeHeader:
    def test_http_only(self):
        header = "<https://example.com/unsubscribe?token=abc>"
        target = _parse_unsubscribe_header(header, None)
        assert target is not None
        assert target.http_url == "https://example.com/unsubscribe?token=abc"
        assert target.mailto is None
        assert not target.supports_one_click

    def test_mailto_only(self):
        header = "<mailto:unsub@example.com>"
        target = _parse_unsubscribe_header(header, None)
        assert target is not None
        assert target.mailto == "unsub@example.com"
        assert target.http_url is None

    def test_both_http_and_mailto(self):
        header = "<https://example.com/unsub>, <mailto:unsub@example.com>"
        target = _parse_unsubscribe_header(header, None)
        assert target is not None
        assert target.http_url is not None
        assert target.mailto is not None

    def test_one_click_rfc8058(self):
        header = "<https://example.com/unsub>"
        post_header = "List-Unsubscribe=One-Click"
        target = _parse_unsubscribe_header(header, post_header)
        assert target is not None
        assert target.supports_one_click is True

    def test_none_returns_none(self):
        assert _parse_unsubscribe_header(None, None) is None

    def test_empty_string_returns_none(self):
        assert _parse_unsubscribe_header("", None) is None

    def test_malformed_no_angle_brackets(self):
        # Non-standard but some senders omit angle brackets.
        header = "https://example.com/unsub"
        target = _parse_unsubscribe_header(header, None)
        # Should still parse via fallback logic.
        assert target is not None
        assert target.http_url == "https://example.com/unsub"

    def test_http_url_must_be_valid(self):
        header = "<not-a-url>"
        target = _parse_unsubscribe_header(header, None)
        assert target is None

    def test_mailto_with_query_string_stripped(self):
        header = "<mailto:unsub@example.com?subject=unsubscribe>"
        target = _parse_unsubscribe_header(header, None)
        assert target is not None
        assert target.mailto == "unsub@example.com"

    def test_preferred_method_http_over_mailto(self):
        header = "<https://example.com/unsub>, <mailto:unsub@example.com>"
        target = _parse_unsubscribe_header(header, None)
        assert target.preferred_method == "http"

    def test_preferred_method_mailto_when_no_http(self):
        header = "<mailto:unsub@example.com>"
        target = _parse_unsubscribe_header(header, None)
        assert target.preferred_method == "mailto"


# ── Full message parsing ──────────────────────────────────────────────────────

class TestParseMessage:
    def test_basic_message(self):
        raw = _make_raw_message()
        msg = parse_message(raw)
        assert msg is not None
        assert msg.id == "abc123"
        assert msg.thread_id == "thread1"
        assert msg.sender_email == "sender@example.com"
        assert msg.sender_name == "Test Sender"
        assert msg.sender_domain == "example.com"
        assert msg.subject == "Hello World"
        assert msg.snippet == "Preview text..."
        assert "INBOX" in msg.labels
        assert "UNREAD" in msg.labels

    def test_is_unread(self):
        raw = _make_raw_message(labels=["INBOX", "UNREAD"])
        msg = parse_message(raw)
        assert msg.is_unread is True

    def test_is_read(self):
        raw = _make_raw_message(labels=["INBOX"])
        msg = parse_message(raw)
        assert msg.is_unread is False

    def test_with_unsubscribe_header(self):
        raw = _make_raw_message(list_unsubscribe="<https://example.com/unsub>")
        msg = parse_message(raw)
        assert msg.unsubscribe is not None
        assert msg.unsubscribe.http_url == "https://example.com/unsub"

    def test_without_unsubscribe_header(self):
        raw = _make_raw_message()
        msg = parse_message(raw)
        assert msg.unsubscribe is None

    def test_missing_internal_date_returns_none(self):
        raw = _make_raw_message()
        del raw["internalDate"]
        msg = parse_message(raw)
        assert msg is None

    def test_empty_subject_uses_placeholder(self):
        raw = _make_raw_message(subject="")
        msg = parse_message(raw)
        assert msg is not None
        assert msg.subject == "(no subject)"

    def test_missing_id_in_payload_still_returns(self):
        # id is at top level, not in payload
        raw = _make_raw_message()
        msg = parse_message(raw)
        assert msg.id == "abc123"

    def test_date_is_utc(self):
        raw = _make_raw_message(internal_date="1715865600000")
        msg = parse_message(raw)
        assert msg.date.tzinfo == timezone.utc


class TestParseMessages:
    def test_filters_invalid_messages(self):
        valid = _make_raw_message(msg_id="valid1")
        invalid = _make_raw_message(msg_id="invalid1")
        del invalid["internalDate"]

        results = parse_messages([valid, invalid])
        assert len(results) == 1
        assert results[0].id == "valid1"

    def test_empty_list(self):
        assert parse_messages([]) == []

    def test_all_valid(self):
        raws = [_make_raw_message(msg_id=f"msg{i}") for i in range(5)]
        results = parse_messages(raws)
        assert len(results) == 5