"""
GmailCleaner — Triage Engine
Classifies inbox messages into four buckets using Gemini (model set in settings.yaml).

Buckets (in priority order for the digest):
  NEEDS_REPLY  — Direct messages requiring a response
  FYI          — Informational, no action needed
  PROMOTIONS   — Marketing, newsletters, offers
  JUNK         — Low-value noise

Privacy guarantee:
  Only sender email, subject, snippet (first ~120 chars), and Gmail labels
  are sent to Gemini. Email body content is never transmitted.

Behaviour:
  - Classification of all inbox messages not in domain/cleanup groups
  - Confidence-based flagging: messages below threshold are UNCLASSIFIED
  - Results cached in DB to avoid re-classifying on re-runs within 24h
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ai.gemini import GeminiClassification, GeminiClient
from gmail.parser import GmailMessage
from store.db import Database

logger = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

BUCKET_ORDER = ["NEEDS_REPLY", "FYI", "PROMOTIONS", "JUNK", "UNCLASSIFIED"]


@dataclass
class TriageResult:
    """
    Triage classification for a single message.
    Extends GeminiClassification with message context for the digest.
    """
    message_id: str
    bucket: str
    confidence: float
    reason: str
    sender_name: str
    sender_email: str
    subject: str
    snippet: str
    date: str         # ISO date string
    is_unread: bool
    low_confidence: bool

    @classmethod
    def from_classification(
        cls,
        classification: GeminiClassification,
        message: GmailMessage,
        confidence_threshold: float,
    ) -> "TriageResult":
        return cls(
            message_id=classification.message_id,
            bucket=classification.bucket,
            confidence=classification.confidence,
            reason=classification.reason,
            sender_name=message.sender_name,
            sender_email=message.sender_email,
            subject=message.subject,
            snippet=message.snippet,
            date=message.date.date().isoformat(),
            is_unread=message.is_unread,
            low_confidence=classification.confidence < confidence_threshold,
        )


@dataclass
class TriageSummary:
    """Aggregate view of triage results for the digest and report."""
    needs_reply: list[TriageResult] = field(default_factory=list)
    fyi: list[TriageResult] = field(default_factory=list)
    promotions: list[TriageResult] = field(default_factory=list)
    junk: list[TriageResult] = field(default_factory=list)
    unclassified: list[TriageResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (len(self.needs_reply) + len(self.fyi)
                + len(self.promotions) + len(self.junk)
                + len(self.unclassified))

    def bucket(self, name: str) -> list[TriageResult]:
        return getattr(self, name.lower(), [])


# ── Engine ────────────────────────────────────────────────────────────────────

class TriageEngine:
    """
    Classifies inbox messages using Gemini.

    Skips messages that:
      - Are already in a domain/cleanup group (passed as excluded_ids)
      - Were classified in the last 24 hours (cached in DB)

    Low-confidence results (below confidence_threshold) are placed in
    UNCLASSIFIED rather than their guessed bucket.
    """

    def __init__(
        self,
        gemini_client: GeminiClient,
        db: Database,
        confidence_threshold: float = 0.75,
    ) -> None:
        self._gemini = gemini_client
        self._db = db
        self._threshold = confidence_threshold

    def close(self) -> None:
        """Release the underlying Gemini HTTP client."""
        self._gemini.__exit__(None, None, None)

    def classify(
        self,
        messages: list[GmailMessage],
        excluded_ids: Optional[set[str]] = None,
    ) -> TriageSummary:
        """
        Classify a list of messages and return a TriageSummary.

        Args:
            messages:     All parsed inbox messages from the scan.
            excluded_ids: Message IDs already claimed by cleanup groups.
                          These are skipped — no point classifying mail we plan to delete.

        Returns:
            TriageSummary with messages sorted into buckets.
        """
        excluded = excluded_ids or set()

        # Filter to messages not already handled by cleanup.
        candidates = [m for m in messages if m.id not in excluded]

        if not candidates:
            logger.info("Triage: no messages to classify (all excluded by cleanup).")
            return TriageSummary()

        # Check DB cache — skip messages classified within the last 24 hours.
        cached = self._db.get_recent_triage_results(hours=24)
        cached_ids = {r["message_id"] for r in cached}
        to_classify = [m for m in candidates if m.id not in cached_ids]

        logger.info(
            "Triage: %d messages to classify (%d cached, %d excluded)",
            len(to_classify), len(candidates) - len(to_classify), len(excluded),
        )

        # Build metadata dicts for Gemini (no body content).
        metadata = [_to_metadata(m) for m in to_classify]

        # Call Gemini.
        classifications: list[GeminiClassification] = []
        if metadata:
            classifications = self._gemini.classify_messages(metadata)
            # Cache results in DB.
            self._db.save_triage_results(
                [
                    {
                        "message_id": c.message_id,
                        "bucket": c.bucket,
                        "confidence": c.confidence,
                        "reason": c.reason,
                        "model": c.model,
                    }
                    for c in classifications
                ]
            )

        # Build a message_id → GmailMessage lookup for context enrichment.
        msg_by_id = {m.id: m for m in candidates}

        # Merge fresh classifications with cached results.
        all_results: list[TriageResult] = []

        for c in classifications:
            msg = msg_by_id.get(c.message_id)
            if not msg:
                continue
            # Enforce confidence threshold: low-confidence → UNCLASSIFIED.
            if c.confidence < self._threshold and c.bucket != "UNCLASSIFIED":
                logger.debug(
                    "Message %s classified as %s with low confidence (%.2f) — marking UNCLASSIFIED",
                    c.message_id, c.bucket, c.confidence,
                )
                c = GeminiClassification(
                    message_id=c.message_id,
                    bucket="UNCLASSIFIED",
                    confidence=c.confidence,
                    reason=f"Low confidence ({c.confidence:.0%}) for original bucket {c.bucket}: {c.reason}",
                )
            all_results.append(
                TriageResult.from_classification(c, msg, self._threshold)
            )

        # Merge cached results.
        for row in cached:
            if row["message_id"] in {r.message_id for r in all_results}:
                continue
            msg = msg_by_id.get(row["message_id"])
            if not msg:
                continue
            c = GeminiClassification(
                message_id=row["message_id"],
                bucket=row["bucket"],
                confidence=row["confidence"],
                reason=row["reason"],
            )
            all_results.append(
                TriageResult.from_classification(c, msg, self._threshold)
            )

        summary = _build_summary(all_results)
        logger.info(
            "Triage summary: NEEDS_REPLY=%d  FYI=%d  PROMOTIONS=%d  JUNK=%d  UNCLASSIFIED=%d",
            len(summary.needs_reply), len(summary.fyi),
            len(summary.promotions), len(summary.junk), len(summary.unclassified),
        )
        return summary


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_metadata(msg: GmailMessage) -> dict[str, str]:
    """Convert a GmailMessage to the minimal metadata dict sent to Gemini."""
    return {
        "message_id": msg.id,
        "sender": msg.sender_email,
        "subject": msg.subject,
        "snippet": msg.snippet,
        "labels": ", ".join(msg.labels),
    }


def _build_summary(results: list[TriageResult]) -> TriageSummary:
    """Sort TriageResult objects into a TriageSummary by bucket."""
    summary = TriageSummary()
    for r in results:
        bucket_name = r.bucket.lower() if r.bucket != "NEEDS_REPLY" else "needs_reply"
        target = getattr(summary, bucket_name, summary.unclassified)
        target.append(r)

    # Sort each bucket: unread first, then by date descending.
    def _sort_key(r: TriageResult):
        # Ascending on (not is_unread): unread=True → 0, unread=False → 1 → unread first.
        # Descending on date: negate by reversing string (ISO dates sort lexicographically).
        # We use a tuple and sort reverse=True for date, but since we need mixed,
        # negate is_unread flag already handles it; for date, prefix with "-" won't work on str.
        # Solution: sort ascending on (not_unread, inverted_date) via a tuple.
        return (not r.is_unread, tuple(-ord(c) for c in r.date))

    summary.needs_reply.sort(key=_sort_key)
    summary.fyi.sort(key=_sort_key)
    summary.promotions.sort(key=_sort_key)
    summary.junk.sort(key=_sort_key)
    summary.unclassified.sort(key=_sort_key)

    return summary