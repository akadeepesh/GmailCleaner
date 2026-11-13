"""
GmailCleaner — background services of the desktop app.

Connects the scheduler, the new-mail poller and the daily update check to a
running dashboard (its AccountRegistry, JobRunner and AppState), so that
scheduled scans show up in the dashboard exactly like a click on "Scan my
inbox now".
"""

import logging
from typing import Callable, Optional

from flask import Flask

from core import updates
from core.scheduler import ScheduledAccount, Scheduler, scan_notification
from pubsub.poller import Poller

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str, str], None]


class Services:
    def __init__(self, app: Flask, *, notify: Optional[NotifyFn] = None, interval: float = 30.0) -> None:
        self.app = app
        self.notify_fn = notify
        self.scheduler = Scheduler(
            accounts=self.accounts,
            submit=self._submit,
            state=app.config["STATE"],
            notify=self.notify,
            extra_tasks=[self.check_updates],
            interval=interval,
        )
        self.poller = Poller(
            accounts=self.accounts,
            is_busy=self._busy,
            paused=lambda: self.scheduler.paused,
            interval=interval,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self.scheduler.start()
        self.poller.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.poller.stop()

    # ── Accounts ──────────────────────────────────────────────────────────────

    def accounts(self) -> list[ScheduledAccount]:
        """Every signed-in account, with its current (settings-aware) config."""
        reg = self.app.config["ACCOUNTS"]
        found = []
        for info in reg.list_accounts():
            if not info.signed_in:
                continue
            ctx = reg.get(info.name)
            found.append(ScheduledAccount(info.key, info.name, info.label, ctx.cfg, ctx.db))
        return found

    def _busy(self, account: ScheduledAccount) -> bool:
        return self.app.config["JOBS"].active(account.name) is not None

    def _submit(self, account: ScheduledAccount, run_trusted: bool, on_finish) -> tuple:
        from web.app import start_scan

        return start_scan(self.app, account.name, execute_trusted=run_trusted, on_finish=on_finish)

    # ── Actions ───────────────────────────────────────────────────────────────

    def scan_now(self) -> int:
        """Start a scan (suggestions only) for every signed-in account. Returns how many started."""
        accounts = self.accounts()
        if not accounts:
            self.notify("GmailCleaner", "Sign in to a Gmail account first: open GmailCleaner → Accounts.")
            return 0
        started = 0
        for account in accounts:
            def on_finish(job: dict, label=account.label) -> None:
                self.notify(*scan_notification(job, label))
            _, created = self._submit(account, False, on_finish)
            started += int(bool(created))
        if not started:
            self.notify("GmailCleaner", "A scan is already running.")
        return started

    @property
    def paused(self) -> bool:
        return self.scheduler.paused

    def set_paused(self, paused: bool) -> None:
        self.scheduler.set_paused(paused)

    def check_updates(self) -> None:
        cfg = self.app.config["ACCOUNTS"].get(None).cfg
        enabled = bool((cfg.get("updates") or {}).get("check", True))
        updates.maybe_check(self.app.config["STATE"], enabled=enabled)

    def notify(self, title: str, message: str) -> None:
        logger.info("Notification: %s", message)
        if self.notify_fn is None:
            return
        try:
            self.notify_fn(title, message)
        except Exception:
            logger.warning("Could not show a desktop notification", exc_info=True)
