"""
GmailCleaner — Pub/Sub Webhook Handler
Processes Gmail push notifications delivered by Google Cloud Pub/Sub.

Flow:
  1. Gmail detects inbox change → publishes to Pub/Sub topic
  2. Pub/Sub push subscription delivers POST to /webhook/pubsub on this service
  3. We decode the notification, extract historyId
  4. Fetch only the messages added since the last known historyId
     (history.list returns IDs only, so metadata is fetched in a batch)
  5. Run a mini-scan: unsubscribe detection + triage on just the new messages
  6. Merge new unsubscribe candidates into pending_actions.json and update the DB
     — no digest is sent (that is the scheduled scan's job)
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
from collections import Counter
from typing import Optional

from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from gmail.parser import METADATA_HEADERS, parse_messages
from engines.unsubscribe import UnsubscribeEngine
from engines.triage import TriageEngine
from output.reporter import merge_unsubscribe_candidates
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
      - Merges new unsubscribe candidates into the pending actions report
      - Updates the stored historyId baseline

    The daily digest and full bulk-cleanup scan are still handled by the Cloud Run Job.
    """

    def __init__(
        self,
        client: GmailClient,
        db: Database,
        unsub_engine: UnsubscribeEngine,
        triage_engine: Optional[TriageEngine],
        pending_actions_path: Optional[str] = None,
    ) -> None:
        self._client = client
        self._db = db
        self._unsub_engine = unsub_engine
        self._triage_engine = triage_engine
        self._pending_actions_path = pending_actions_path

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

        # Surface new unsubscribe candidates on the dashboard immediately.
        if unsub_candidates and self._pending_actions_path:
            try:
                merge_unsubscribe_candidates(unsub_candidates, self._pending_actions_path)
            except OSError as exc:
                logger.warning("Could not merge candidates into pending actions: %s", exc)

        # Update sender records in DB (increment — the webhook sees a delta).
        per_domain = Counter(m.sender_domain for m in new_messages if m.sender_domain)
        names = {m.sender_domain: m.sender_name for m in new_messages if m.sender_name}
        for domain, count in per_domain.items():
            self._db.increment_sender(
                domain=domain,
                display_name=names.get(domain) or None,
                count=count,
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
        efficient than a full inbox scan — it returns only the delta. History
        records carry only message/thread IDs, so headers are then fetched
        in one metadata batch.
        """
        try:
            history = self._client.list_history(
                start_history_id=start_history_id,
                history_types=["messageAdded"],
                label_id="INBOX",
            )
            message_ids = list(dict.fromkeys(m["id"] for m in history if m.get("id")))
            if not message_ids:
                return []
            raw_messages = self._client.batch_get_messages(
                message_ids,
                format="metadata",
                metadata_headers=METADATA_HEADERS,
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

def verify_pubsub_token(
    authorization_header: Optional[str],
    audience: str,
    expected_email: Optional[str] = None,
) -> bool:
    """
    Verify the Google-signed OIDC JWT that Pub/Sub includes in push requests.

    Pub/Sub attaches a Bearer token signed by Google's service account.
    We verify it to ensure the request genuinely came from Pub/Sub,
    not from an external caller trying to spoof notifications.

    Args:
        authorization_header: The full "Authorization: Bearer <token>" header value.
        audience:             The expected audience (the push endpoint URL).
        expected_email:       If set, the service account the token must be issued to.

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
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
        if expected_email and (
            claims.get("email") != expected_email or not claims.get("email_verified")
        ):
            logger.warning("Webhook JWT issued to unexpected principal %s", claims.get("email"))
            return False
        return True
    except Exception as exc:
        logger.warning("Webhook JWT verification failed: %s", exc)
        return False