"""
GmailCleaner — scan and execute pipelines.

One source of truth for what a scan and an execute do. The CLI (main.py)
and the dashboard (web/app.py, via background jobs) are thin wrappers that
only differ in how they show progress:

    run_scan(cfg, dry_run=True, progress=cb)  → ScanResult
    run_execute(cfg, progress=cb)             → ExecutionSummary

Neither function prints or exits. Failures raise PipelineError subclasses
with plain-language messages. Both hold a per-account file lock, so the CLI
and the desktop app can never scan or execute the same account at once.
"""

import json
import logging
import os
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from filelock import FileLock, Timeout

from ai.gemini import GeminiClient
from auth.oauth import load_credentials
from core.secrets import get_gemini_key
from engines.bulk_cleanup import BulkCleanupEngine
from engines.digest import DigestEngine
from engines.rules import TrustedRules
from engines.triage import TriageEngine
from engines.unsubscribe import UnsubscribeEngine
from gmail.client import GmailClient
from gmail.executor import ExecutionSummary, GmailExecutor, ResultCallback, confirmed_items, execute_report
from gmail.fetcher import GmailFetcher
from output.reporter import generate_report, load_pending_actions, save_pending_actions
from store.db import Database

logger = logging.getLogger("gmailcleaner")

ProgressFn = Callable[[str, float], None]

TRIAGE_QUOTA_WARNING = "AI triage skipped: free daily limit reached"


# ──────────────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────────────

class PipelineError(Exception):
    """Base class: str(exc) is a message that is safe to show to the user."""

    default_message = "Something went wrong."

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message or self.default_message)


class AuthRequired(PipelineError):
    default_message = "Please sign in again."


class FetchFailed(PipelineError):
    default_message = "Could not fetch emails from Gmail. Check your internet connection and try again."


class Busy(PipelineError):
    default_message = "A scan is already running for this account."


class NoReport(PipelineError):
    default_message = "No suggestions yet: run a scan first."


class NothingConfirmed(PipelineError):
    default_message = "Nothing is approved yet. Approve some suggestions first."


# ──────────────────────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    run_id: str
    emails_scanned: int = 0
    unsubscribe_count: int = 0
    cleanup_count: int = 0
    triage_counts: dict = field(default_factory=dict)
    auto_confirmed: int = 0
    executed: int = 0
    failed: int = 0
    digest_sent: bool = False
    dry_run: bool = True
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def execution_result(summary: ExecutionSummary) -> dict:
    """The JSON shape /api/execute has always returned."""
    return {
        "status": "executed",
        "unsubscribed": summary.total_unsubscribed,
        "trashed": summary.total_deleted,
        "failed": summary.total_failed,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Locking
# ──────────────────────────────────────────────────────────────────────────────

def lock_path(cfg: dict) -> Path:
    """<data dir of the account>/.lock, next to that account's database."""
    return Path(cfg["database"]["path"]).parent / ".lock"


@contextmanager
def account_lock(cfg: dict) -> Iterator[None]:
    """
    Hold the account's cross-process lock without waiting; raise Busy if held.

    If the filesystem does not support locking (some network mounts), log it
    and carry on unlocked rather than making the app unusable.
    """
    path = lock_path(cfg)
    lock: Optional[FileLock] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path), thread_local=False)
        lock.acquire(timeout=0)
    except Timeout:
        raise Busy() from None
    except OSError as exc:
        logger.warning("Could not lock %s (%s); continuing without a lock.", path, exc)
        lock = None
    try:
        yield
    finally:
        if lock is not None:
            lock.release()


# ──────────────────────────────────────────────────────────────────────────────
# Scan
# ──────────────────────────────────────────────────────────────────────────────

def run_scan(
    cfg: dict,
    *,
    dry_run: bool = True,
    progress: Optional[ProgressFn] = None,
    creds=None,
    client: Optional[GmailClient] = None,
    db: Optional[Database] = None,
    on_result: Optional[ResultCallback] = None,
    on_auto_execute: Optional[Callable[[int], None]] = None,
) -> ScanResult:
    """
    Scan the inbox and write the pending actions report.

    Runs the unsubscribe, bulk cleanup and triage engines, sends the digest,
    then writes the report. Nothing is unsubscribed or trashed unless, with
    dry_run=False, it matches a trusted rule (which requires confirm_first: false).

    Args:
        cfg:             Resolved config for one account.
        dry_run:         Produce the report only (trusted rules just auto-confirm).
        progress:        Called as progress(step, fraction 0..1).
        creds / client:  Inject credentials or a ready GmailClient. With neither,
                         the saved token is used without any interactive sign-in.
        db:              Reuse an open Database for this account.
        on_result:       Per-action callback when trusted rules execute.
        on_auto_execute: Called with the count just before trusted rules execute.

    Raises:
        AuthRequired, FetchFailed, Busy.
    """
    with account_lock(cfg):
        return _scan(cfg, dry_run, progress or _noop, creds, client, db, on_result, on_auto_execute)


def _scan(cfg, dry_run, progress, creds, client, db, on_result, on_auto_execute) -> ScanResult:
    run_id = make_run_id()
    result = ScanResult(run_id=run_id, dry_run=dry_run)
    pending_path = cfg["output"]["pending_actions_path"]

    # ── Auth & init ───────────────────────────────────────────────────────────
    progress("Signing in", 0.02)
    if client is None:
        client = GmailClient(creds if creds is not None else _credentials(cfg))
    db = db or Database(cfg["database"]["path"])
    db.create_run(run_id, mode="scan")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    progress("Fetching emails", 0.05)
    fetcher = GmailFetcher(
        client=client,
        max_emails=cfg["scan"]["max_emails"],
        lookback_days=cfg["scan"]["lookback_days"],
    )
    try:
        messages = fetcher.fetch_inbox(extra_query=cfg["scan"]["query_filter"])
    except Exception as exc:
        logger.error("Fetch failed: %s", exc)
        db.complete_run(run_id, emails_scanned=0, actions_pending=0,
                        actions_executed=0, actions_failed=0)
        raise FetchFailed() from exc
    progress("Fetching emails", 0.40)

    if not messages:
        db.complete_run(run_id, emails_scanned=0, actions_pending=0,
                        actions_executed=0, actions_failed=0)
        progress("Done", 1.0)
        return result
    result.emails_scanned = len(messages)

    update_sender_records(db, messages)
    blocked = cfg.get("blocked_senders", [])

    # ── Engines ───────────────────────────────────────────────────────────────
    progress("Finding newsletters", 0.45)
    unsubscribe_candidates = []
    if cfg["unsubscribe"]["enabled"]:
        unsub_engine = UnsubscribeEngine(
            db=db,
            min_email_count=cfg["unsubscribe"]["min_email_count"],
            blocked_senders=blocked,
        )
        unsubscribe_candidates = unsub_engine.get_new_candidates(messages)

    progress("Grouping clutter", 0.55)
    cleanup_groups = []
    if cfg["bulk_cleanup"]["enabled"]:
        cleanup_engine = BulkCleanupEngine(
            db=db,
            min_group_size=cfg["bulk_cleanup"]["min_group_size"],
            blocked_senders=blocked,
        )
        cleanup_groups = cleanup_engine.detect(messages)

    # ── Triage ────────────────────────────────────────────────────────────────
    triage_summary = None
    if cfg["triage"]["enabled"]:
        progress("AI triage", 0.60)
        gemini_api_key = get_gemini_key()
        if not gemini_api_key:
            logger.warning("Triage enabled but GEMINI_API_KEY is not set — skipping triage.")
            result.warnings.append("AI sorting is on, but no Gemini key is set, so it was skipped.")
        else:
            # Don't classify mail that a cleanup group already proposes to trash.
            cleanup_claimed_ids: set[str] = set()
            for group in cleanup_groups:
                cleanup_claimed_ids.update(group.message_ids)

            with GeminiClient(api_key=gemini_api_key, model=cfg["triage"]["model"]) as gemini:
                triage_engine = TriageEngine(
                    gemini_client=gemini,
                    db=db,
                    confidence_threshold=cfg["triage"]["confidence_threshold"],
                )
                triage_summary = triage_engine.classify(
                    messages=messages,
                    excluded_ids=cleanup_claimed_ids,
                )
                if getattr(gemini, "rate_limited", False):
                    result.warnings.append(TRIAGE_QUOTA_WARNING)
            result.triage_counts = {
                name: len(triage_summary.bucket(name))
                for name in ("needs_reply", "fyi", "promotions", "junk", "unclassified")
            }

    # ── Digest ────────────────────────────────────────────────────────────────
    if cfg["digest"]["enabled"] and triage_summary is not None:
        progress("Sending digest", 0.85)
        recipient = cfg["digest"].get("recipient") or own_address(client)
        if not recipient:
            logger.warning("Digest enabled but no recipient could be determined — skipping.")
        else:
            result.digest_sent = bool(DigestEngine(
                client=client,
                recipient=recipient,
                dashboard_url=os.getenv("DASHBOARD_URL") or None,
                session_path=pending_path,
            ).send(
                run_id=run_id,
                emails_scanned=len(messages),
                triage=triage_summary,
                unsubscribe_candidates=unsubscribe_candidates,
                cleanup_groups=cleanup_groups,
            ))

    # ── Report ────────────────────────────────────────────────────────────────
    progress("Saving report", 0.90)
    report = generate_report(
        run_id=run_id,
        emails_scanned=len(messages),
        unsubscribe_candidates=unsubscribe_candidates,
        cleanup_groups=cleanup_groups,
        triage_summary=triage_summary,
        dry_run=dry_run,
        output_path=pending_path,
    )
    result.unsubscribe_count = len(report["unsubscribe"])
    result.cleanup_count = len(report["delete_groups"])

    for item in report["unsubscribe"]:
        db.log_action(
            run_id=run_id,
            action_type="unsubscribe",
            target=item["domain"],
            target_meta=json.dumps({
                "display_name": item["display_name"],
                "email_count": item["email_count"],
                "method": item["method"],
            }),
            status="pending",
        )

    for item in report["delete_groups"]:
        db.log_action(
            run_id=run_id,
            action_type="delete",
            target=item["group_id"],
            target_meta=json.dumps({
                "label": item["label"],
                "email_count": item["email_count"],
                "group_type": item["group_type"],
            }),
            status="pending",
        )

    # The new report replaces the previous one, so older pending rows are stale.
    db.supersede_pending_actions(keep_run_id=run_id)

    # ── Trusted rules ─────────────────────────────────────────────────────────
    progress("Applying trusted rules", 0.95)
    rules = TrustedRules.from_config(cfg)
    result.auto_confirmed = rules.apply(report) if rules.active else 0
    if result.auto_confirmed:
        if not dry_run:
            if on_auto_execute:
                on_auto_execute(result.auto_confirmed)
            with GmailExecutor(client, db) as executor:
                summary = execute_report(executor, db, report, on_result=on_result)
            result.executed = summary.total_unsubscribed + sum(
                1 for r in summary.delete_results if r.failed == 0
            )
            result.failed = summary.total_failed
        save_pending_actions(report, pending_path)

    db.complete_run(
        run_id=run_id,
        emails_scanned=len(messages),
        actions_pending=result.unsubscribe_count + result.cleanup_count - result.executed,
        actions_executed=result.executed,
        actions_failed=result.failed,
    )
    progress("Done", 1.0)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Execute
# ──────────────────────────────────────────────────────────────────────────────

def run_execute(
    cfg: dict,
    *,
    report_path: Optional[str] = None,
    progress: Optional[ProgressFn] = None,
    on_result: Optional[ResultCallback] = None,
    creds=None,
    client: Optional[GmailClient] = None,
    db: Optional[Database] = None,
) -> ExecutionSummary:
    """
    Execute every confirmed, not-yet-executed action in the pending report.

    The report is always saved afterwards (even after a failure part-way), so
    executed_at stamps are never lost and nothing runs twice.

    Raises:
        NoReport, NothingConfirmed, AuthRequired, Busy.
    """
    progress = progress or _noop
    path = report_path or cfg["output"]["pending_actions_path"]
    with account_lock(cfg):
        report = load_pending_actions(path, missing_ok=True)
        if not report:
            raise NoReport()
        unsub_items, delete_items = confirmed_items(report)
        total = len(unsub_items) + len(delete_items)
        if not total:
            raise NothingConfirmed()

        progress("Signing in", 0.02)
        if client is None:
            client = GmailClient(creds if creds is not None else _credentials(cfg))
        db = db or Database(cfg["database"]["path"])

        done = 0

        def _on_result(action_type: str, item: dict, res: Any) -> None:
            nonlocal done
            done += 1
            progress("Unsubscribing and moving to Trash", 0.05 + 0.95 * done / total)
            if on_result:
                on_result(action_type, item, res)

        progress("Unsubscribing and moving to Trash", 0.05)
        try:
            with GmailExecutor(client, db) as executor:
                summary = execute_report(executor, db, report, on_result=_on_result)
        finally:
            save_pending_actions(report, path)
        progress("Done", 1.0)
        return summary


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def update_sender_records(db: Database, messages) -> None:
    by_domain: dict = {}
    for msg in messages:
        if msg.sender_domain:
            by_domain.setdefault(msg.sender_domain, []).append(msg)
    for domain, msgs in by_domain.items():
        names = [m.sender_name for m in msgs if m.sender_name]
        display_name = Counter(names).most_common(1)[0][0] if names else None
        db.upsert_sender(domain=domain, display_name=display_name, email_count=len(msgs))


def own_address(client: GmailClient) -> str:
    """The authenticated account's address — the default digest recipient."""
    try:
        return client.get_profile().get("emailAddress", "")
    except Exception as exc:
        logger.warning("Could not look up own address for digest: %s", exc)
        return ""


def _credentials(cfg: dict):
    """Saved credentials, never an interactive consent flow."""
    try:
        return load_credentials(cfg, interactive=False)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.warning("Sign-in needed: %s", exc)
        raise AuthRequired() from exc


def _noop(step: str, fraction: float) -> None:
    pass
