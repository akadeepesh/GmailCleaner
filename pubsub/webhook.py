"""
GmailCleaner — Pub/Sub Webhook Handler
Processes Gmail push notifications delivered by Google Cloud Pub/Sub.

Flow:
  1. Gmail detects inbox change → publishes to Pub/Sub topic
  2. Pub/Sub push subscription delivers POST to /webhook/pubsub on this service
  3. We decode the notification, extract historyId
  4. Fetch only the changed messages since the last known historyId
  5. Run a mini-scan: unsubscribe detection + triage on just the new messages
  6. Update pending_actions and DB — no digest sent (that's the scheduled job's job)
  7. Return HTTP 200 to acknowledge the Pub/Sub message
     (non-200 → Pub/Sub retries with exponential backoff, up to the subscription deadline)

Pub/Sub message envelope:
  {
    "message": {
      "data": "<base64-encoded JSON>",   ← {"emailAddress": "...", "historyId": "..."}
      "messageId": "...",
      "publishTime": "..."
    },
    "subscription": "projects/.../subscriptions/..."
  }

Security:
  - Pub/Sub push requests include a signed JWT in the Authorization header.
  - We verify this token against Google's public keys before processing.
  - Unauthenticated requests are rejected with 401.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from gmail.parser import parse_messages
from engines.unsubscribe import UnsubscribeEngine
from engines.triage import TriageEngine
from store.db import Database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Pub/Sub notification decoder
# ──────────────────────────────────────────────────────────────────────────────

class PubSubNotification:
    """Decoded Gmail Pub/Sub push notification."""

    def __init__(self, email_address: str, history_id: str, raw_message_id: str) -> None:
        self.email_address = email_address
        self.history_id = history_id
        self.raw_message_id = raw_message_id  # Pub/Sub message ID (for dedup)

    @classmethod
    def from_request_body(cls, body: dict) -> "PubSubNotification":
        """
        Parse a Pub/Sub push HTTP request body into a PubSubNotification.

        Raises:
            ValueError: If the body is malformed or missing required fields.
        """
        try:
            message = body["message"]
            raw_message_id = message.get("messageId", "")
            data_b64 = message.get("data", "")
            # Pub/Sub data is base64-encoded; add padding if needed.
            padding = 4 - len(data_b64) % 4
            if padding != 4:
                data_b64 += "=" * padding
            data = json.loads(base64.b64decode(data_b64).decode("utf-8"))
            return cls(
                email_address=data["emailAddress"],
                history_id=str(data["historyId"]),
                raw_message_id=raw_message_id,
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed Pub/Sub notification: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

class WebhookHandler:
    """
    Processes Gmail Pub/Sub notifications and runs a mini-scan on changed messages.

    This is intentionally lightweight: it does NOT run the full scan pipeline.
    It only:
      - Fetches messages changed since the last known historyId
      - Runs unsubscribe detection on new messages
      - Runs triage on new messages (using cached Gemini results where possible)
      - Updates the DB with new pending actions
      - Updates the stored historyId baseline

    The daily digest and full bulk-cleanup scan are still handled by the Cloud Run Job.
    """

    def __init__(
        self,
        client: GmailClient,
        db: Database,
        unsub_engine: UnsubscribeEngine,
        triage_engine: Optional[TriageEngine],
    ) -> None:
        self._client = client
        self._db = db
        self._unsub_engine = unsub_engine
        self._triage_engine = triage_engine

    def handle(self, notification: PubSubNotification) -> dict:
        """
        Process a single Pub/Sub notification.

        Args:
            notification: Decoded Pub/Sub message from Gmail.

        Returns:
            A result dict summarising what was found and processed.
        """
        logger.info(
            "Webhook: received notification for %s (historyId=%s)",
            notification.email_address, notification.history_id,
        )

        # Get our last known historyId from DB.
        last_history_id = self._db.get_last_history_id()

        if not last_history_id:
            logger.warning(
                "No baseline historyId in DB — storing current and skipping this notification. "
                "This is normal on first webhook call after watch registration."
            )
            self._db.save_history_id(notification.history_id)
            return {"status": "baseline_set", "history_id": notification.history_id}

        # If the incoming historyId is not newer than our baseline, skip.
        if int(notification.history_id) <= int(last_history_id):
            logger.info(
                "Notification historyId %s <= stored %s — already processed, skipping.",
                notification.history_id, last_history_id,
            )
            return {"status": "already_processed"}

        # Fetch history changes since our last known point.
        new_messages = self._fetch_history(last_history_id)
        if not new_messages:
            logger.info("No new messages in history since historyId=%s", last_history_id)
            self._db.save_history_id(notification.history_id)
            return {"status": "no_new_messages"}

        logger.info("Webhook: %d new message(s) to process", len(new_messages))

        # Run unsubscribe detection on new messages only.
        unsub_candidates = self._unsub_engine.get_new_candidates(new_messages)

        # Run triage on new messages (skips any already cached in DB).
        triage_summary = None
        if self._triage_engine:
            triage_summary = self._triage_engine.classify(new_messages)

        # Update sender records in DB.
        for msg in new_messages:
            if msg.sender_domain:
                self._db.upsert_sender(
                    domain=msg.sender_domain,
                    display_name=msg.sender_name or None,
                    email_count=1,
                )

        # Update DB inbox stats for the dashboard.
        self._db.record_inbox_event(
            new_count=len(new_messages),
            needs_reply_count=len(triage_summary.needs_reply) if triage_summary else 0,
            promotions_count=len(triage_summary.promotions) if triage_summary else 0,
            junk_count=len(triage_summary.junk) if triage_summary else 0,
            unsub_count=len(unsub_candidates),
        )

        # Advance the historyId baseline.
        self._db.save_history_id(notification.history_id)

        result = {
            "status": "processed",
            "new_messages": len(new_messages),
            "unsub_candidates": len(unsub_candidates),
            "needs_reply": len(triage_summary.needs_reply) if triage_summary else 0,
        }
        logger.info("Webhook processing complete: %s", result)
        return result

    def _fetch_history(self, start_history_id: str) -> list:
        """
        Fetch messages added to the inbox since the given historyId.

        Uses the Gmail History API (users.history.list) which is much more
        efficient than a full inbox scan — it returns only the delta.
        """
        try:
            raw_messages = self._client.list_history(
                start_history_id=start_history_id,
                history_types=["messageAdded"],
                label_id="INBOX",
            )
            return parse_messages(raw_messages)
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId too old (Gmail only retains ~30 days of history).
                logger.warning(
                    "historyId %s is too old for Gmail history API — "
                    "falling back to full scan is not done in webhook mode. "
                    "The next scheduled full scan will catch up.",
                    start_history_id,
                )
                return []
            raise


# ──────────────────────────────────────────────────────────────────────────────
# JWT verification
# ──────────────────────────────────────────────────────────────────────────────

def verify_pubsub_token(authorization_header: Optional[str], audience: str) -> bool:
    """
    Verify the Google-signed OIDC JWT that Pub/Sub includes in push requests.

    Pub/Sub attaches a Bearer token signed by Google's service account.
    We verify it to ensure the request genuinely came from Pub/Sub,
    not from an external caller trying to spoof notifications.

    Args:
        authorization_header: The full "Authorization: Bearer <token>" header value.
        audience:             The expected audience (your Cloud Run service URL).

    Returns:
        True if the token is valid, False otherwise.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        logger.warning("Webhook: missing or malformed Authorization header")
        return False

    token = authorization_header.split(" ", 1)[1]

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
        return True
    except Exception as exc:
        logger.warning("Webhook JWT verification failed: %s", exc)
        return False