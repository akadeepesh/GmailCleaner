"""
GmailCleaner — Gmail API Client
Wraps the Google API Python client to provide a typed, testable interface.

All Gmail API calls go through this module. Raw googleapiclient objects
are never exposed outside gmail/.
"""

import logging
from typing import Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


class GmailClient:
    """
    Authenticated Gmail API client.

    Thin wrapper around the Google API Python client that:
      - Provides typed method signatures
      - Centralises error logging and re-raising
      - Makes the service object injectable for testing

    Usage:
        creds = get_credentials(...)
        client = GmailClient(creds)
        messages = client.list_messages(query="in:inbox", max_results=100)
    """

    def __init__(self, credentials: Credentials) -> None:
        self._service = build(
            "gmail",
            "v1",
            credentials=credentials,
            # Disable the file-based discovery cache; not needed for production.
            cache_discovery=False,
        )
        self._user_id = "me"  # Gmail API convention: "me" = authenticated user
        logger.debug("Gmail API client initialised")

    # ── Message listing ───────────────────────────────────────────────────────

    def list_messages(
        self,
        query: str,
        max_results: int = 500,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Call users.messages.list and return the raw API response.

        Args:
            query:       Gmail search query string (e.g. "in:inbox after:2025/01/01")
            max_results: Maximum messages to return in this page (1–500).
            page_token:  Pagination token from a previous list_messages call.

        Returns:
            Raw API response dict containing 'messages', 'nextPageToken',
            and 'resultSizeEstimate'.
        """
        try:
            response = (
                self._service.users()
                .messages()
                .list(
                    userId=self._user_id,
                    q=query,
                    maxResults=min(max_results, 500),  # API hard limit
                    pageToken=page_token,
                )
                .execute()
            )
            logger.debug(
                "list_messages: fetched page with %d messages (nextPageToken=%s)",
                len(response.get("messages", [])),
                bool(response.get("nextPageToken")),
            )
            return response
        except HttpError as exc:
            logger.error("Gmail API error on list_messages (query=%r): %s", query, exc)
            raise

    # ── Message fetching ──────────────────────────────────────────────────────

    def get_message(
        self,
        message_id: str,
        format: str = "metadata",
        metadata_headers: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Fetch a single message by ID.

        Args:
            message_id:       Gmail message ID.
            format:           'metadata' (headers only) | 'full' (includes body) | 'minimal' | 'raw'
            metadata_headers: When format='metadata', restrict to these header names only.
                              Reduces response size significantly.

        Returns:
            Raw API message resource.
        """
        kwargs: dict[str, Any] = {
            "userId": self._user_id,
            "id": message_id,
            "format": format,
        }
        if format == "metadata" and metadata_headers:
            kwargs["metadataHeaders"] = metadata_headers

        try:
            message = self._service.users().messages().get(**kwargs).execute()
            return message
        except HttpError as exc:
            logger.error("Gmail API error on get_message (id=%s): %s", message_id, exc)
            raise

    def batch_get_messages(
        self,
        message_ids: list[str],
        format: str = "metadata",
        metadata_headers: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch multiple messages using the Google API batch HTTP mechanism,
        with exponential backoff retry on 429 rate-limit responses.

        Batch size is kept at 25 (well below the API max of 100) to avoid
        triggering the "Too many concurrent requests" per-user rate limit.
        A short sleep between batches gives the API quota time to recover.

        Args:
            message_ids:      List of Gmail message IDs to fetch.
            format:           Response format ('metadata' or 'full').
            metadata_headers: Headers to include when format='metadata'.

        Returns:
            List of raw API message resources in the same order as message_ids.
        """
        import time

        results: dict[str, Any] = {}

        # Reduced from 100 to 25 to stay within Gmail's per-user concurrency limit.
        BATCH_SIZE = 25
        # Polite pause between successful batches.
        INTER_BATCH_DELAY = 0.5
        # Retry config for 429 responses.
        MAX_RETRIES = 4
        BASE_BACKOFF = 2.0  # seconds; doubles each attempt

        total_chunks = (len(message_ids) + BATCH_SIZE - 1) // BATCH_SIZE

        for chunk_index, chunk_start in enumerate(range(0, len(message_ids), BATCH_SIZE)):
            chunk = message_ids[chunk_start: chunk_start + BATCH_SIZE]
            failed_ids: list[str] = list(chunk)

            for attempt in range(1, MAX_RETRIES + 1):
                if not failed_ids:
                    break

                attempt_results: dict[str, Any] = {}
                attempt_errors: list[str] = []

                def _make_callback(
                    _attempt_results: dict, _attempt_errors: list
                ):
                    def _callback(request_id: str, response: Any, exception: Any) -> None:
                        if exception:
                            if (
                                isinstance(exception, HttpError)
                                and exception.resp.status == 429
                            ):
                                _attempt_errors.append(request_id)
                            else:
                                logger.warning(
                                    "Batch get non-retriable error for %s: %s",
                                    request_id, exception,
                                )
                        else:
                            _attempt_results[request_id] = response
                    return _callback

                batch = self._service.new_batch_http_request(
                    callback=_make_callback(attempt_results, attempt_errors)
                )
                for msg_id in failed_ids:
                    kwargs: dict[str, Any] = {
                        "userId": self._user_id,
                        "id": msg_id,
                        "format": format,
                    }
                    if format == "metadata" and metadata_headers:
                        kwargs["metadataHeaders"] = metadata_headers
                    batch.add(
                        self._service.users().messages().get(**kwargs),
                        request_id=msg_id,
                    )

                batch.execute()
                results.update(attempt_results)

                if attempt_errors:
                    backoff = BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "Rate limited on %d message(s) in batch %d/%d "
                        "(attempt %d/%d). Retrying in %.1fs...",
                        len(attempt_errors), chunk_index + 1, total_chunks,
                        attempt, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    failed_ids = attempt_errors
                else:
                    failed_ids = []

            if failed_ids:
                logger.error(
                    "Giving up on %d message(s) after %d attempts: %s",
                    len(failed_ids), MAX_RETRIES, failed_ids,
                )

            logger.debug(
                "Batch %d/%d complete: %d total fetched so far",
                chunk_index + 1, total_chunks, len(results),
            )

            if chunk_start + BATCH_SIZE < len(message_ids):
                time.sleep(INTER_BATCH_DELAY)

        return [results[mid] for mid in message_ids if mid in results]

    # ── Message mutation ──────────────────────────────────────────────────────

    def batch_trash(self, message_ids: list[str]) -> None:
        """
        Move messages to Trash using batchModify. Supports up to 1,000 IDs per call.

        Trash is recoverable for 30 days, after which Gmail purges it automatically.
        This only needs the gmail.modify scope (users.messages.batchDelete would
        require full https://mail.google.com/ access).
        """
        if not message_ids:
            return
        if len(message_ids) > 1000:
            raise ValueError(
                f"Gmail API batchModify supports max 1,000 IDs per call. "
                f"Got {len(message_ids)}. Split into chunks before calling."
            )
        try:
            self._service.users().messages().batchModify(
                userId=self._user_id,
                body={
                    "ids": message_ids,
                    "addLabelIds": ["TRASH"],
                    "removeLabelIds": ["INBOX"],
                },
            ).execute()
            logger.info("batchModify: moved %d messages to Trash", len(message_ids))
        except HttpError as exc:
            logger.error("Gmail API error on batchModify (trash): %s", exc)
            raise

    def trash_message(self, message_id: str) -> None:
        """Move a single message to Trash (recoverable). Safer than batchDelete."""
        try:
            self._service.users().messages().trash(
                userId=self._user_id,
                id=message_id,
            ).execute()
        except HttpError as exc:
            logger.error("Gmail API error on trash (id=%s): %s", message_id, exc)
            raise

    def send_message(self, raw_message: str) -> dict[str, Any]:
        """
        Send a pre-composed RFC 2822 email message.

        Args:
            raw_message: Base64url-encoded RFC 2822 message string.

        Returns:
            API response containing the sent message ID.
        """
        try:
            return (
                self._service.users()
                .messages()
                .send(userId=self._user_id, body={"raw": raw_message})
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail API error on send_message: %s", exc)
            raise

    # ── Profile ───────────────────────────────────────────────────────────────

    def get_profile(self) -> dict[str, Any]:
        """Return the authenticated user's Gmail profile (email address, quota, etc.)."""
        try:
            return self._service.users().getProfile(userId=self._user_id).execute()
        except HttpError as exc:
            logger.error("Gmail API error on getProfile: %s", exc)
            raise

    # ── Push notifications ────────────────────────────────────────────────────

    def watch_inbox(
        self,
        topic_name: str,
        label_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Register a Gmail push watch on a Pub/Sub topic.

        Gmail will publish a notification to the topic whenever a message
        matching the label filter arrives. Returns a response containing
        historyId (baseline) and expiration (epoch ms, ~7 days from now).

        Args:
            topic_name: Full Pub/Sub topic name, e.g.
                        "projects/my-project/topics/gmail-cleaner"
            label_ids:  Gmail label IDs to watch. Defaults to ["INBOX"].

        Returns:
            {"historyId": "...", "expiration": "..."}
        """
        body: dict[str, Any] = {
            "topicName": topic_name,
            "labelIds": label_ids or ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
        try:
            return self._service.users().watch(
                userId=self._user_id, body=body
            ).execute()
        except HttpError as exc:
            logger.error("Gmail API error on watch(): %s", exc)
            raise

    def stop_watch(self) -> None:
        """Stop the Gmail push watch (unsubscribes from Pub/Sub notifications)."""
        try:
            self._service.users().stop(userId=self._user_id).execute()
        except HttpError as exc:
            logger.error("Gmail API error on stop(): %s", exc)
            raise

    def list_history(
        self,
        start_history_id: str,
        history_types: Optional[list[str]] = None,
        label_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch inbox history changes since a given historyId.

        Uses users.history.list to return only the delta since the baseline —
        far more efficient than a full inbox scan for real-time processing.

        Args:
            start_history_id: The historyId to start from (exclusive).
                              Use the value returned by watch_inbox() as the baseline.
            history_types:    Types of history to return. Defaults to ["messageAdded"].
            label_id:         Filter to changes affecting a specific label.

        Returns:
            List of raw message resource dicts for messages added since the historyId.
            Returns an empty list if there are no changes.
        """
        messages: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        h_types = history_types or ["messageAdded"]

        while True:
            kwargs: dict[str, Any] = {
                "userId": self._user_id,
                "startHistoryId": start_history_id,
                "historyTypes": h_types,
            }
            if label_id:
                kwargs["labelId"] = label_id
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                response = self._service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                logger.error("Gmail API error on history.list: %s", exc)
                raise

            for history_record in response.get("history", []):
                for added in history_record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if msg:
                        messages.append(msg)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        logger.debug("list_history: %d message(s) since historyId=%s", len(messages), start_history_id)
        return messages