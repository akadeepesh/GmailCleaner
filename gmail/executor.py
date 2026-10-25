"""
GmailCleaner — Gmail Executor
Applies confirmed actions against the Gmail API and external HTTP endpoints.

Handles three action types:
  1. HTTP unsubscribe  — POST (RFC 8058 one-click) or GET to the unsubscribe URL
  2. Mailto unsubscribe — compose and send a blank email via Gmail API
  3. Bulk cleanup       — move message IDs to Trash in chunks of 1,000
                          (recoverable for 30 days, then purged by Gmail)

All methods are idempotent where possible and log every outcome to the DB.
The executor never decides what to do — it only executes pre-confirmed actions.
"""

import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, Callable, Optional

import httpx
from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from store.db import Database
from version import __version__

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
    """Outcome of a cleanup group. `deleted` counts messages moved to Trash."""
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
    Executes confirmed actions produced by a scan.

    Usage:
        executor = GmailExecutor(client, db)
        result = executor.unsubscribe(domain, http_url=..., supports_one_click=True)
        result = executor.delete_group(group_id, message_ids)
    """

    # HTTP request timeout for unsubscribe calls (seconds).
    _HTTP_TIMEOUT = 15
    # Delay between consecutive unsubscribe HTTP requests to avoid hammering servers.
    _UNSUB_DELAY = 1.0
    # Batch trash chunk size (Gmail API batchModify hard limit).
    _DELETE_CHUNK = 1000

    def __init__(self, client: GmailClient, db: Database) -> None:
        self._client = client
        self._db = db
        self._http = httpx.Client(
            timeout=self._HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                # Identify ourselves politely.
                "User-Agent": f"GmailCleaner/{__version__} (https://github.com/akadeepesh/GmailCleaner)"
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
            success = resp.status_code in (200, 202, 204, 302)
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
            success = resp.status_code in (200, 202, 204, 302)
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

    # ── Bulk cleanup ──────────────────────────────────────────────────────────

    def delete_group(
        self,
        group_id: str,
        message_ids: list[str],
        run_id: str,
        action_id: int,
    ) -> DeleteResult:
        """
        Move all messages in a confirmed cleanup group to Trash.

        Splits the message_ids list into chunks of _DELETE_CHUNK (1,000)
        since Gmail's batchModify endpoint supports max 1,000 IDs per call.

        Trashed messages stay recoverable for 30 days. The caller must have
        confirmed the action before invoking this.

        Args:
            group_id:     Identifier for this cleanup group (for logging).
            message_ids:  All message IDs to trash.
            run_id:       Current run ID.
            action_id:    DB action_log row ID to update on completion.

        Returns:
            DeleteResult with counts of trashed and failed messages.
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
                self._client.batch_trash(chunk)
                deleted += len(chunk)
                logger.debug(
                    "Trash chunk %d/%d: %d messages moved from group %s",
                    chunk_index + 1, total_chunks, len(chunk), group_id,
                )
            except HttpError as exc:
                error_msg = f"Chunk {chunk_index + 1}/{total_chunks}: {exc}"
                errors.append(error_msg)
                logger.error("Cleanup failed for group %s — %s", group_id, error_msg)

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
            logger.info("✓ Moved %d messages to Trash from group %s", deleted, group_id)
        elif deleted > 0:
            self._db.update_action_status(
                action_id, "failed",
                error=f"Partial: {deleted} trashed, {failed} failed"
            )
            logger.warning(
                "⚠ Partial cleanup for group %s: %d/%d succeeded",
                group_id, deleted, len(message_ids),
            )
        else:
            self._db.update_action_status(action_id, "failed", error="; ".join(errors))
            logger.error("✗ Cleanup completely failed for group %s", group_id)

        return result


# ──────────────────────────────────────────────────────────────────────────────
# Report execution (shared by the CLI, the dashboard and trusted rules)
# ──────────────────────────────────────────────────────────────────────────────

# Called after each action with (action_type, report_item, result).
ResultCallback = Callable[[str, dict, Any], None]


def confirmed_items(report: dict) -> tuple[list[dict], list[dict]]:
    """Return (unsubscribe, delete_groups) items that are confirmed and not yet executed."""
    unsub = [
        item for item in report.get("unsubscribe", [])
        if item.get("confirmed") is True and not item.get("executed_at")
    ]
    delete = [
        item for item in report.get("delete_groups", [])
        if item.get("confirmed") is True and not item.get("executed_at")
    ]
    return unsub, delete


def execute_report(
    executor: GmailExecutor,
    db: Database,
    report: dict,
    on_result: Optional[ResultCallback] = None,
) -> ExecutionSummary:
    """
    Execute every confirmed, not-yet-executed item in a pending actions report.

    Successful items are stamped with `executed_at` in the report dict so a
    repeated execute never re-runs them; the caller persists the report.
    """
    run_id = report["run_id"]
    unsub_items, delete_items = confirmed_items(report)
    unsub_results: list[UnsubscribeResult] = []
    delete_results: list[DeleteResult] = []

    for item in unsub_items:
        action_id = find_or_create_action(db, run_id, "unsubscribe", item["domain"])
        result = executor.unsubscribe(
            domain=item["domain"],
            run_id=run_id,
            action_id=action_id,
            http_url=item.get("http_url"),
            mailto=item.get("mailto"),
            supports_one_click=item.get("supports_one_click", False),
        )
        unsub_results.append(result)
        if result.success:
            item["executed_at"] = _utcnow()
        if on_result:
            on_result("unsubscribe", item, result)

    for item in delete_items:
        action_id = find_or_create_action(db, run_id, "delete", item["group_id"])
        result = executor.delete_group(
            group_id=item["group_id"],
            message_ids=item.get("message_ids", []),
            run_id=run_id,
            action_id=action_id,
        )
        delete_results.append(result)
        if result.failed == 0:
            item["executed_at"] = _utcnow()
        if on_result:
            on_result("delete", item, result)

    summary = ExecutionSummary(
        run_id=run_id,
        unsubscribe_results=unsub_results,
        delete_results=delete_results,
    )
    _record_execute_run(db, run_id, summary)
    return summary


def find_or_create_action(db: Database, run_id: str, action_type: str, target: str) -> int:
    """
    Find the pending action_log row for this run+type+target, or create a
    new 'confirmed' row (e.g. when re-running execute after a failure).
    """
    for action in db.get_pending_actions(run_id):
        if action.action_type == action_type and action.target == target:
            return action.id
    return db.log_action(
        run_id=run_id,
        action_type=action_type,
        target=target,
        status="confirmed",
    )


def _record_execute_run(db: Database, run_id: str, summary: ExecutionSummary) -> None:
    """Log the execute pass as its own row in the runs table."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    execute_run_id = f"{run_id}-execute-{stamp}"
    db.create_run(execute_run_id, mode="execute")
    db.complete_run(
        run_id=execute_run_id,
        emails_scanned=0,
        actions_pending=0,
        actions_executed=summary.total_unsubscribed + summary.total_deleted,
        actions_failed=summary.total_failed,
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
