"""
Tests for core/scheduler.py

Covers:
  - most_recent_slot: same day, earlier day, day filter, no days, bad time
  - Scheduler.tick: baseline on first sight, a slot passing while running
    runs once, catch-up after a closed app (on/off, 24h window), turning on
    or resuming doesn't run old slots, pause, disabled, duplicate job
  - Notifications on finish (done / failed) and scan_notification wording
  - The Ticker thread starts, ticks and stops
  - AppState persistence (file and memory)
"""

import json
import threading
from datetime import datetime, timedelta

import pytest

from core.scheduler import (
    LAST_RUN_KEY,
    PAUSED_KEY,
    ScheduledAccount,
    Scheduler,
    Ticker,
    most_recent_slot,
    scan_notification,
)
from core.state import AppState

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def local(*args) -> datetime:
    """An aware datetime in the machine's local time zone."""
    return datetime(*args).astimezone()


# 2026-11-13 is a Friday.
FRI_0900 = local(2026, 11, 13, 9, 0)


# ── Slot arithmetic ──────────────────────────────────────────────────────────

class TestMostRecentSlot:
    def test_today_after_time(self):
        assert most_recent_slot(FRI_0900, "08:00", ALL_DAYS) == local(2026, 11, 13, 8, 0)

    def test_exactly_at_time(self):
        assert most_recent_slot(local(2026, 11, 13, 8, 0), "08:00", ALL_DAYS) == local(2026, 11, 13, 8, 0)

    def test_before_time_uses_yesterday(self):
        assert most_recent_slot(local(2026, 11, 13, 7, 0), "08:00", ALL_DAYS) == local(2026, 11, 12, 8, 0)

    def test_day_filter(self):
        # Friday 09:00, only Mondays → Monday 2026-11-09
        assert most_recent_slot(FRI_0900, "08:00", ["mon"]) == local(2026, 11, 9, 8, 0)

    def test_same_weekday_before_time_goes_back_a_week(self):
        assert most_recent_slot(local(2026, 11, 13, 7, 0), "08:00", ["fri"]) == local(2026, 11, 6, 8, 0)

    def test_no_days(self):
        assert most_recent_slot(FRI_0900, "08:00", []) is None

    def test_bad_time(self):
        assert most_recent_slot(FRI_0900, "8 o'clock", ALL_DAYS) is None


# ── Scheduler ────────────────────────────────────────────────────────────────

class Harness:
    def __init__(self, tmp_path, *, schedule=None, started=FRI_0900, created=True):
        self.state = AppState(tmp_path / "state.json")
        self.schedule = {"enabled": True, "time": "08:00", "days": ALL_DAYS,
                         "run_trusted_rules": True, "catch_up": True, **(schedule or {})}
        self.submitted = []
        self.callbacks = []
        self.notes = []
        self.created = created
        self.scheduler = Scheduler(
            accounts=self.accounts, submit=self.submit, state=self.state,
            notify=lambda t, m: self.notes.append((t, m)), clock=lambda: started,
        )

    def accounts(self):
        return [ScheduledAccount("jane_gmail.com", "jane_gmail.com", "jane@gmail.com",
                                 {"schedule": self.schedule})]

    def submit(self, account, run_trusted, on_finish):
        self.submitted.append((account.name, run_trusted))
        self.callbacks.append(on_finish)
        return "job1", self.created

    def set_last(self, when):
        self.state.set(LAST_RUN_KEY, {"jane_gmail.com": when.isoformat()})

    def last(self):
        return datetime.fromisoformat(self.state.get(LAST_RUN_KEY)["jane_gmail.com"])


class TestScheduler:
    def test_first_sight_sets_baseline_without_running(self, tmp_path):
        h = Harness(tmp_path)
        assert h.scheduler.tick(FRI_0900) == []
        assert h.submitted == []
        assert h.last() == FRI_0900

    def test_slot_passing_while_running_runs_once(self, tmp_path):
        h = Harness(tmp_path, schedule={"time": "09:02"})
        h.scheduler.tick(FRI_0900)                         # baseline
        h.scheduler.tick(local(2026, 11, 13, 9, 1))        # not yet
        assert h.submitted == []
        assert h.scheduler.tick(local(2026, 11, 13, 9, 2, 10)) == ["jane_gmail.com"]
        h.scheduler.tick(local(2026, 11, 13, 9, 2, 40))    # same slot: no repeat
        h.scheduler.tick(local(2026, 11, 13, 12, 0))
        assert h.submitted == [("jane_gmail.com", True)]

    def test_runs_again_next_day(self, tmp_path):
        h = Harness(tmp_path)
        h.set_last(local(2026, 11, 13, 8, 0, 5))
        h.scheduler.tick(FRI_0900)
        assert h.submitted == []
        h.scheduler.tick(local(2026, 11, 14, 8, 0, 20))
        assert len(h.submitted) == 1

    def test_catch_up_after_closed_app(self, tmp_path):
        h = Harness(tmp_path)
        h.set_last(local(2026, 11, 12, 8, 0))   # yesterday's ran; today's 08:00 was missed
        assert h.scheduler.tick(FRI_0900) == ["jane_gmail.com"]
        h.scheduler.tick(local(2026, 11, 13, 9, 0, 30))
        assert len(h.submitted) == 1             # only once

    def test_catch_up_off(self, tmp_path):
        h = Harness(tmp_path, schedule={"catch_up": False})
        h.set_last(local(2026, 11, 12, 8, 0))
        h.scheduler.tick(FRI_0900)
        h.scheduler.tick(local(2026, 11, 13, 12, 0))
        assert h.submitted == []
        h.scheduler.tick(local(2026, 11, 14, 8, 0, 10))   # the next slot runs normally
        assert len(h.submitted) == 1

    def test_catch_up_window_is_24h(self, tmp_path):
        # Only Mondays; app opens Friday: Monday's slot is days old → skipped.
        h = Harness(tmp_path, schedule={"days": ["mon"]})
        h.set_last(local(2026, 11, 2, 8, 0))
        h.scheduler.tick(FRI_0900)
        assert h.submitted == []

    def test_turning_on_does_not_run_old_slots(self, tmp_path):
        h = Harness(tmp_path, schedule={"enabled": False}, started=local(2026, 11, 13, 6, 0))
        h.set_last(local(2026, 11, 1, 8, 0))     # ran long ago, then turned off
        h.scheduler.tick(local(2026, 11, 13, 6, 0))
        h.schedule["enabled"] = True             # turned on at 09:00, after today's 08:00
        h.scheduler.tick(FRI_0900)
        h.scheduler.tick(local(2026, 11, 13, 9, 0, 30))
        assert h.submitted == []
        assert h.last() == FRI_0900

    def test_pause_and_resume(self, tmp_path):
        h = Harness(tmp_path, started=local(2026, 11, 13, 6, 0))
        h.set_last(local(2026, 11, 12, 8, 0, 1))
        h.scheduler.tick(local(2026, 11, 13, 6, 0))
        h.scheduler.set_paused(True)
        assert h.state.get(PAUSED_KEY) is True
        h.scheduler.tick(local(2026, 11, 13, 8, 0, 10))   # paused: skipped
        h.scheduler.set_paused(False)
        h.scheduler.tick(FRI_0900)                        # resumed after the slot: not run
        assert h.submitted == []
        h.scheduler.tick(local(2026, 11, 14, 8, 0, 10))
        assert len(h.submitted) == 1

    def test_disabled_never_runs(self, tmp_path):
        h = Harness(tmp_path, schedule={"enabled": False})
        h.set_last(local(2026, 11, 12, 8, 0))
        h.scheduler.tick(FRI_0900)
        assert h.submitted == []

    def test_run_trusted_rules_passed(self, tmp_path):
        h = Harness(tmp_path, schedule={"run_trusted_rules": False})
        h.set_last(local(2026, 11, 12, 8, 0))
        h.scheduler.tick(FRI_0900)
        assert h.submitted == [("jane_gmail.com", False)]

    def test_busy_account_marks_slot_done(self, tmp_path):
        h = Harness(tmp_path, created=False)
        h.set_last(local(2026, 11, 12, 8, 0))
        assert h.scheduler.tick(FRI_0900) == []
        h.scheduler.tick(local(2026, 11, 13, 9, 0, 30))
        assert len(h.submitted) == 1             # tried once, not every tick

    def test_notification_on_finish(self, tmp_path):
        h = Harness(tmp_path)
        h.set_last(local(2026, 11, 12, 8, 0))
        h.scheduler.tick(FRI_0900)
        h.callbacks[0]({"status": "done", "result": {"unsubscribe_count": 4, "cleanup_count": 3}})
        assert h.notes and "found 7 suggestions for jane@gmail.com" in h.notes[0][1]

    def test_extra_tasks_run_and_errors_contained(self, tmp_path):
        calls = []
        state = AppState(tmp_path / "s.json")
        s = Scheduler(accounts=lambda: [], submit=lambda *a: ("x", True), state=state,
                      extra_tasks=[lambda: 1 / 0, lambda: calls.append(1)])
        s.tick(FRI_0900)
        assert calls == [1]

    def test_account_listing_failure_contained(self, tmp_path):
        def boom():
            raise OSError("disk")
        s = Scheduler(accounts=boom, submit=lambda *a: ("x", True), state=AppState(None))
        assert s.tick(FRI_0900) == []


class TestScanNotification:
    def test_nothing_found(self):
        _, msg = scan_notification({"status": "done", "result": {}}, "me@x.com")
        assert "nothing new" in msg

    def test_executed(self):
        _, msg = scan_notification(
            {"status": "done", "result": {"unsubscribe_count": 1, "executed": 2}}, "me@x.com")
        assert "1 suggestion for" in msg and "2 trusted actions carried out" in msg

    def test_failed(self):
        _, msg = scan_notification({"status": "failed", "error": "Please sign in again."}, "me@x.com")
        assert "didn't finish" in msg and "sign in again" in msg


# ── Ticker ───────────────────────────────────────────────────────────────────

def test_ticker_runs_and_stops():
    ticked = threading.Event()

    class T(Ticker):
        def tick(self):
            ticked.set()
            raise RuntimeError("errors don't kill the loop")

    t = T(interval=0.01)
    t.start()
    assert ticked.wait(2)
    assert t.running
    t.stop()
    assert not t.running


# ── AppState ─────────────────────────────────────────────────────────────────

class TestAppState:
    def test_file_roundtrip(self, tmp_path):
        path = tmp_path / "dir with space" / "state.json"
        s = AppState(path)
        s.set("a", 1)
        s.update(lambda d: d.setdefault("b", {}).__setitem__("c", 2))
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": {"c": 2}}
        assert AppState(path).get("b") == {"c": 2}

    def test_memory_only(self, tmp_path):
        s = AppState(None)
        s.set("a", 1)
        assert s.get("a") == 1
        assert list(tmp_path.iterdir()) == []

    def test_corrupt_file_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        s = AppState(path)
        assert s.load() == {}
        s.set("a", 1)
        assert s.get("a") == 1

    @pytest.mark.parametrize("content", ["[]", "3"])
    def test_non_dict_ignored(self, tmp_path, content):
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        assert AppState(path).load() == {}
