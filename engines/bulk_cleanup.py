"""
GmailCleaner — Bulk Cleanup Engine
Groups inbox emails by sender domain and subject prefix to identify
bulk deletion candidates, then scores and ranks them.

Grouping strategy (applied in this order, each message placed in first matching group):
  1. Exact sender domain  — all emails from @newsletters.example.com
  2. Subject prefix        — emails sharing a normalized subject root across domains

Scoring factors:
  - email_count            (volume — higher = noisier)
  - all_unread             (never opened — strong signal of worthless mail)
  - has_unsubscribe        (suggests promotional, not transactional)
  - domain_age_days        (oldest email in group — older = more entrenched noise)

Phase 2 scope: detection and candidate generation.
Execution (batch delete) is handled by gmail/executor.py.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from gmail.parser import GmailMessage
from store.db import Database

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CleanupGroup:
    """
    A group of emails identified as bulk deletion candidates.

    All message IDs in the group will be deleted if the user confirms this group.
    """
    group_id: str                          # Stable ID: "domain:<domain>" or "subject:<prefix>"
    group_type: str                        # "domain" | "subject"
    label: str                             # Human-readable label (domain or subject prefix)
    domain: Optional[str]                  # Sender domain (None for subject-prefix groups)
    email_count: int
    oldest_date: str                       # ISO date of oldest email in the group
    newest_date: str                       # ISO date of newest email in the group
    all_unread: bool                       # True if every email in the group is unread
    has_unsubscribe: bool                  # True if any email has a List-Unsubscribe header
    score: float                           # Composite score — higher = noisier / more worth cleaning
    message_ids: list[str] = field(default_factory=list)
    sample_subjects: list[str] = field(default_factory=list)  # Up to 3 example subjects

    @property
    def score_label(self) -> str:
        if self.score >= 0.75:
            return "high"
        if self.score >= 0.40:
            return "medium"
        return "low"


# ──────────────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────────────

class BulkCleanupEngine:
    """
    Scans parsed messages to surface bulk deletion candidates.

    Rules:
      1. A domain group requires at least min_group_size emails from the same domain.
      2. A subject-prefix group requires at least min_group_size emails sharing a
         normalized subject prefix, across any number of domains.
      3. Domains in blocked_senders are skipped entirely.
      4. Groups are ranked by composite score descending.
    """

    def __init__(
        self,
        db: Database,
        min_group_size: int = 5,
        blocked_senders: Optional[list[str]] = None,
    ) -> None:
        self._db = db
        self._min_group_size = min_group_size
        self._blocked_senders = set(blocked_senders or [])

    def detect(self, messages: list[GmailMessage]) -> list[CleanupGroup]:
        """
        Run bulk cleanup detection on a list of parsed messages.

        Args:
            messages: List of GmailMessage objects from the scan phase.

        Returns:
            Ranked list of CleanupGroup objects (highest score first).
        """
        blocked = self._blocked_senders | self._db.get_blocked_domains()

        # ── Phase 1: domain groups ────────────────────────────────────────────
        by_domain: dict[str, list[GmailMessage]] = defaultdict(list)
        for msg in messages:
            if msg.sender_domain and msg.sender_domain not in blocked:
                by_domain[msg.sender_domain].append(msg)

        # Track which message IDs are already claimed by a domain group
        # so subject-prefix groups don't double-count them.
        claimed_ids: set[str] = set()
        groups: list[CleanupGroup] = []

        for domain, msgs in by_domain.items():
            if len(msgs) < self._min_group_size:
                continue
            group = _build_domain_group(domain, msgs)
            groups.append(group)
            claimed_ids.update(group.message_ids)

        # ── Phase 2: subject-prefix groups (unclaimed messages only) ──────────
        unclaimed = [m for m in messages if m.id not in claimed_ids
                     and m.sender_domain not in blocked]
        subject_groups = _build_subject_groups(unclaimed, self._min_group_size)
        groups.extend(subject_groups)

        # ── Score and sort ────────────────────────────────────────────────────
        for group in groups:
            group.score = _score_group(group)
        groups.sort(key=lambda g: g.score, reverse=True)

        domain_count = sum(1 for g in groups if g.group_type == "domain")
        subject_count = sum(1 for g in groups if g.group_type == "subject")
        total_msgs = sum(g.email_count for g in groups)

        logger.info(
            "Bulk cleanup scan: %d group(s) (%d domain, %d subject), %d total emails",
            len(groups), domain_count, subject_count, total_msgs,
        )
        return groups


# ──────────────────────────────────────────────────────────────────────────────
# Group builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_domain_group(domain: str, messages: list[GmailMessage]) -> CleanupGroup:
    messages_sorted = sorted(messages, key=lambda m: m.date)
    sample_subjects = list({m.subject for m in messages[:10]})[:3]

    return CleanupGroup(
        group_id=f"domain:{domain}",
        group_type="domain",
        label=domain,
        domain=domain,
        email_count=len(messages),
        oldest_date=messages_sorted[0].date.date().isoformat(),
        newest_date=messages_sorted[-1].date.date().isoformat(),
        all_unread=all(m.is_unread for m in messages),
        has_unsubscribe=any(m.unsubscribe and m.unsubscribe.has_any for m in messages),
        score=0.0,  # Filled by _score_group
        message_ids=[m.id for m in messages],
        sample_subjects=sample_subjects,
    )


def _build_subject_groups(
    messages: list[GmailMessage],
    min_group_size: int,
) -> list[CleanupGroup]:
    """
    Cluster messages by normalized subject prefix.

    Normalization strips:
      - Leading Re:/Fwd:/Fw: chains
      - Issue/edition numbers (e.g. "#42", "Issue 7")
      - Dates embedded in subjects
      - Trailing punctuation and whitespace

    This lets "Your Weekly Digest #41" and "Your Weekly Digest #42"
    cluster together as "Your Weekly Digest".
    """
    by_prefix: dict[str, list[GmailMessage]] = defaultdict(list)
    for msg in messages:
        prefix = _normalize_subject(msg.subject)
        if prefix:
            by_prefix[prefix].append(msg)

    groups = []
    for prefix, msgs in by_prefix.items():
        if len(msgs) < min_group_size:
            continue
        msgs_sorted = sorted(msgs, key=lambda m: m.date)
        sample_subjects = list({m.subject for m in msgs[:10]})[:3]

        groups.append(CleanupGroup(
            group_id=f"subject:{prefix[:60]}",
            group_type="subject",
            label=f'"{prefix[:60]}"',
            domain=None,
            email_count=len(msgs),
            oldest_date=msgs_sorted[0].date.date().isoformat(),
            newest_date=msgs_sorted[-1].date.date().isoformat(),
            all_unread=all(m.is_unread for m in msgs),
            has_unsubscribe=any(m.unsubscribe and m.unsubscribe.has_any for m in msgs),
            score=0.0,
            message_ids=[m.id for m in msgs],
            sample_subjects=sample_subjects,
        ))
    return groups


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────

def _score_group(group: CleanupGroup) -> float:
    """
    Compute a composite noise score in [0.0, 1.0].

    Weights:
      - Volume (email_count):   40% — normalized against a reference of 50 emails
      - All unread:             30% — never-opened mail is pure noise
      - Has unsubscribe header: 20% — strong signal of promotional bulk mail
      - Domain group type:      10% — domain groups are more reliable than subject clusters
    """
    # Volume score: caps at 1.0 once count >= 50.
    volume_score = min(group.email_count / 50.0, 1.0)

    unread_score = 1.0 if group.all_unread else 0.0
    unsub_score = 1.0 if group.has_unsubscribe else 0.0
    type_score = 1.0 if group.group_type == "domain" else 0.5

    return (
        0.40 * volume_score
        + 0.30 * unread_score
        + 0.20 * unsub_score
        + 0.10 * type_score
    )


# ──────────────────────────────────────────────────────────────────────────────
# Subject normalization
# ──────────────────────────────────────────────────────────────────────────────

# Prefixes to strip before extracting the subject root.
_RE_REPLY_FWD = re.compile(r"^(re|fwd?)\s*:\s*", re.IGNORECASE)
# Issue / edition numbers: "#42", "No. 7", "Issue 3", "Vol. 2"
_RE_ISSUE_NUM = re.compile(r"#\d+\b|\b(?:no\.?\s*\d+|issue\s*\d+|vol\.?\s*\d+|edition\s*\d+)\b", re.IGNORECASE)
# Embedded dates: "May 2025", "2025-05-16", "05/16/25"
_RE_DATE = re.compile(
    r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}|"
    r"\d{4}[\/\-]\d{2}[\/\-]\d{2})\b",
    re.IGNORECASE,
)
_RE_TRAILING = re.compile(r"[\s\-–—|:]+$")
_RE_WHITESPACE = re.compile(r"\s+")


def _normalize_subject(subject: str) -> str:
    """
    Strip variable parts from a subject line to extract a stable prefix
    suitable for grouping across multiple emails in a series.

    Returns an empty string if the result is too short to be meaningful.
    """
    s = subject.strip()
    # Strip Re:/Fwd: chains.
    while _RE_REPLY_FWD.match(s):
        s = _RE_REPLY_FWD.sub("", s).strip()
    # Remove issue numbers, dates.
    s = _RE_ISSUE_NUM.sub("", s)
    s = _RE_DATE.sub("", s)
    # Collapse whitespace and strip trailing separators.
    s = _RE_WHITESPACE.sub(" ", s).strip()
    s = _RE_TRAILING.sub("", s).strip()
    # Require at least 6 chars to be a useful cluster key.
    return s if len(s) >= 6 else ""