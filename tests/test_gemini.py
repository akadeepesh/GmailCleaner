"""
Tests for ai/gemini.py

Covers:
  - Prompt building (privacy: no body, correct fields)
  - Response parsing: valid JSON, markdown fences, partial arrays, invalid buckets
  - Confidence clamping (out-of-range values)
  - Unknown message_id in response handled gracefully
  - Missing message from response → UNCLASSIFIED
  - Retry on 429 (mock HTTP layer)
  - Non-retriable HTTP error → returns UNCLASSIFIED
  - Timeout → returns UNCLASSIFIED
  - Empty input → returns empty list
  - API key missing → raises ValueError
"""

import json
from unittest.mock import MagicMock, patch, call

import pytest
import httpx

from ai.gemini import (
    GeminiClient,
    GeminiClassification,
    _build_prompt,
    _parse_response,
    _make_unclassified,
    _extract_text,
    _safe,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "test-key") -> GeminiClient:
    return GeminiClient(api_key=api_key)


def _make_msg(
    msg_id: str = "m1",
    sender: str = "news@example.com",
    subject: str = "Weekly Digest",
    snippet: str = "Here is this week...",
    labels: str = "INBOX, UNREAD",
) -> dict[str, str]:
    return {
        "message_id": msg_id,
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "labels": labels,
    }


def _gemini_response(items: list[dict]) -> dict:
    """Build a fake Gemini API response wrapping a JSON array."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(items)}]
                }
            }
        ]
    }


# ── Initialisation ────────────────────────────────────────────────────────────

class TestGeminiClientInit:
    def test_raises_if_no_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            # Ensure GEMINI_API_KEY is not set
            import os
            os.environ.pop("GEMINI_API_KEY", None)
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                GeminiClient(api_key="")

    def test_accepts_api_key_arg(self):
        client = _make_client("my-key")
        assert client._api_key == "my-key"

    def test_context_manager(self):
        with _make_client() as client:
            assert client is not None


# ── _safe helper ──────────────────────────────────────────────────────────────

class TestSafe:
    def test_escapes_quotes(self):
        assert '\\"' in _safe('say "hello"')

    def test_escapes_backslash(self):
        assert '\\\\' in _safe("a\\b")

    def test_removes_newlines(self):
        assert "\n" not in _safe("line1\nline2")

    def test_plain_string_unchanged(self):
        assert _safe("hello world") == "hello world"


# ── Prompt builder ────────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_contains_message_id(self):
        msgs = [_make_msg("abc123")]
        prompt = _build_prompt(msgs)
        assert "abc123" in prompt

    def test_contains_sender(self):
        msgs = [_make_msg(sender="alerts@bank.com")]
        prompt = _build_prompt(msgs)
        assert "alerts@bank.com" in prompt

    def test_contains_subject(self):
        msgs = [_make_msg(subject="Your statement is ready")]
        prompt = _build_prompt(msgs)
        assert "Your statement is ready" in prompt

    def test_snippet_truncated_to_120(self):
        long_snippet = "x" * 200
        msgs = [_make_msg(snippet=long_snippet)]
        prompt = _build_prompt(msgs)
        # Snippet in prompt should be at most 120 chars of x's
        assert "x" * 121 not in prompt

    def test_no_body_content(self):
        # Body is never in the metadata dict — confirm prompt doesn't have a body field.
        msgs = [_make_msg()]
        prompt = _build_prompt(msgs)
        assert '"body"' not in prompt
        assert '"content"' not in prompt

    def test_multiple_messages_all_included(self):
        msgs = [_make_msg(f"m{i}") for i in range(5)]
        prompt = _build_prompt(msgs)
        for i in range(5):
            assert f"m{i}" in prompt

    def test_bucket_names_in_prompt(self):
        prompt = _build_prompt([_make_msg()])
        for bucket in ("NEEDS_REPLY", "FYI", "PROMOTIONS", "JUNK"):
            assert bucket in prompt


# ── Response parsing ──────────────────────────────────────────────────────────

class TestParseResponse:
    def _batch(self, ids=("m1",)):
        return [_make_msg(msg_id=i) for i in ids]

    def test_valid_response(self):
        raw = json.dumps([
            {"message_id": "m1", "bucket": "FYI", "confidence": 0.9, "reason": "receipt"}
        ])
        results = _parse_response(raw, self._batch(["m1"]))
        assert len(results) == 1
        assert results[0].bucket == "FYI"
        assert results[0].confidence == 0.9
        assert results[0].message_id == "m1"

    def test_strips_markdown_fences(self):
        raw = '```json\n[{"message_id":"m1","bucket":"JUNK","confidence":0.8,"reason":"noise"}]\n```'
        results = _parse_response(raw, self._batch(["m1"]))
        assert results[0].bucket == "JUNK"

    def test_strips_plain_code_fence(self):
        raw = '```\n[{"message_id":"m1","bucket":"FYI","confidence":0.7,"reason":"ok"}]\n```'
        results = _parse_response(raw, self._batch(["m1"]))
        assert results[0].bucket == "FYI"

    def test_invalid_bucket_becomes_unclassified(self):
        raw = json.dumps([
            {"message_id": "m1", "bucket": "GARBAGE", "confidence": 0.9, "reason": "bad"}
        ])
        results = _parse_response(raw, self._batch(["m1"]))
        assert results[0].bucket == "UNCLASSIFIED"
        assert results[0].confidence == 0.0

    def test_missing_message_returns_unclassified(self):
        # Model returned m2 but we asked about m1.
        raw = json.dumps([
            {"message_id": "m2", "bucket": "FYI", "confidence": 0.9, "reason": "ok"}
        ])
        results = _parse_response(raw, self._batch(["m1"]))
        assert results[0].message_id == "m1"
        assert results[0].bucket == "UNCLASSIFIED"

    def test_unknown_message_id_in_response_ignored(self):
        raw = json.dumps([
            {"message_id": "unknown", "bucket": "FYI", "confidence": 0.9, "reason": "ok"},
            {"message_id": "m1", "bucket": "JUNK", "confidence": 0.85, "reason": "noise"},
        ])
        results = _parse_response(raw, self._batch(["m1"]))
        assert len(results) == 1
        assert results[0].message_id == "m1"

    def test_confidence_clamped_to_zero_one(self):
        raw = json.dumps([
            {"message_id": "m1", "bucket": "FYI", "confidence": 1.5, "reason": "ok"}
        ])
        results = _parse_response(raw, self._batch(["m1"]))
        assert results[0].confidence == 1.0

        raw2 = json.dumps([
            {"message_id": "m1", "bucket": "FYI", "confidence": -0.5, "reason": "ok"}
        ])
        results2 = _parse_response(raw2, self._batch(["m1"]))
        assert results2[0].confidence == 0.0

    def test_malformed_json_all_unclassified(self):
        results = _parse_response("not valid json at all", self._batch(["m1", "m2"]))
        assert all(r.bucket == "UNCLASSIFIED" for r in results)
        assert len(results) == 2

    def test_empty_batch_returns_empty(self):
        results = _parse_response("[]", [])
        assert results == []

    def test_partial_array_fills_missing_as_unclassified(self):
        raw = json.dumps([
            {"message_id": "m1", "bucket": "FYI", "confidence": 0.9, "reason": "ok"}
            # m2 is missing
        ])
        results = _parse_response(raw, self._batch(["m1", "m2"]))
        buckets = {r.message_id: r.bucket for r in results}
        assert buckets["m1"] == "FYI"
        assert buckets["m2"] == "UNCLASSIFIED"


# ── _extract_text ─────────────────────────────────────────────────────────────

class TestExtractText:
    def test_extracts_correctly(self):
        response = {
            "candidates": [
                {"content": {"parts": [{"text": "hello"}]}}
            ]
        }
        assert _extract_text(response) == "hello"

    def test_missing_candidates_returns_none(self):
        assert _extract_text({}) is None

    def test_empty_candidates_returns_none(self):
        assert _extract_text({"candidates": []}) is None


# ── HTTP retry logic ──────────────────────────────────────────────────────────

class TestRetry:
    def _make_response(self, status: int, body: dict | None = None) -> httpx.Response:
        import httpx
        content = json.dumps(body or {}).encode()
        return httpx.Response(
            status_code=status,
            content=content,
            request=httpx.Request("POST", "https://example.com"),
        )

    def test_429_triggers_retry_then_succeeds(self):
        client = _make_client()
        good_body = _gemini_response([
            {"message_id": "m1", "bucket": "FYI", "confidence": 0.9, "reason": "ok"}
        ])
        responses = [
            self._make_response(429),
            self._make_response(200, good_body),
        ]
        with patch.object(client._http, "post", side_effect=responses):
            with patch("time.sleep"):  # Skip actual waits in tests
                results = client.classify_messages([_make_msg("m1")])
        assert results[0].bucket == "FYI"

    def test_non_retriable_error_returns_unclassified(self):
        client = _make_client()
        with patch.object(client._http, "post",
                          return_value=self._make_response(403)):
            with patch("time.sleep"):
                results = client.classify_messages([_make_msg("m1")])
        assert results[0].bucket == "UNCLASSIFIED"

    def test_all_retries_exhausted_returns_unclassified(self):
        client = _make_client()
        with patch.object(client._http, "post",
                          return_value=self._make_response(429)):
            with patch("time.sleep"):
                results = client.classify_messages([_make_msg("m1")])
        assert results[0].bucket == "UNCLASSIFIED"

    def test_timeout_returns_unclassified(self):
        client = _make_client()
        with patch.object(client._http, "post",
                          side_effect=httpx.TimeoutException("timed out")):
            with patch("time.sleep"):
                results = client.classify_messages([_make_msg("m1")])
        assert results[0].bucket == "UNCLASSIFIED"

    def test_empty_input_returns_empty(self):
        client = _make_client()
        results = client.classify_messages([])
        assert results == []