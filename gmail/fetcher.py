"""
GmailCleaner — Gmail Fetcher
Fetches emails from the inbox using paginated API calls, then retrieves
full metadata in batches.

Design decisions:
  - Two-phase fetch: list message IDs first (cheap), then batch-fetch metadata.
  - Only METADATA format is used (no body). Body is never fetched in Phase 1.
  - lookback_days translates to Gmail's `after:` query operator (most reliable filter).
  - Respects max_emails config to bound quota usage per run.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from gmail.client import GmailClient
from gmail.parser import GmailMessage, METADATA_HEADERS, parse_messages

logger = logging.getLogger(__name__)

# Gmail API returns max 500 message IDs per page.
_LIST_PAGE_SIZE = 500

# Batch size for metadata fetches (Google API batches max 100).
_METADATA_BATCH_SIZE = 100


class GmailFetcher:
    """
    Fetches and parses emails from the authenticated user's Gmail inbox.

    Usage:
        fetcher = GmailFetcher(client, max_emails=500, lookback_days=30)
        messages = fetcher.fetch_inbox(extra_query="in:inbox -in:spam")
    """

    def __init__(
        self,
        client: GmailClient,
        max_emails: int = 500,
        lookback_days: int = 30,
    ) -> None:
        self._client = client
        self._max_emails = max_emails
        self._lookback_days = lookback_days

    def fetch_inbox(self, extra_query: str = "in:inbox -in:trash -in:spam") -> list[GmailMessage]:
        """
        Fetch and parse up to max_emails from the inbox.

        Args:
            extra_query: Gmail search string appended to the date filter.

        Returns:
            List of parsed GmailMessage objects, ordered newest-first.
        """
        query = self._build_query(extra_query)
        logger.info("Fetching inbox (max=%d, lookback=%dd, query=%r)", self._max_emails, self._lookback_days, query)

        message_ids = self._list_all_message_ids(query)

        if not message_ids:
            logger.info("No messages found matching query.")
            return []

        logger.info("Found %d messages. Fetching metadata in batches...", len(message_ids))
        raw_messages = self._batch_fetch_metadata(message_ids)

        messages = parse_messages(raw_messages)
        logger.info("Fetched and parsed %d messages.", len(messages))
        return messages

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_query(self, base_query: str) -> str:
        """
        Append a date filter to the user's base query.

        Gmail's `after:` operator accepts YYYY/MM/DD format.
        Example: "in:inbox -in:trash -in:spam after:2025/04/16"
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._lookback_days)
        date_filter = f"after:{cutoff.strftime('%Y/%m/%d')}"
        return f"{base_query} {date_filter}".strip()

    def _list_all_message_ids(self, query: str) -> list[str]:
        """
        Paginate through users.messages.list until we have all IDs or hit max_emails.

        Returns a flat list of message ID strings.
        """
        ids: list[str] = []
        page_token: Optional[str] = None

        while len(ids) < self._max_emails:
            remaining = self._max_emails - len(ids)
            page_size = min(remaining, _LIST_PAGE_SIZE)

            response = self._client.list_messages(
                query=query,
                max_results=page_size,
                page_token=page_token,
            )

            page_messages = response.get("messages", [])
            ids.extend(msg["id"] for msg in page_messages)

            page_token = response.get("nextPageToken")
            if not page_token:
                break  # No more pages.

            logger.debug("Paginating: %d IDs collected so far...", len(ids))

        # Trim to exact max in case the last page pushed us over.
        ids = ids[: self._max_emails]
        logger.debug("Listed %d message IDs total", len(ids))
        return ids

    def _batch_fetch_metadata(self, message_ids: list[str]) -> list[dict]:
        """
        Fetch metadata for all message IDs in batches of _METADATA_BATCH_SIZE.

        Uses batch HTTP requests to reduce TCP overhead vs. individual calls.
        """
        raw_messages = []
        for chunk_start in range(0, len(message_ids), _METADATA_BATCH_SIZE):
            chunk = message_ids[chunk_start : chunk_start + _METADATA_BATCH_SIZE]
            batch_results = self._client.batch_get_messages(
                message_ids=chunk,
                format="metadata",
                metadata_headers=METADATA_HEADERS,
            )
            raw_messages.extend(batch_results)
            logger.debug(
                "Metadata batch %d–%d: fetched %d",
                chunk_start + 1,
                chunk_start + len(chunk),
                len(batch_results),
            )
        return raw_messages