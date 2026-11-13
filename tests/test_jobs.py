"""
Tests for core/jobs.py

Covers:
  - Lifecycle: queued → running → done with progress, step and result
  - Failures are captured with a friendly message (no raw exception text)
  - One running job per account; duplicates return the existing id
  - Different accounts run in parallel
  - History is pruned to max_history, never dropping running jobs
  - user_message mapping for sign-in, busy and pipeline errors
"""

import threading

import pytest

from auth.oauth import SignInRequired
from core import jobs as jobs_mod
from core.jobs import JobRunner, user_message
from core.pipeline import AuthRequired, Busy, NothingConfirmed


@pytest.fixture
def runner():
    r = JobRunner(max_history=5)
    yield r
    r.shutdown(timeout=5)


def _blocking():
    release = threading.Event()

    def fn(progress):
        progress("Working", 0.5)
        release.wait(5)
        return {"ok": True}

    return fn, release


def test_lifecycle(runner):
    fn, release = _blocking()
    job_id, created = runner.submit("a", "scan", fn)
    assert created
    release.set()
    job = runner.wait(job_id, 5)
    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert job["result"] == {"ok": True}
    assert job["started_at"] and job["finished_at"]


def test_progress_visible_while_running(runner):
    started = threading.Event()
    release = threading.Event()

    def fn(progress):
        progress("Fetching emails", 0.4)
        started.set()
        release.wait(5)

    job_id, _ = runner.submit("a", "scan", fn)
    assert started.wait(5)
    job = runner.get(job_id)
    assert job["status"] == "running" and job["step"] == "Fetching emails" and job["progress"] == 0.4
    release.set()
    runner.wait(job_id, 5)


def test_failure_captured_without_raw_text(runner):
    def fn(progress):
        raise ValueError("token=ya29.SECRET")

    job = runner.wait(runner.submit("a", "scan", fn)[0], 5)
    assert job["status"] == "failed"
    assert "SECRET" not in job["error"]
    assert "ValueError" in job["error"]


def test_duplicate_rejected(runner):
    fn, release = _blocking()
    first, _ = runner.submit("a", "scan", fn)
    second, created = runner.submit("a", "execute", lambda p: None)
    assert second == first and created is False
    other, created = runner.submit("b", "scan", lambda p: {"b": 1})
    assert created and other != first
    assert runner.wait(other, 5)["status"] == "done"
    release.set()
    runner.wait(first, 5)
    # Free again once finished.
    assert runner.submit("a", "scan", lambda p: None)[1] is True


def test_active_and_list(runner):
    fn, release = _blocking()
    job_id, _ = runner.submit(None, "scan", fn)
    assert runner.active(None)["id"] == job_id
    assert runner.any_active()
    assert [j["id"] for j in runner.list(None)] == [job_id]
    assert runner.list("other") == []
    release.set()
    runner.wait(job_id, 5)
    assert runner.active(None) is None


def test_history_pruned(runner):
    ids = [runner.submit(f"acct{i}", "scan", lambda p: None)[0] for i in range(8)]
    for job_id in ids:
        runner.wait(job_id, 5)
    runner.submit("last", "scan", lambda p: None)
    assert len(runner.list()) <= 6


def test_get_unknown(runner):
    assert runner.get("nope") is None


@pytest.mark.parametrize("exc,expected", [
    (AuthRequired(), "Please sign in again."),
    (SignInRequired("x"), "Please sign in again."),
    (Busy(), "A scan is already running."),
    (NothingConfirmed(), str(NothingConfirmed())),
])
def test_user_message(exc, expected):
    assert user_message(exc) == expected


def test_module_has_no_leaking_threads(runner):
    job_id, _ = runner.submit("a", "scan", lambda p: None)
    runner.wait(job_id, 5)
    assert not runner._threads
    assert jobs_mod.ACTIVE == ("queued", "running")


def test_on_finish_called_before_wait_returns():
    import time

    from core.jobs import JobRunner

    runner = JobRunner()
    seen = []

    def slow_callback(job):
        time.sleep(0.2)
        seen.append(job["status"])

    job_id, _ = runner.submit("a", "scan", lambda p: {"ok": True}, on_finish=slow_callback)
    runner.wait(job_id, 5)
    assert seen == ["done"]


def test_on_finish_errors_are_contained():
    from core.jobs import JobRunner

    runner = JobRunner()

    def bad_callback(job):
        raise RuntimeError("boom")

    job_id, _ = runner.submit("a", "scan", lambda p: None, on_finish=bad_callback)
    assert runner.wait(job_id, 5)["status"] == "done"
    assert runner.active("a") is None
