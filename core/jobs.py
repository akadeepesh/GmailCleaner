"""
GmailCleaner — in-process background jobs for the dashboard.

Scans and executes take minutes, so HTTP requests only submit a job and the
page polls GET /api/jobs/<id> for its progress. Rules:

  * At most one running job per account. Submitting another job for an
    account that is busy returns the existing job instead of starting one.
  * Each job runs in its own daemon thread; the last ~50 jobs are kept in
    memory for the UI. Nothing is persisted: jobs do not survive a restart.
  * Errors are turned into plain-language messages (user_message) and the
    full traceback goes to the log only, so tokens or keys that may appear
    in exception text never reach the browser.
"""

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# fn(progress) -> JSON-serialisable result dict
JobFn = Callable[[Callable[[str, float], None]], Optional[dict]]

ACTIVE = ("queued", "running")


@dataclass
class Job:
    id: str
    account: Optional[str]
    kind: str
    status: str = "queued"           # queued | running | done | failed
    progress: float = 0.0
    step: str = "Waiting to start"
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: _now())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class JobRunner:
    def __init__(self, max_history: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._max_history = max_history

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, account: Optional[str], kind: str, fn: JobFn) -> tuple[str, bool]:
        """
        Start fn in the background for an account.

        Returns:
            (job_id, created). created is False when the account already had a
            running job; job_id is then that job's id and fn is not run.
        """
        with self._lock:
            running = self._active_for(account)
            if running is not None:
                return running.id, False
            job = Job(id=uuid.uuid4().hex[:12], account=account, kind=kind)
            self._jobs[job.id] = job
            thread = threading.Thread(
                target=self._run, args=(job, fn), name=f"gc-job-{kind}-{job.id}", daemon=True
            )
            self._threads[job.id] = thread
            self._prune()
        thread.start()
        return job.id, True

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def list(self, account: Any = ...) -> list[dict]:
        """Newest first. Pass account=None for the unnamed account; omit for all."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if account is ... or j.account == account]
            return [j.to_dict() for j in sorted(jobs, key=lambda j: j.created_at, reverse=True)]

    def active(self, account: Optional[str]) -> Optional[dict]:
        with self._lock:
            job = self._active_for(account)
            return job.to_dict() if job else None

    def any_active(self) -> bool:
        with self._lock:
            return any(j.status in ACTIVE for j in self._jobs.values())

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[dict]:
        """Block until the job finishes (tests, shutdown). Returns its final state."""
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Wait up to timeout seconds in total for running jobs to finish."""
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        for thread in list(self._threads.values()):
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                break
            thread.join(remaining)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _run(self, job: Job, fn: JobFn) -> None:
        def progress(step: str, fraction: float) -> None:
            with self._lock:
                job.step = step
                job.progress = max(job.progress, min(max(float(fraction), 0.0), 1.0))

        with self._lock:
            job.status = "running"
            job.started_at = _now()
        try:
            result = fn(progress)
            with self._lock:
                job.result = result
                job.status = "done"
                job.progress = 1.0
                job.step = "Done"
        except Exception as exc:
            logger.error("Job %s (%s) failed", job.id, job.kind, exc_info=True)
            with self._lock:
                job.error = user_message(exc)
                job.status = "failed"
                job.step = "Failed"
        finally:
            with self._lock:
                job.finished_at = _now()
                self._threads.pop(job.id, None)

    def _active_for(self, account: Optional[str]) -> Optional[Job]:
        return next(
            (j for j in self._jobs.values() if j.account == account and j.status in ACTIVE),
            None,
        )

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_history:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status not in ACTIVE),
            key=lambda j: j.created_at,
        )
        for job in finished[: len(self._jobs) - self._max_history]:
            self._jobs.pop(job.id, None)


def user_message(exc: BaseException) -> str:
    """Map an exception to a message that is safe and useful to show to users."""
    from auth.oauth import SignInRequired
    from core.pipeline import AuthRequired, Busy, PipelineError

    if isinstance(exc, (AuthRequired, SignInRequired)):
        return "Please sign in again."
    if isinstance(exc, Busy):
        return "A scan is already running."
    if isinstance(exc, PipelineError):
        return str(exc)
    return f"Something went wrong ({type(exc).__name__}). Details are in the log file."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
