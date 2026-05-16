"""
GmailCleaner — Gmail Executor
Applies confirmed actions against the Gmail API and external HTTP endpoints.

Handles three action types:
  1. HTTP unsubscribe  — POST (RFC 8058 one-click) or GET to the unsubscribe URL
  2. Mailto unsubscribe — compose and send a blank email via Gmail API
  3. Batch delete       — permanently delete message IDs in chunks of 1,000

All methods are idempotent where possible and log every outcome to the DB.
The executor never decides what to do — it only executes pre-confirmed actions.
"""

import base64
import logging
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Optional

import httpx
from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from store.db import Database

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UnsubscribeResult:
    domain: str
    method: str            # "http_post" | "http_get" | "mailto" | "failed"
    success: bool
    error: Optional[str] = None
    status_code: Optional[int] = None  # HTTP status (for HTTP method only)


@dataclass
class DeleteResult:
    group_id: str
    message_count: int
    deleted: int
    failed: int
    errors: list[str]


@dataclass
class ExecutionSummary:
    run_id: str
    unsubscribe_results: list[UnsubscribeResult]
    delete_results: list[DeleteResult]

    @property
    def total_unsubscribed(self) -> int:
        return sum(1 for r in self.unsubscribe_results if r.success)

    @property
    def total_deleted(self) -> int:
        return sum(r.deleted for r in self.delete_results)

    @property
    def total_failed(self) -> int:
        return (
            sum(1 for r in self.unsubscribe_results if not r.success)
            + sum(r.failed for r in self.delete_results)
        )


# ──────────────────────────────────────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────────────────────────────────────

class GmailExecutor:
    """
    Executes confirmed actions produced by the scan phase.

    Usage:
        executor = GmailExecutor(client, db)
        result = executor.unsubscribe(domain, http_url=..., supports_one_click=True)
        result = executor.delete_group(group_id, message_ids)
    """

    # HTTP request timeout for unsubscribe calls (seconds).
    _HTTP_TIMEOUT = 15
    # Delay between consecutive unsubscribe HTTP requests to avoid hammering servers.
    _UNSUB_DELAY = 1.0
    # Batch delete chunk size (Gmail API hard limit).
    _DELETE_CHUNK = 1000

    def __init__(self, client: GmailClient, db: Database) -> None:
        self._client = client
        self._db = db
        self._http = httpx.Client(
            timeout=self._HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                # Identify ourselves politely.
                "User-Agent": "GmailCleaner/0.2 (https://github.com/yourusername/GmailCleaner)"
            },
        )

    def __enter__(self) -> "GmailExecutor":
        return self

    def __exit__(self, *args) -> None:
        self._http.close()

    # ── Unsubscribe ───────────────────────────────────────────────────────────

    def unsubscribe(
        self,
        domain: str,
        run_id: str,
        action_id: int,
        http_url: Optional[str] = None,
        mailto: Optional[str] = None,
        supports_one_click: bool = False,
        sender_email: Optional[str] = None,
    ) -> UnsubscribeResult:
        """
        Execute an unsubscribe action using the best available method.

        Priority: one-click POST (RFC 8058) > plain HTTP GET > mailto.

        Args:
            domain:            Sender domain being unsubscribed.
            run_id:            Current run ID for DB logging.
            action_id:         DB action_log row ID to update on completion.
            http_url:          HTTP/HTTPS unsubscribe URL (if available).
            mailto:            Unsubscribe email address (if available).
            supports_one_click: True if RFC 8058 List-Unsubscribe-Post is supported.
            sender_email:      The specific sender email (used as mailto reply-to).

        Returns:
            UnsubscribeResult with success status and method used.
        """
        result: Optional[UnsubscribeResult] = None

        if http_url:
            if supports_one_click:
                result = self._unsub_http_post(domain, http_url)
            else:
                result = self._unsub_http_get(domain, http_url)

        if (result is None or not result.success) and mailto:
            result = self._unsub_mailto(domain, mailto)

        if result is None:
            result = UnsubscribeResult(
                domain=domain,
                method="failed",
                success=False,
                error="No valid unsubscribe target provided",
            )

        # Update DB.
        if result.success:
            self._db.mark_unsubscribed(domain, result.method)
            self._db.update_action_status(action_id, "executed")
            logger.info("✓ Unsubscribed from %s via %s", domain, result.method)
        else:
            self._db.update_action_status(action_id, "failed", error=result.error)
            logger.warning("✗ Unsubscribe failed for %s: %s", domain, result.error)

        # Rate-limit courtesy delay between unsubscribe calls.
        time.sleep(self._UNSUB_DELAY)
        return result

    def _unsub_http_post(self, domain: str, url: str) -> UnsubscribeResult:
        """
        RFC 8058 one-click POST unsubscribe.
        Sends: POST <url> with body: List-Unsubscribe=One-Click
        """
        try:
            resp = self._http.post(
                url,
                content=b"List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            success = resp.status_code in (200, 204, 302)
            return UnsubscribeResult(
                domain=domain,
                method="http_post",
                success=success,
                status_code=resp.status_code,
                error=None if success else f"Unexpected HTTP {resp.status_code}",
            )
        except httpx.TimeoutException:
            return UnsubscribeResult(domain=domain, method="http_post", success=False,
                                     error="Request timed out")
        except httpx.RequestError as exc:
            return UnsubscribeResult(domain=domain, method="http_post", success=False,
                                     error=str(exc))

    def _unsub_http_get(self, domain: str, url: str) -> UnsubscribeResult:
        """
        Plain HTTP GET unsubscribe (non-RFC 8058 fallback).
        Some senders require a browser-style GET to complete unsubscription.
        """
        try:
            resp = self._http.get(url)
            success = resp.status_code in (200, 204, 302)
            return UnsubscribeResult(
                domain=domain,
                method="http_get",
                success=success,
                status_code=resp.status_code,
                error=None if success else f"Unexpected HTTP {resp.status_code}",
            )
        except httpx.TimeoutException:
            return UnsubscribeResult(domain=domain, method="http_get", success=False,
                                     error="Request timed out")
        except httpx.RequestError as exc:
            return UnsubscribeResult(domain=domain, method="http_get", success=False,
                                     error=str(exc))

    def _unsub_mailto(self, domain: str, mailto_addr: str) -> UnsubscribeResult:
        """
        Send a blank unsubscribe email via the Gmail API.

        RFC 2369 says to send a blank email (or with subject "unsubscribe")
        to the List-Unsubscribe mailto address.
        """
        try:
            mime = MIMEText("")
            mime["To"] = mailto_addr
            mime["Subject"] = "unsubscribe"
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            self._client.send_message(raw)
            return UnsubscribeResult(domain=domain, method="mailto", success=True)
        except HttpError as exc:
            return UnsubscribeResult(domain=domain, method="mailto", success=False,
                                     error=str(exc))
        except Exception as exc:
            return UnsubscribeResult(domain=domain, method="mailto", success=False,
                                     error=str(exc))

    # ── Batch delete ──────────────────────────────────────────────────────────

    def delete_group(
        self,
        group_id: str,
        message_ids: list[str],
        run_id: str,
        action_id: int,
    ) -> DeleteResult:
        """
        Permanently delete all messages in a confirmed cleanup group.

        Splits the message_ids list into chunks of _DELETE_CHUNK (1,000)
        since Gmail's batchDelete endpoint supports max 1,000 IDs per call.

        NOTE: batchDelete is permanent — messages bypass Trash.
        The caller must have confirmed the action before invoking this.

        Args:
            group_id:     Identifier for this cleanup group (for logging).
            message_ids:  All message IDs to delete.
            run_id:       Current run ID.
            action_id:    DB action_log row ID to update on completion.

        Returns:
            DeleteResult with counts of deleted and failed messages.
        """
        deleted = 0
        errors: list[str] = []

        chunks = [
            message_ids[i: i + self._DELETE_CHUNK]
            for i in range(0, len(message_ids), self._DELETE_CHUNK)
        ]
        total_chunks = len(chunks)

        for chunk_index, chunk in enumerate(chunks):
            try:
                self._client.batch_delete(chunk)
                deleted += len(chunk)
                logger.debug(
                    "Delete chunk %d/%d: %d messages removed from group %s",
                    chunk_index + 1, total_chunks, len(chunk), group_id,
                )
            except HttpError as exc:
                error_msg = f"Chunk {chunk_index + 1}/{total_chunks}: {exc}"
                errors.append(error_msg)
                logger.error("Delete failed for group %s — %s", group_id, error_msg)

        failed = len(message_ids) - deleted
        result = DeleteResult(
            group_id=group_id,
            message_count=len(message_ids),
            deleted=deleted,
            failed=failed,
            errors=errors,
        )

        if failed == 0:
            self._db.update_action_status(action_id, "executed")
            logger.info("✓ Deleted %d messages from group %s", deleted, group_id)
        elif deleted > 0:
            self._db.update_action_status(
                action_id, "failed",
                error=f"Partial: {deleted} deleted, {failed} failed"
            )
            logger.warning(
                "⚠ Partial delete for group %s: %d/%d succeeded",
                group_id, deleted, len(message_ids),
            )
        else:
            self._db.update_action_status(action_id, "failed", error="; ".join(errors))
            logger.error("✗ Delete completely failed for group %s", group_id)

        return result