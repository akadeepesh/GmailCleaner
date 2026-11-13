"""
GmailCleaner — desktop replacement for Pub/Sub push: poll for new mail.

Cloud Run gets new-mail notifications pushed by Pub/Sub. A desktop app can't
receive pushes, so every `realtime.poll_minutes` minutes (0 = off, minimum 5)
it asks Gmail for the account's current historyId and feeds it to the exact
same WebhookHandler the push endpoint uses (unsubscribe detection + triage on
newly arrived inbox mail).

  * First poll for an account (no stored historyId): store it and stop.
  * Later polls: WebhookHandler.handle(PubSubNotification(email, id, "poll")).
  * Errors back off: the wait doubles after each failure, up to 1 hour, and
    returns to normal after a success.
  * Skipped while a scan or execute job is running for that account, or while
    the CLI holds the account's lock; it retries a minute later.
"""

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.pipeline import Busy, _credentials, account_lock
from core.scheduler import ScheduledAccount, Ticker
from gmail.client import GmailClient
from pubsub.webhook import PubSubNotification, build_handler
from store.db import Database

logger = logging.getLogger(__name__)

MIN_MINUTES = 5
MAX_BACKOFF_SECONDS = 3600
BUSY_RETRY_SECONDS = 60

PollFn = Callable[[ScheduledAccount], dict]


@dataclass
class _PollState:
    next_due: float
    base: float
    failures: int = 0


def poll_account(account: ScheduledAccount) -> dict:
    """
    Check one account for new mail and process it like a push notification.

    Raises:
        AuthRequired: the account needs to sign in again.
        Busy:         a scan holds the account's lock.
    """
    cfg = account.cfg
    client = GmailClient(_credentials(cfg))
    profile = client.get_profile()
    history_id = str(profile.get("historyId") or "")
    if not history_id:
        raise RuntimeError("Gmail did not return a historyId")
    db = account.db or Database(cfg["database"]["path"])

    with account_lock(cfg):
        if not db.get_last_history_id():
            db.save_history_id(history_id)
            return {"status": "baseline_set", "history_id": history_id}
        handler = build_handler(client, db, cfg)
        try:
            return handler.handle(
                PubSubNotification(profile.get("emailAddress", ""), history_id, "poll")
            )
        finally:
            handler.close()


class Poller(Ticker):
    thread_name = "gc-poller"

    def __init__(
        self,
        *,
        accounts: Callable[[], list[ScheduledAccount]],
        is_busy: Callable[[ScheduledAccount], bool] = lambda a: False,
        paused: Callable[[], bool] = lambda: False,
        poll: PollFn = poll_account,
        interval: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval)
        self._accounts = accounts
        self._is_busy = is_busy
        self._paused = paused
        self._poll = poll
        self._clock = clock
        self._state: dict[str, _PollState] = {}

    def tick(self, now: Optional[float] = None) -> dict[str, dict]:
        """Poll every account that is due. Returns {account key: handler result}."""
        now = self._clock() if now is None else now
        results: dict[str, dict] = {}
        if self._paused():
            return results
        try:
            accounts = self._accounts()
        except Exception:
            logger.warning("Poller could not list accounts", exc_info=True)
            return results

        seen = set()
        for account in accounts:
            seen.add(account.key)
            minutes = int((account.cfg.get("realtime") or {}).get("poll_minutes") or 0)
            if minutes <= 0:
                self._state.pop(account.key, None)
                continue
            base = max(minutes, MIN_MINUTES) * 60.0
            st = self._state.get(account.key)
            if st is None or st.base != base:
                # New account, or the interval changed in Settings: poll now.
                st = self._state[account.key] = _PollState(next_due=now, base=base)
            if now < st.next_due:
                continue
            if self._is_busy(account):
                st.next_due = now + BUSY_RETRY_SECONDS
                continue
            try:
                result = self._poll(account)
            except Busy:
                st.next_due = now + BUSY_RETRY_SECONDS
                continue
            except Exception as exc:
                st.failures += 1
                delay = min(base * (2 ** st.failures), MAX_BACKOFF_SECONDS)
                st.next_due = now + delay
                logger.warning("New-mail check for %s failed (%s); next try in %d min",
                               account.label, type(exc).__name__, delay // 60,
                               exc_info=logger.isEnabledFor(logging.DEBUG))
                continue
            st.failures = 0
            st.next_due = now + base
            results[account.key] = result
            if result.get("status") == "processed":
                logger.info("New-mail check for %s: %s", account.label, result)

        for key in set(self._state) - seen:
            self._state.pop(key, None)
        return results

    def next_due(self, key: str) -> Optional[float]:
        st = self._state.get(key)
        return st.next_due if st else None
