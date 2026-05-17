"""
GmailCleaner — Gemini API Client
Thin wrapper around the Gemini 1.5 Flash REST API for email classification.

Design decisions:
  - Uses the generativelanguage.googleapis.com REST API directly.
    No google-generativeai SDK dependency; one less thing to install and update.
  - Sends up to BATCH_SIZE messages per request to minimise API call count.
  - Prompts the model to return only JSON — no markdown fences, no preamble.
  - Parses responses defensively: any message that can't be parsed is returned
    as UNCLASSIFIED with confidence 0.0, rather than crashing the pipeline.
  - Implements exponential backoff with jitter on HTTP 429 / 503.

Gemini 1.5 Flash free tier (as of 2025):
  - 15 requests per minute
  - 1,000,000 tokens per day
  - 1,500 requests per day
For a personal inbox scan of 500 emails, we use ~25 API calls (500 / 20 per batch).
"""

import json
import logging
import os
import time
import random
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-1.5-flash"
# Messages per Gemini request. Higher = fewer API calls but larger prompts.
BATCH_SIZE = 20
# Retry config for 429 / 503 responses.
MAX_RETRIES = 5
BASE_BACKOFF = 4.0   # seconds
MAX_BACKOFF = 60.0   # seconds cap
# Request timeout.
HTTP_TIMEOUT = 30.0


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class GeminiClassification:
    """Raw classification result for a single message from Gemini."""
    message_id: str
    bucket: str           # NEEDS_REPLY | FYI | PROMOTIONS | JUNK | UNCLASSIFIED
    confidence: float     # 0.0 – 1.0
    reason: str           # One-sentence explanation from the model
    model: str = GEMINI_MODEL

    @property
    def is_low_confidence(self, threshold: float = 0.6) -> bool:
        return self.confidence < threshold


# ── Client ────────────────────────────────────────────────────────────────────

class GeminiClient:
    """
    Gemini 1.5 Flash REST client for batch email classification.

    Usage:
        client = GeminiClient(api_key="...")
        results = client.classify_messages(messages_metadata)
    """

    VALID_BUCKETS = {"NEEDS_REPLY", "FYI", "PROMOTIONS", "JUNK", "UNCLASSIFIED"}

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = GEMINI_MODEL,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. "
                "Get a free key at https://aistudio.google.com/app/apikey "
                "and add it to your .env file."
            )
        self._model = model
        self._batch_size = batch_size
        self._url = f"{GEMINI_API_BASE}/{self._model}:generateContent"
        self._http = httpx.Client(timeout=HTTP_TIMEOUT)

    def __enter__(self) -> "GeminiClient":
        return self

    def __exit__(self, *args) -> None:
        self._http.close()

    def classify_messages(
        self,
        messages: list[dict[str, str]],
    ) -> list[GeminiClassification]:
        """
        Classify a list of email metadata dicts into inbox buckets.

        Each dict in `messages` must have keys:
            message_id, sender, subject, snippet, labels

        Body content is never included — classification is metadata-only.
        Messages are sent to Gemini in batches of self._batch_size.

        Args:
            messages: List of email metadata dicts.

        Returns:
            List of GeminiClassification objects, one per input message.
            Messages that fail to classify are returned as UNCLASSIFIED.
        """
        if not messages:
            return []

        results: list[GeminiClassification] = []
        total = len(messages)
        total_batches = (total + self._batch_size - 1) // self._batch_size

        for batch_num, start in enumerate(range(0, total, self._batch_size), 1):
            batch = messages[start: start + self._batch_size]
            logger.debug(
                "Gemini batch %d/%d: classifying %d messages",
                batch_num, total_batches, len(batch),
            )
            batch_results = self._classify_batch(batch)
            results.extend(batch_results)

            # Respect the 15 RPM free tier limit: ~4s between batches.
            if start + self._batch_size < total:
                time.sleep(4.1)

        logger.info(
            "Gemini classification complete: %d/%d messages classified",
            sum(1 for r in results if r.bucket != "UNCLASSIFIED"),
            total,
        )
        return results

    # ── Private ───────────────────────────────────────────────────────────────

    def _classify_batch(self, batch: list[dict[str, str]]) -> list[GeminiClassification]:
        """Send one batch to Gemini and parse the response."""
        prompt = _build_prompt(batch)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,       # Low temperature = more deterministic classification
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
            },
        }

        raw_text = self._post_with_retry(payload)
        if raw_text is None:
            # All retries failed — return UNCLASSIFIED for every message in batch.
            return [_make_unclassified(m["message_id"], "API call failed after retries")
                    for m in batch]

        return _parse_response(raw_text, batch)

    def _post_with_retry(self, payload: dict) -> Optional[str]:
        """
        POST to the Gemini API with exponential backoff on 429 / 503.

        Returns the raw response text on success, or None if all retries fail.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._http.post(
                    self._url,
                    params={"key": self._api_key},
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code == 200:
                    return _extract_text(resp.json())

                if resp.status_code in (429, 503):
                    backoff = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                    jitter = random.uniform(0, backoff * 0.2)
                    wait = backoff + jitter
                    logger.warning(
                        "Gemini rate limited (HTTP %d), attempt %d/%d. "
                        "Waiting %.1fs...",
                        resp.status_code, attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                # Non-retriable HTTP error.
                logger.error(
                    "Gemini API error (HTTP %d): %s",
                    resp.status_code, resp.text[:200],
                )
                return None

            except httpx.TimeoutException:
                logger.warning("Gemini request timed out (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt < MAX_RETRIES:
                    time.sleep(BASE_BACKOFF * attempt)
            except httpx.RequestError as exc:
                logger.error("Gemini network error: %s", exc)
                return None

        logger.error("Gemini: all %d attempts exhausted.", MAX_RETRIES)
        return None


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(batch: list[dict[str, str]]) -> str:
    """
    Build the classification prompt for a batch of messages.

    The prompt instructs the model to:
      1. Classify each message into exactly one of four buckets.
      2. Assign a confidence score between 0.0 and 1.0.
      3. Provide a one-sentence reason.
      4. Return ONLY a JSON array — no markdown, no commentary.

    Privacy guarantee: only sender, subject, snippet and labels are included.
    Email body is never part of the prompt.
    """
    bucket_definitions = """
Buckets:
  NEEDS_REPLY   — Requires a response from you. Direct messages, questions, meeting requests, action items.
  FYI           — Informational only. Receipts, order confirmations, shipping updates, calendar invites, status updates.
  PROMOTIONS    — Marketing, newsletters, product announcements, deals, offers.
  JUNK          — Low-value noise. Mass mailings, automated alerts you don't need, social notifications.

Rules:
  - Choose exactly one bucket per message.
  - Confidence is your certainty (0.0 = completely unsure, 1.0 = certain).
  - Confidence < 0.6 means the classification is uncertain and will be flagged for human review.
  - Return ONLY a valid JSON array. No markdown, no code fences, no explanation outside the JSON.
"""

    messages_block = "\n".join(
        f'  {{"index": {i}, "message_id": "{m["message_id"]}", '
        f'"sender": "{_safe(m["sender"])}", "subject": "{_safe(m["subject"])}", '
        f'"snippet": "{_safe(m["snippet"][:120])}", "labels": "{_safe(m["labels"])}"}}'
        for i, m in enumerate(batch)
    )

    return f"""You are an email classifier. Classify each email below into exactly one inbox bucket.

{bucket_definitions}

Emails to classify:
[
{messages_block}
]

Return a JSON array with one object per email:
[
  {{"message_id": "<id>", "bucket": "<BUCKET>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}},
  ...
]"""


def _safe(s: str) -> str:
    """Escape characters that would break JSON string embedding in the prompt."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")


# ── Response parsing ──────────────────────────────────────────────────────────

def _extract_text(response_json: dict) -> Optional[str]:
    """Pull the text content out of a Gemini generateContent response."""
    try:
        return response_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        logger.warning("Unexpected Gemini response structure: %s — %s", exc, str(response_json)[:200])
        return None


def _parse_response(
    raw_text: str,
    batch: list[dict[str, str]],
) -> list[GeminiClassification]:
    """
    Parse the Gemini response text into GeminiClassification objects.

    Handles:
      - Valid JSON arrays
      - JSON wrapped in markdown code fences (model ignores the instruction sometimes)
      - Partial arrays (some messages classified, others missing)
      - Completely malformed responses

    Any message not successfully parsed is returned as UNCLASSIFIED.
    """
    batch_ids = {m["message_id"] for m in batch}
    classified: dict[str, GeminiClassification] = {}

    # Strip markdown fences if the model added them despite instructions.
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first line (```json or ```) and last line (```)
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array, got {type(data).__name__}")

        for item in data:
            msg_id = str(item.get("message_id", "")).strip()
            bucket = str(item.get("bucket", "")).strip().upper()
            confidence = float(item.get("confidence", 0.0))
            reason = str(item.get("reason", "")).strip()

            if msg_id not in batch_ids:
                logger.debug("Gemini returned unknown message_id %r — skipping", msg_id)
                continue

            if bucket not in GeminiClient.VALID_BUCKETS:
                logger.warning("Gemini returned invalid bucket %r for %s — marking UNCLASSIFIED", bucket, msg_id)
                bucket = "UNCLASSIFIED"
                confidence = 0.0

            confidence = max(0.0, min(1.0, confidence))  # Clamp to [0, 1]

            classified[msg_id] = GeminiClassification(
                message_id=msg_id,
                bucket=bucket,
                confidence=confidence,
                reason=reason,
            )

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Failed to parse Gemini response: %s\nRaw text: %s", exc, raw_text[:300])

    # Fill in UNCLASSIFIED for any messages missing from the response.
    results = []
    for m in batch:
        if m["message_id"] in classified:
            results.append(classified[m["message_id"]])
        else:
            results.append(_make_unclassified(m["message_id"], "Not returned by model"))
    return results


def _make_unclassified(message_id: str, reason: str) -> GeminiClassification:
    return GeminiClassification(
        message_id=message_id,
        bucket="UNCLASSIFIED",
        confidence=0.0,
        reason=reason,
    )