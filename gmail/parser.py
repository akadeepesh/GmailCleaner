"""
GmailCleaner — Gmail Message Parser
Converts raw Gmail API message dicts into clean, typed GmailMessage dataclasses.

Normalization applied here:
  - Sender email extracted and lowercased from display-name formatted headers
  - Sender domain isolated for domain-level analysis
  - List-Unsubscribe header parsed into structured fields
  - Internal date converted from Gmail's epoch-millisecond format to datetime
  - Label list normalized to uppercase strings
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.headerregistry import Address
from email.utils import parseaddr
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Headers we request in metadata format. Keeping this list tight minimises
# response payload size and quota usage.
METADATA_HEADERS = [
    "From",
    "Subject",
    "Date",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "Message-ID",
]


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UnsubscribeTarget:
    """
    Parsed representation of a List-Unsubscribe header value.

    RFC 2369 allows multiple mechanisms (HTTP URL and/or mailto) in a single header.
    RFC 8058 adds List-Unsubscribe-Post for one-click POST unsubscription.
    """
    http_url: Optional[str] = None     # HTTPS URL to POST/GET for unsubscription
    mailto: Optional[str] = None       # mailto: address to send blank email to
    supports_one_click: bool = False   # True if List-Unsubscribe-Post header is present

    @property
    def has_any(self) -> bool:
        return bool(self.http_url or self.mailto)

    @property
    def preferred_method(self) -> Optional[str]:
        """Return the preferred unsubscribe method: HTTP > mailto."""
        if self.http_url:
            return "http"
        if self.mailto:
            return "mailto"
        return None


@dataclass
class GmailMessage:
    """
    Normalized representation of a Gmail message.

    All string fields are stripped of surrounding whitespace.
    All optional fields are None when the data was absent in the API response.
    """
    id: str
    thread_id: str
    sender_raw: str                      # Raw From header value (e.g. "Alice <alice@example.com>")
    sender_name: str                     # Display name (e.g. "Alice")
    sender_email: str                    # Normalized email (e.g. "alice@example.com")
    sender_domain: str                   # Domain portion (e.g. "example.com")
    subject: str
    date: datetime                       # UTC datetime parsed from internalDate
    labels: list[str] = field(default_factory=list)
    snippet: str = ""
    message_id_header: Optional[str] = None  # RFC 2822 Message-ID header
    unsubscribe: Optional[UnsubscribeTarget] = None

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.labels

    @property
    def is_in_inbox(self) -> bool:
        return "INBOX" in self.labels


# ──────────────────────────────────────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_message(raw: dict[str, Any]) -> Optional[GmailMessage]:
    """
    Parse a raw Gmail API message resource into a GmailMessage.

    Returns None if the message is malformed or missing required fields,
    logging a warning so the calling code can skip it cleanly.

    Args:
        raw: A Gmail API message resource dict (format='metadata' or 'full').

    Returns:
        GmailMessage, or None if parsing fails.
    """
    msg_id = raw.get("id", "<unknown>")

    try:
        headers = _extract_headers(raw)
        from_header = headers.get("from", "")
        sender_name, sender_email = _parse_address(from_header)
        sender_domain = _extract_domain(sender_email)

        date = _parse_internal_date(raw.get("internalDate"))
        if date is None:
            logger.warning("Message %s missing internalDate — skipping", msg_id)
            return None

        unsubscribe = _parse_unsubscribe_header(
            headers.get("list-unsubscribe"),
            headers.get("list-unsubscribe-post"),
        )

        return GmailMessage(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            sender_raw=from_header,
            sender_name=sender_name,
            sender_email=sender_email,
            sender_domain=sender_domain,
            subject=headers.get("subject", "").strip() or "(no subject)",
            date=date,
            labels=[lbl.upper() for lbl in raw.get("labelIds", [])],
            snippet=raw.get("snippet", "").strip(),
            message_id_header=headers.get("message-id"),
            unsubscribe=unsubscribe,
        )

    except Exception as exc:
        logger.warning("Failed to parse message %s: %s — skipping", msg_id, exc)
        return None


def parse_messages(raws: list[dict[str, Any]]) -> list[GmailMessage]:
    """
    Parse a list of raw API message resources.
    Invalid messages are silently omitted from the result.
    """
    results = []
    for raw in raws:
        parsed = parse_message(raw)
        if parsed:
            results.append(parsed)
    logger.debug("Parsed %d/%d messages successfully", len(results), len(raws))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_headers(raw: dict[str, Any]) -> dict[str, str]:
    """
    Flatten the Gmail API's header list into a lowercase-keyed dict.
    When a header appears multiple times, the last value wins.
    """
    headers: dict[str, str] = {}
    payload = raw.get("payload", {})
    for header in payload.get("headers", []):
        name = header.get("name", "").lower().strip()
        value = header.get("value", "").strip()
        if name:
            headers[name] = value
    return headers


def _parse_address(from_header: str) -> tuple[str, str]:
    """
    Parse a From header into (display_name, email).

    Handles formats:
      - "Alice Smith <alice@example.com>"
      - "alice@example.com"
      - "Alice Smith" (malformed — no email; returns empty email)
    """
    if not from_header:
        return ("", "")

    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    # parseaddr can return the whole thing as display_name if angle brackets are absent.
    if not email_addr and "@" in display_name:
        email_addr = display_name.lower().strip()
        display_name = ""

    return (display_name.strip(), email_addr)


def _extract_domain(email: str) -> str:
    """
    Extract the domain from an email address.

    Examples:
      "alice@example.com"              → "example.com"
      "newsletter@mail.substack.com"   → "mail.substack.com"
      ""                               → ""
    """
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower().strip()


def _parse_internal_date(internal_date: Optional[str]) -> Optional[datetime]:
    """
    Convert Gmail's internalDate (Unix epoch milliseconds as string) to a UTC datetime.
    """
    if not internal_date:
        return None
    try:
        epoch_ms = int(internal_date)
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    except (ValueError, TypeError) as exc:
        logger.debug("Could not parse internalDate %r: %s", internal_date, exc)
        return None


# Matches content within angle brackets: <https://...> or <mailto:...>
_ANGLE_BRACKET_RE = re.compile(r"<([^>]+)>")


def _parse_unsubscribe_header(
    header_value: Optional[str],
    post_header: Optional[str],
) -> Optional[UnsubscribeTarget]:
    """
    Parse List-Unsubscribe and List-Unsubscribe-Post headers per RFC 2369 / RFC 8058.

    List-Unsubscribe can contain comma-separated values in angle brackets:
      <https://example.com/unsub?token=abc>, <mailto:unsub@example.com>

    List-Unsubscribe-Post signals one-click POST support (RFC 8058):
      List-Unsubscribe-Post: List-Unsubscribe=One-Click

    Returns None if no List-Unsubscribe header is present.
    Returns an UnsubscribeTarget with available fields populated.
    """
    if not header_value:
        return None

    target = UnsubscribeTarget()

    # Extract all angle-bracket enclosed values.
    candidates = _ANGLE_BRACKET_RE.findall(header_value)
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate.startswith("https://") or candidate.startswith("http://"):
            if _is_valid_url(candidate):
                target.http_url = candidate
        elif candidate.startswith("mailto:"):
            mailto_addr = candidate[len("mailto:"):].split("?")[0].strip()
            if mailto_addr:
                target.mailto = mailto_addr

    if not target.has_any:
        # Fallback: some senders omit angle brackets (non-standard).
        parts = [p.strip() for p in header_value.split(",")]
        for part in parts:
            if part.startswith("https://") and _is_valid_url(part):
                target.http_url = part
                break
            elif part.startswith("mailto:"):
                target.mailto = part[len("mailto:"):].split("?")[0].strip()
                break

    if not target.has_any:
        logger.debug("List-Unsubscribe header present but no valid target parsed: %r", header_value)
        return None

    # RFC 8058: one-click support if List-Unsubscribe-Post is present.
    if post_header and "one-click" in post_header.lower():
        target.supports_one_click = True

    return target


def _is_valid_url(url: str) -> bool:
    """Basic URL validation — must have scheme and netloc."""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ("http", "https") and parsed.netloc)
    except Exception:
        return False