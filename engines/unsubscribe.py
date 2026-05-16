"""
GmailCleaner — Unsubscribe Engine
Detects unsubscribe candidates from parsed messages and (in execute phase)
performs the actual unsubscription via HTTP or mailto.

Phase 1 scope: detection and candidate generation only.
Execution (HTTP POST / mailto send) is called from gmail/executor.py in Phase 2.

RFC references:
  - RFC 2369: List-Unsubscribe header (HTTP and mailto mechanisms)
  - RFC 8058: List-Unsubscribe-Post one-click mechanism
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from gmail.parser import GmailMessage, UnsubscribeTarget
from store.db import Database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UnsubscribeCandidate:
    """
    A sender domain identified as an unsubscribe candidate.

    One candidate is produced per sender domain (not per email).
    The representative email is the most recent one from that domain.
    """
    domain: str
    display_name: str                         # Best display name seen for this domain
    sender_email: str                         # Representative sender email
    email_count: int                          # Total emails from this domain in scan window
    unsubscribe: UnsubscribeTarget            # Parsed unsubscribe targets
    representative_subject: str               # Subject of the most recent email
    most_recent_date: str                     # ISO date string
    already_unsubscribed: bool = False        # True if DB shows prior unsubscribe
    sample_message_ids: list[str] = field(default_factory=list)  # Up to 5 message IDs

    @property
    def method_label(self) -> str:
        if self.unsubscribe.supports_one_click:
            return "One-click (RFC 8058)"
        if self.unsubscribe.http_url:
            return "HTTP"
        if self.unsubscribe.mailto:
            return "mailto"
        return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────────────

class UnsubscribeEngine:
    """
    Scans parsed messages for List-Unsubscribe headers and produces a
    deduplicated list of UnsubscribeCandidates for the confirm-first flow.

    Rules:
      1. Only domains that have a valid List-Unsubscribe target are included.
      2. Domains already unsubscribed in the DB are filtered out (with a log note).
      3. Domains in the blocked_senders config are skipped.
      4. Candidates are ranked by email_count descending (noisiest senders first).
      5. Minimum email count threshold filters single-occurrence senders.
    """

    def __init__(
        self,
        db: Database,
        min_email_count: int = 2,
        blocked_senders: Optional[list[str]] = None,
    ) -> None:
        self._db = db
        self._min_email_count = min_email_count
        self._blocked_senders = set(blocked_senders or [])

    def detect(self, messages: list[GmailMessage]) -> list[UnsubscribeCandidate]:
        """
        Run unsubscribe detection on a list of parsed messages.

        Args:
            messages: List of GmailMessage objects from the scan phase.

        Returns:
            Ranked list of UnsubscribeCandidate objects (noisiest first).
        """
        already_unsubscribed = self._db.get_unsubscribed_domains()
        db_blocked = self._db.get_blocked_domains()
        all_blocked = self._blocked_senders | db_blocked

        # Group messages by sender domain.
        by_domain: dict[str, list[GmailMessage]] = defaultdict(list)
        for msg in messages:
            if msg.sender_domain:
                by_domain[msg.sender_domain].append(msg)

        candidates: list[UnsubscribeCandidate] = []

        for domain, domain_messages in by_domain.items():
            # Skip blocked domains.
            if domain in all_blocked:
                logger.debug("Skipping blocked domain: %s", domain)
                continue

            # Find messages with a valid unsubscribe target.
            unsub_messages = [m for m in domain_messages if m.unsubscribe and m.unsubscribe.has_any]
            if not unsub_messages:
                continue

            total_count = len(domain_messages)

            # Skip domains below the minimum email threshold.
            if total_count < self._min_email_count:
                logger.debug(
                    "Domain %s has only %d email(s) (min=%d) — skipping",
                    domain, total_count, self._min_email_count,
                )
                continue

            # Sort newest first to get the best representative.
            unsub_messages.sort(key=lambda m: m.date, reverse=True)
            representative = unsub_messages[0]

            # Use the most complete unsubscribe target available
            # (prefers one-click RFC 8058 > HTTP > mailto).
            best_target = _pick_best_target(unsub_messages)
            if not best_target:
                continue

            candidate = UnsubscribeCandidate(
                domain=domain,
                display_name=_best_display_name(domain_messages),
                sender_email=representative.sender_email,
                email_count=total_count,
                unsubscribe=best_target,
                representative_subject=representative.subject,
                most_recent_date=representative.date.date().isoformat(),
                already_unsubscribed=domain in already_unsubscribed,
                sample_message_ids=[m.id for m in domain_messages[:5]],
            )
            candidates.append(candidate)

        # Sort by email count descending (noisiest first).
        candidates.sort(key=lambda c: c.email_count, reverse=True)

        total = len(candidates)
        new = sum(1 for c in candidates if not c.already_unsubscribed)
        already = total - new
        logger.info(
            "Unsubscribe scan complete: %d candidate(s) (%d new, %d already unsubscribed)",
            total, new, already,
        )
        return candidates

    def get_new_candidates(self, messages: list[GmailMessage]) -> list[UnsubscribeCandidate]:
        """Convenience: return only candidates not yet unsubscribed."""
        return [c for c in self.detect(messages) if not c.already_unsubscribed]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pick_best_target(messages: list[GmailMessage]) -> Optional[UnsubscribeTarget]:
    """
    Among messages with unsubscribe headers, pick the most capable target:
    one-click RFC 8058 > HTTP > mailto.
    """
    one_click = next((m.unsubscribe for m in messages if m.unsubscribe and m.unsubscribe.supports_one_click), None)
    if one_click:
        return one_click

    http = next((m.unsubscribe for m in messages if m.unsubscribe and m.unsubscribe.http_url), None)
    if http:
        return http

    mailto = next((m.unsubscribe for m in messages if m.unsubscribe and m.unsubscribe.mailto), None)
    return mailto


def _best_display_name(messages: list[GmailMessage]) -> str:
    """
    Return the most common non-empty display name across messages from a domain.
    Falls back to sender_email if no display name is found.
    """
    from collections import Counter
    names = [m.sender_name for m in messages if m.sender_name]
    if not names:
        return messages[0].sender_email if messages else ""
    return Counter(names).most_common(1)[0][0]