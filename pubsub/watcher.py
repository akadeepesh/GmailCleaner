"""
GmailCleaner — Gmail Watch Manager
Manages gmail.users.watch() registration for Pub/Sub push notifications.

How it works:
  - gmail.users.watch() tells Gmail to publish a Pub/Sub message to your topic
    whenever your inbox changes (new message, label change, etc.).
  - The watch registration expires after ~7 days, so it must be renewed regularly.
    Cloud Scheduler calls POST /api/watch/renew daily to keep it alive.
  - Each watch call returns a historyId — the baseline for incremental history fetches.
    We store this in the DB so the webhook handler knows where to start from.

Gmail Pub/Sub notification payload:
  {
    "emailAddress": "user@gmail.com",
    "historyId": "12345"
  }
  (delivered as base64-encoded JSON inside the Pub/Sub message envelope)

References:
  - https://developers.google.com/gmail/api/guides/push
  - https://cloud.google.com/pubsub/docs/push
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from store.db import Database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class WatchRegistration:
    history_id: str          # Baseline historyId returned by watch()
    expiration_ms: str       # Unix epoch milliseconds when the watch expires
    topic_name: str          # Full Pub/Sub topic name
    registered_at: str       # ISO timestamp of when we registered


# ──────────────────────────────────────────────────────────────────────────────
# Watch manager
# ──────────────────────────────────────────────────────────────────────────────

class WatchManager:
    """
    Manages the Gmail push notification watch for a single user.

    Usage:
        manager = WatchManager(client, db, topic_name="projects/proj/topics/gmail-cleaner")
        reg = manager.register()          # Call once at setup; repeat daily via scheduler
        manager.ensure_active()           # Idempotent: renews only if expiring soon
    """

    # Renew the watch if it expires within this many seconds.
    _RENEWAL_BUFFER_SECONDS = 86_400  # 24 hours

    # Gmail label IDs to watch. INBOX is sufficient for GmailCleaner's use case.
    _LABEL_IDS = ["INBOX"]

    def __init__(
        self,
        client: GmailClient,
        db: Database,
        topic_name: str,
    ) -> None:
        self._client = client
        self._db = db
        self._topic_name = topic_name

    def register(self) -> WatchRegistration:
        """
        Register (or re-register) a Gmail push watch.

        Calling this when a watch already exists replaces it cleanly —
        Gmail only allows one active watch per user.

        Returns:
            WatchRegistration with the new historyId and expiration.

        Raises:
            RuntimeError: If the Gmail API call fails.
        """
        logger.info("Registering Gmail watch on topic: %s", self._topic_name)
        try:
            response = self._client.watch_inbox(
                topic_name=self._topic_name,
                label_ids=self._LABEL_IDS,
            )
        except HttpError as exc:
            raise RuntimeError(
                f"Failed to register Gmail watch: {exc}\n"
                "Ensure the Pub/Sub topic exists and the Gmail service account "
                "has Pub/Sub Publisher permission on it."
            ) from exc

        reg = WatchRegistration(
            history_id=str(response["historyId"]),
            expiration_ms=str(response["expiration"]),
            topic_name=self._topic_name,
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        self._db.save_watch_registration(reg)
        logger.info(
            "Watch registered. historyId=%s, expires=%s",
            reg.history_id,
            _format_expiry(reg.expiration_ms),
        )
        return reg

    def ensure_active(self) -> WatchRegistration:
        """
        Renew the watch if it's expiring within the next 24 hours,
        or register fresh if no active watch exists.

        Idempotent — safe to call on every server startup.
        """
        existing = self._db.get_watch_registration()
        if existing:
            expiry_ms = int(existing["expiration_ms"])
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            remaining_s = (expiry_ms - now_ms) / 1000
            if remaining_s > self._RENEWAL_BUFFER_SECONDS:
                logger.info(
                    "Watch is active (expires in %.0fh). No renewal needed.",
                    remaining_s / 3600,
                )
                return WatchRegistration(
                    history_id=existing["history_id"],
                    expiration_ms=existing["expiration_ms"],
                    topic_name=existing["topic_name"],
                    registered_at=existing["registered_at"],
                )

            logger.info(
                "Watch expires in %.0fh — renewing.", remaining_s / 3600
            )

        return self.register()

    def stop(self) -> None:
        """
        Stop the Gmail push watch (unregisters the Pub/Sub subscription).
        Call this during clean shutdown or when reconfiguring.
        """
        try:
            self._client.stop_watch()
            self._db.clear_watch_registration()
            logger.info("Gmail watch stopped.")
        except HttpError as exc:
            logger.warning("Failed to stop watch (may already be expired): %s", exc)


def _format_expiry(expiration_ms: str) -> str:
    """Convert Gmail expiration epoch-milliseconds to a human-readable string."""
    try:
        ts = int(expiration_ms) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return expiration_ms