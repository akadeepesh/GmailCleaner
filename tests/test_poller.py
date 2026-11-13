"""
Tests for pubsub/poller.py

Covers:
  - poll_account: the first run stores the historyId and stops; later runs
    call WebhookHandler.handle with the account's email and current id
  - Poller.tick: off when poll_minutes is 0, minimum 5 minutes, interval
    respected, backoff doubles up to 1 hour and resets after a success,
    skipped while busy or paused, interval change applies at once
"""

from unittest.mock import MagicMock, patch

import pytest

from core.pipeline import Busy
from core.scheduler import ScheduledAccount
from pubsub import poller as poller_mod
from pubsub.poller import MAX_BACKOFF_SECONDS, Poller, poll_account
from store.db import Database


def account(tmp_path, minutes=15, db=None):
    cfg = {
        "realtime": {"poll_minutes": minutes},
        "database": {"path": str(tmp_path / "acct" / "gmailcleaner.db")},
        "output": {"pending_actions_path": str(tmp_path / "pending.json")},
        "blocked_senders": [],
        "triage": {"enabled": False},
    }
    return ScheduledAccount("me_x.com", "me_x.com", "me@x.com", cfg, db)


# ── poll_account ─────────────────────────────────────────────────────────────

class TestPollAccount:
    @pytest.fixture
    def gmail(self):
        g = MagicMock()
        g.get_profile.return_value = {"emailAddress": "me@x.com", "historyId": "500"}
        with patch.object(poller_mod, "_credentials"), \
             patch.object(poller_mod, "GmailClient", return_value=g):
            yield g

    def test_first_run_stores_baseline(self, tmp_path, gmail):
        acct = account(tmp_path)
        with patch.object(poller_mod, "build_handler") as build:
            result = poll_account(acct)
        assert result == {"status": "baseline_set", "history_id": "500"}
        build.assert_not_called()
        assert Database(acct.cfg["database"]["path"]).get_last_history_id() == "500"

    def test_later_runs_call_handler(self, tmp_path, gmail):
        db = Database(str(tmp_path / "acct" / "gmailcleaner.db"))
        db.save_history_id("400")
        acct = account(tmp_path, db=db)
        handler = MagicMock()
        handler.handle.return_value = {"status": "processed", "new_messages": 2}
        with patch.object(poller_mod, "build_handler", return_value=handler) as build:
            result = poll_account(acct)
        assert result["status"] == "processed"
        build.assert_called_once_with(gmail, db, acct.cfg)
        note = handler.handle.call_args.args[0]
        assert (note.email_address, note.history_id, note.raw_message_id) == ("me@x.com", "500", "poll")
        handler.close.assert_called_once()

    def test_handler_closed_on_error(self, tmp_path, gmail):
        db = Database(str(tmp_path / "acct" / "gmailcleaner.db"))
        db.save_history_id("400")
        handler = MagicMock()
        handler.handle.side_effect = RuntimeError("boom")
        with patch.object(poller_mod, "build_handler", return_value=handler):
            with pytest.raises(RuntimeError):
                poll_account(account(tmp_path, db=db))
        handler.close.assert_called_once()

    def test_missing_history_id(self, tmp_path, gmail):
        gmail.get_profile.return_value = {"emailAddress": "me@x.com"}
        with pytest.raises(RuntimeError):
            poll_account(account(tmp_path))

    def test_real_handler_end_to_end(self, tmp_path, gmail):
        """With the real WebhookHandler: an equal historyId is 'already processed'."""
        db = Database(str(tmp_path / "acct" / "gmailcleaner.db"))
        db.save_history_id("500")
        assert poll_account(account(tmp_path, db=db)) == {"status": "already_processed"}


# ── Poller.tick ──────────────────────────────────────────────────────────────

class Harness:
    def __init__(self, tmp_path, minutes=15):
        self.acct = account(tmp_path, minutes)
        self.calls = []
        self.fail = False
        self.busy = False
        self.is_paused = False
        self.poller = Poller(accounts=lambda: [self.acct], poll=self.poll,
                             is_busy=lambda a: self.busy, paused=lambda: self.is_paused)

    def poll(self, acct):
        self.calls.append(acct.key)
        if self.fail:
            raise ConnectionError("offline")
        return {"status": "processed"}


class TestPollerTick:
    def test_off_when_zero(self, tmp_path):
        h = Harness(tmp_path, minutes=0)
        h.poller.tick(0)
        assert h.calls == []

    def test_polls_immediately_then_on_interval(self, tmp_path):
        h = Harness(tmp_path, minutes=15)
        assert h.poller.tick(0) == {"me_x.com": {"status": "processed"}}
        h.poller.tick(14 * 60)
        assert len(h.calls) == 1
        h.poller.tick(15 * 60)
        assert len(h.calls) == 2

    def test_minimum_five_minutes(self, tmp_path):
        h = Harness(tmp_path, minutes=1)
        h.poller.tick(0)
        h.poller.tick(4 * 60)
        assert len(h.calls) == 1
        h.poller.tick(5 * 60)
        assert len(h.calls) == 2

    def test_backoff_doubles_and_caps(self, tmp_path):
        h = Harness(tmp_path, minutes=15)
        h.fail = True
        h.poller.tick(0)                       # failure 1 → wait 30 min
        assert h.poller.next_due("me_x.com") == 30 * 60
        h.poller.tick(30 * 60)                 # failure 2 → 60 min (cap)
        assert h.poller.next_due("me_x.com") == 30 * 60 + MAX_BACKOFF_SECONDS
        h.poller.tick(90 * 60)                 # failure 3 → still capped at 1h
        assert h.poller.next_due("me_x.com") == 90 * 60 + MAX_BACKOFF_SECONDS

    def test_success_resets_backoff(self, tmp_path):
        h = Harness(tmp_path, minutes=15)
        h.fail = True
        h.poller.tick(0)
        h.fail = False
        h.poller.tick(30 * 60)
        assert h.poller.next_due("me_x.com") == 45 * 60

    def test_skipped_while_busy(self, tmp_path):
        h = Harness(tmp_path)
        h.busy = True
        h.poller.tick(0)
        assert h.calls == []
        h.busy = False
        h.poller.tick(60)
        assert h.calls == ["me_x.com"]

    def test_lock_busy_retries_soon_without_backoff(self, tmp_path):
        h = Harness(tmp_path)

        def busy_poll(acct):
            raise Busy()
        h.poller._poll = busy_poll
        h.poller.tick(0)
        assert h.poller.next_due("me_x.com") == 60

    def test_paused(self, tmp_path):
        h = Harness(tmp_path)
        h.is_paused = True
        h.poller.tick(0)
        assert h.calls == []

    def test_interval_change_applies_now(self, tmp_path):
        h = Harness(tmp_path, minutes=60)
        h.poller.tick(0)
        h.acct.cfg["realtime"]["poll_minutes"] = 15
        h.poller.tick(60)
        assert len(h.calls) == 2

    def test_forgets_removed_accounts(self, tmp_path):
        h = Harness(tmp_path)
        h.poller.tick(0)
        h.poller._accounts = lambda: []
        h.poller.tick(10)
        assert h.poller.next_due("me_x.com") is None
