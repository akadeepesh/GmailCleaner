"""
GmailCleaner — automatic scans for the desktop app.

The scheduler is a thread inside the app (no OS task scheduler, no admin
rights). Every 30 seconds it checks each signed-in account and, when a
scheduled time has passed since that account's last scheduled scan, submits a
scan through the JobRunner. Settings come from the `schedule` block:

    schedule:
      enabled: false
      time: "08:00"            local time
      days: [mon, ..., sun]
      run_trusted_rules: true  scan --no-dry-run (acts only when confirm_first is false)
      catch_up: true

Rules:
  * A scheduled time that passes while the app is running always runs once.
  * A time missed while the app was closed runs once on the next launch, but
    only with catch_up on and only if it is less than 24 hours old.
  * Turning the schedule on, or resuming after "Pause automatic scans",
    starts counting from that moment: times missed before don't run.
  * Two scans never run for one account at once: the JobRunner refuses a
    second job and the per-account file lock stops a parallel CLI scan.

When a scheduled scan finishes, notify(title, message) is called (the tray
shows it as a desktop notification).
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Callable, Iterable, Optional

from core.state import AppState

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CATCH_UP_WINDOW = timedelta(hours=24)
LAST_RUN_KEY = "last_scheduled_run"
PAUSED_KEY = "scheduler_paused"

NotifyFn = Callable[[str, str], None]


# ──────────────────────────────────────────────────────────────────────────────
# Background thread helper
# ──────────────────────────────────────────────────────────────────────────────

class Ticker:
    """A daemon thread that calls tick() right away and then every `interval` seconds."""

    thread_name = "gc-ticker"

    def __init__(self, interval: float = 30.0) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.thread_name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.error("%s tick failed", self.thread_name, exc_info=True)
            self._stop.wait(self.interval)


# ──────────────────────────────────────────────────────────────────────────────
# Slot arithmetic
# ──────────────────────────────────────────────────────────────────────────────

def parse_hhmm(value: str) -> time:
    hours, minutes = str(value).strip().split(":")
    return time(int(hours), int(minutes))


def most_recent_slot(now: datetime, at: str, days: Iterable[str]) -> Optional[datetime]:
    """
    The latest scheduled moment at or before now, in local time (aware).

    Args:
        now:  An aware datetime.
        at:   "HH:MM" local time.
        days: Day names (mon..sun) the schedule runs on.

    Returns None when no day is selected or the time can't be parsed.
    """
    try:
        slot_time = parse_hhmm(at)
    except (ValueError, TypeError):
        logger.warning("Ignoring schedule with invalid time %r", at)
        return None
    wanted = {str(d).lower()[:3] for d in days or ()}
    local_now = now.astimezone()
    for back in range(8):
        day = (local_now - timedelta(days=back)).date()
        if DAYS[day.weekday()] not in wanted:
            continue
        # A naive datetime's astimezone() reads it as local time (DST aware).
        candidate = datetime.combine(day, slot_time).astimezone()
        if candidate <= local_now:
            return candidate
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScheduledAccount:
    key: str                 # stable id for state (the dashboard's account key)
    name: Optional[str]      # account name passed to submit (None = unnamed account)
    label: str               # shown in notifications (the email address)
    cfg: dict
    db: Any = None           # the account's open Database, if the caller has one


# submit(account, run_trusted_rules, on_finish) -> (job_id, created)
SubmitFn = Callable[[ScheduledAccount, bool, Callable[[dict], None]], tuple]


class Scheduler(Ticker):
    thread_name = "gc-scheduler"

    def __init__(
        self,
        *,
        accounts: Callable[[], list[ScheduledAccount]],
        submit: SubmitFn,
        state: AppState,
        notify: Optional[NotifyFn] = None,
        extra_tasks: Iterable[Callable[[], None]] = (),
        interval: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        super().__init__(interval)
        self._accounts = accounts
        self._submit = submit
        self._state = state
        self._notify = notify
        self._extra_tasks = list(extra_tasks)
        self._clock = clock
        self._started_at = clock()
        self._active: dict[str, bool] = {}

    # ── Pause ─────────────────────────────────────────────────────────────────

    @property
    def paused(self) -> bool:
        return bool(self._state.get(PAUSED_KEY, False))

    def set_paused(self, paused: bool) -> None:
        self._state.set(PAUSED_KEY, bool(paused))
        logger.info("Automatic scans %s", "paused" if paused else "resumed")

    # ── Tick ──────────────────────────────────────────────────────────────────

    def tick(self, now: Optional[datetime] = None) -> list[str]:
        """Check every account once. Returns the keys of accounts a scan was submitted for."""
        now = now or self._clock()
        submitted: list[str] = []
        paused = self.paused
        try:
            accounts = self._accounts()
        except Exception:
            logger.warning("Scheduler could not list accounts", exc_info=True)
            accounts = []
        for account in accounts:
            if self._check(account, now, paused):
                submitted.append(account.key)
        for task in self._extra_tasks:
            try:
                task()
            except Exception:
                logger.warning("Scheduler task %r failed", task, exc_info=True)
        return submitted

    def _check(self, account: ScheduledAccount, now: datetime, paused: bool) -> bool:
        sched = account.cfg.get("schedule") or {}
        active = bool(sched.get("enabled")) and not paused
        was_active = self._active.get(account.key)
        self._active[account.key] = active
        if not active:
            return False

        last = self._last_run(account.key)
        if was_active is False or last is None:
            # Just switched on / resumed, or never ran: start counting from now.
            self._set_last_run(account.key, now)
            return False

        slot = most_recent_slot(now, sched.get("time", "08:00"), sched.get("days", DAYS))
        if slot is None or slot <= last:
            return False
        if slot < self._started_at:
            # Missed while the app was closed.
            if not sched.get("catch_up", True) or now - slot > CATCH_UP_WINDOW:
                return False
            logger.info("Catching up on the missed %s scan for %s", slot.isoformat(), account.label)

        self._set_last_run(account.key, now)
        run_trusted = bool(sched.get("run_trusted_rules", True))
        try:
            job_id, created = self._submit(account, run_trusted, self._finished_callback(account))
        except Exception:
            logger.error("Could not start the scheduled scan for %s", account.label, exc_info=True)
            return False
        if created:
            logger.info("Started scheduled scan %s for %s", job_id, account.label)
        else:
            logger.info("Skipped scheduled scan for %s: a scan is already running", account.label)
        return bool(created)

    # ── Notifications ─────────────────────────────────────────────────────────

    def _finished_callback(self, account: ScheduledAccount) -> Callable[[dict], None]:
        def on_finish(job: dict) -> None:
            if self._notify is None:
                return
            title, message = scan_notification(job, account.label)
            self._notify(title, message)
        return on_finish

    # ── State ─────────────────────────────────────────────────────────────────

    def _last_run(self, key: str) -> Optional[datetime]:
        raw = (self._state.get(LAST_RUN_KEY) or {}).get(key)
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        return value if value.tzinfo else value.astimezone()

    def _set_last_run(self, key: str, when: datetime) -> None:
        def apply(data: dict) -> None:
            runs = data.get(LAST_RUN_KEY)
            if not isinstance(runs, dict):
                runs = data[LAST_RUN_KEY] = {}
            runs[key] = when.isoformat(timespec="seconds")
        self._state.update(apply)


def scan_notification(job: dict, label: str) -> tuple[str, str]:
    """(title, message) for a finished scheduled scan job."""
    if job.get("status") != "done":
        return ("GmailCleaner", f"The automatic scan for {label} didn't finish: "
                                f"{job.get('error') or 'unknown error'}")
    result = job.get("result") or {}
    found = int(result.get("unsubscribe_count") or 0) + int(result.get("cleanup_count") or 0)
    executed = int(result.get("executed") or 0)
    parts = []
    if found:
        parts.append(f"found {found} suggestion{'s' if found != 1 else ''} for {label}. "
                     "Open GmailCleaner to review.")
    else:
        parts.append(f"checked {label}: nothing new to clean up.")
    if executed:
        parts.append(f"{executed} trusted action{'s' if executed != 1 else ''} carried out.")
    return ("GmailCleaner", "GmailCleaner " + " ".join(parts))
