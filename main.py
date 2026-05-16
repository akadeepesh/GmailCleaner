"""
GmailCleaner — Main Entrypoint
CLI interface for running scans and executing confirmed actions.

Commands:
    python main.py info
    python main.py scan
    python main.py scan --no-dry-run
    python main.py execute --session output/pending_actions.json
"""

import json
import logging
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv  # type: ignore
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

load_dotenv()
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from auth.oauth import get_credentials
from engines.bulk_cleanup import BulkCleanupEngine
from engines.unsubscribe import UnsubscribeEngine
from gmail.client import GmailClient
from gmail.executor import ExecutionSummary, GmailExecutor
from gmail.fetcher import GmailFetcher
from output.reporter import generate_report, load_pending_actions
from store.db import Database

console = Console()


# ──────────────────────────────────────────────────────────────────────────────
# Config & logging
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/settings.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Ensure config/settings.yaml exists."
        )
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(level: str = "INFO") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
    )
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--config", default="config/settings.yaml", help="Path to settings.yaml")
@click.option("--log-level", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)")
@click.pass_context
def cli(ctx: click.Context, config: str, log_level: str) -> None:
    """GmailCleaner — Self-hosted Gmail intelligence engine."""
    cfg = load_config(config)
    log_lvl = log_level or os.getenv("LOG_LEVEL") or cfg.get("logging", {}).get("level", "INFO")
    setup_logging(log_lvl)
    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg


# ── info ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show authenticated account info and config summary."""
    cfg = ctx.obj["config"]
    _print_banner()

    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    try:
        creds = get_credentials(creds_path, token_path)
        client = GmailClient(creds)
        profile = client.get_profile()
        console.print(f"\n  [bold green]✓[/bold green] Authenticated as [bold]{profile.get('emailAddress')}[/bold]")
        console.print(f"  Messages total: [bold]{profile.get('messagesTotal', 'N/A'):,}[/bold]")
        console.print(f"  Threads total:  [bold]{profile.get('threadsTotal', 'N/A'):,}[/bold]")
        console.print()
        console.print(f"  [dim]Scan window:[/dim]     {cfg['scan']['lookback_days']} days")
        console.print(f"  [dim]Max emails:[/dim]      {cfg['scan']['max_emails']:,}")
        console.print(f"  [dim]Unsubscribe:[/dim]     {'enabled' if cfg['unsubscribe']['enabled'] else 'disabled'}")
        console.print(f"  [dim]Bulk cleanup:[/dim]    {'enabled' if cfg['bulk_cleanup']['enabled'] else 'disabled'}")
        console.print()
    except FileNotFoundError as exc:
        console.print(f"\n  [red]✗ {exc}[/red]\n")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n  [red]✗ Authentication failed: {exc}[/red]\n")
        sys.exit(1)


# ── scan ──────────────────────────────────────────────────────────────────────

@cli.command("scan")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True,
              help="Dry run: produce report only, take no actions (default: true).")
@click.pass_context
def scan(ctx: click.Context, dry_run: bool) -> None:
    """
    Scan the inbox and produce a pending actions report.

    Runs the unsubscribe engine and bulk cleanup engine, then writes
    output/pending_actions.json for your review. No emails are deleted
    or unsubscribed until you run the execute command.
    """
    cfg = ctx.obj["config"]
    _print_banner()

    run_id = _make_run_id()
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    # ── Auth ──────────────────────────────────────────────────────────────────
    try:
        creds = get_credentials(creds_path, token_path)
    except FileNotFoundError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    # ── Init ──────────────────────────────────────────────────────────────────
    client = GmailClient(creds)
    db = Database(cfg["database"]["path"])
    db.create_run(run_id, mode="scan")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    fetcher = GmailFetcher(
        client=client,
        max_emails=cfg["scan"]["max_emails"],
        lookback_days=cfg["scan"]["lookback_days"],
    )
    try:
        messages = fetcher.fetch_inbox(extra_query=cfg["scan"]["query_filter"])
    except Exception as exc:
        logging.getLogger(__name__).error("Fetch failed: %s", exc)
        sys.exit(1)

    if not messages:
        console.print("\n  [yellow]No messages found in the scan window.[/yellow]\n")
        return

    # ── Update sender records ─────────────────────────────────────────────────
    _update_sender_records(db, messages)

    # ── Engines ───────────────────────────────────────────────────────────────
    unsubscribe_candidates = []
    if cfg["unsubscribe"]["enabled"]:
        unsub_engine = UnsubscribeEngine(
            db=db,
            min_email_count=cfg["unsubscribe"]["min_email_count"],
            blocked_senders=cfg.get("blocked_senders", []),
        )
        unsubscribe_candidates = unsub_engine.get_new_candidates(messages)

    cleanup_groups = []
    if cfg["bulk_cleanup"]["enabled"]:
        cleanup_engine = BulkCleanupEngine(
            db=db,
            min_group_size=cfg["bulk_cleanup"]["min_group_size"],
            blocked_senders=cfg.get("blocked_senders", []),
        )
        cleanup_groups = cleanup_engine.detect(messages)

    # ── Report ────────────────────────────────────────────────────────────────
    generate_report(
        run_id=run_id,
        emails_scanned=len(messages),
        unsubscribe_candidates=unsubscribe_candidates,
        cleanup_groups=cleanup_groups,
        dry_run=dry_run,
    )

    # ── Log pending actions to DB ─────────────────────────────────────────────
    for candidate in unsubscribe_candidates:
        db.log_action(
            run_id=run_id,
            action_type="unsubscribe",
            target=candidate.domain,
            target_meta=json.dumps({
                "display_name": candidate.display_name,
                "email_count": candidate.email_count,
                "method": candidate.method_label,
            }),
            status="pending",
        )

    for group in cleanup_groups:
        db.log_action(
            run_id=run_id,
            action_type="delete",
            target=group.group_id,
            target_meta=json.dumps({
                "label": group.label,
                "email_count": group.email_count,
                "group_type": group.group_type,
            }),
            status="pending",
        )

    db.complete_run(
        run_id=run_id,
        emails_scanned=len(messages),
        actions_pending=len(unsubscribe_candidates) + len(cleanup_groups),
        actions_executed=0,
        actions_failed=0,
    )


# ── execute ───────────────────────────────────────────────────────────────────

@cli.command("execute")
@click.option("--session", default="output/pending_actions.json", show_default=True,
              help="Path to the pending_actions.json file from a scan run.")
@click.pass_context
def execute(ctx: click.Context, session: str) -> None:
    """
    Execute confirmed actions from a pending_actions.json file.

    Open the file, set "confirmed": true on the items you want to process,
    then run this command. GmailCleaner will unsubscribe and/or delete
    only the confirmed items.
    """
    cfg = ctx.obj["config"]
    _print_banner()

    report = load_pending_actions(session)
    if not report:
        sys.exit(1)

    confirmed_unsub = [
        item for item in report.get("unsubscribe", [])
        if item.get("confirmed") is True
    ]
    confirmed_delete = [
        item for item in report.get("delete_groups", [])
        if item.get("confirmed") is True
    ]

    if not confirmed_unsub and not confirmed_delete:
        console.print(
            "\n  [yellow]No confirmed actions found.[/yellow]\n"
            f'  Open [cyan]{session}[/cyan] and set [cyan]"confirmed": true[/cyan] '
            "on the items you want to process.\n"
        )
        return

    console.print(
        f"\n  [bold]{len(confirmed_unsub)}[/bold] unsubscribe(s) confirmed  "
        f"[bold]{len(confirmed_delete)}[/bold] delete group(s) confirmed\n"
    )

    # ── Auth & init ───────────────────────────────────────────────────────────
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    try:
        creds = get_credentials(creds_path, token_path)
    except FileNotFoundError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    client = GmailClient(creds)
    db = Database(cfg["database"]["path"])
    run_id = report["run_id"]

    # ── Execute ───────────────────────────────────────────────────────────────
    unsub_results = []
    delete_results = []

    with GmailExecutor(client, db) as executor:

        for item in confirmed_unsub:
            action_id = _find_or_create_action(db, run_id, "unsubscribe", item["domain"])
            console.print(
                f"  [cyan]→[/cyan] Unsubscribing from [bold]{item['display_name']}[/bold] "
                f"([dim]{item['domain']}[/dim])"
            )
            result = executor.unsubscribe(
                domain=item["domain"],
                run_id=run_id,
                action_id=action_id,
                http_url=item.get("http_url"),
                mailto=item.get("mailto"),
                supports_one_click=item.get("supports_one_click", False),
            )
            unsub_results.append(result)
            status = "[green]✓[/green]" if result.success else f"[red]✗ {result.error}[/red]"
            console.print(f"    {status}")

        for item in confirmed_delete:
            action_id = _find_or_create_action(db, run_id, "delete", item["group_id"])
            console.print(
                f"  [yellow]→[/yellow] Deleting [bold]{item['label']}[/bold] "
                f"([dim]{item['email_count']} emails[/dim])"
            )
            result = executor.delete_group(
                group_id=item["group_id"],
                message_ids=item["message_ids"],
                run_id=run_id,
                action_id=action_id,
            )
            delete_results.append(result)
            if result.failed == 0:
                console.print(f"    [green]✓ {result.deleted} emails permanently deleted[/green]")
            else:
                console.print(
                    f"    [yellow]⚠ {result.deleted} deleted, {result.failed} failed[/yellow]"
                )

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = ExecutionSummary(
        run_id=run_id,
        unsubscribe_results=unsub_results,
        delete_results=delete_results,
    )

    execute_run_id = f"{run_id}-execute"
    try:
        db.create_run(execute_run_id, mode="execute")
    except Exception:
        pass
    db.complete_run(
        run_id=execute_run_id,
        emails_scanned=0,
        actions_pending=0,
        actions_executed=summary.total_unsubscribed + summary.total_deleted,
        actions_failed=summary.total_failed,
    )

    console.print()
    console.print(
        Panel(
            f"[bold]Execution complete[/bold]\n\n"
            f"  Unsubscribed:   [green]{summary.total_unsubscribed}[/green] / {len(confirmed_unsub)}\n"
            f"  Emails deleted: [green]{summary.total_deleted:,}[/green]\n"
            f"  Failed:         "
            + (f"[red]{summary.total_failed}[/red]" if summary.total_failed else "0"),
            border_style="green" if summary.total_failed == 0 else "yellow",
            padding=(1, 4),
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _print_banner() -> None:
    console.print()
    console.print("[bold bright_blue]GmailCleaner[/bold bright_blue]  [dim]v0.2.0 · Phase 2[/dim]")
    console.print("[dim]─────────────────────────────────────[/dim]")


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _update_sender_records(db: Database, messages) -> None:
    by_domain: dict = {}
    for msg in messages:
        if msg.sender_domain:
            by_domain.setdefault(msg.sender_domain, []).append(msg)
    for domain, msgs in by_domain.items():
        names = [m.sender_name for m in msgs if m.sender_name]
        display_name = Counter(names).most_common(1)[0][0] if names else None
        db.upsert_sender(domain=domain, display_name=display_name, email_count=len(msgs))


def _find_or_create_action(
    db: Database,
    run_id: str,
    action_type: str,
    target: str,
) -> int:
    """
    Find an existing pending action_log row for this run+type+target,
    or create a new confirmed one if re-running execute.
    """
    pending = db.get_pending_actions(run_id)
    for action in pending:
        if action.action_type == action_type and action.target == target:
            return action.id
    return db.log_action(
        run_id=run_id,
        action_type=action_type,
        target=target,
        status="confirmed",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()