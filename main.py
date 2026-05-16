"""
GmailCleaner — Main Entrypoint
CLI interface for running scans and executing confirmed actions.

Usage:
    python main.py --dry-run
    python main.py --scan
    python main.py --execute --session output/pending_actions.json
    python main.py --info
"""

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv  # type: ignore
from rich.console import Console
from rich.logging import RichHandler

# ── Env & path setup ──────────────────────────────────────────────────────────
load_dotenv()
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from auth.oauth import get_credentials
from engines.unsubscribe import UnsubscribeEngine
from gmail.client import GmailClient
from gmail.fetcher import GmailFetcher
from output.reporter import generate_report, load_pending_actions
from store.db import Database

console = Console()


# ──────────────────────────────────────────────────────────────────────────────
# Config loader
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/settings.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Ensure config/settings.yaml exists (see config/settings.yaml in the repo)."
        )
    with open(path) as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

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
    # Suppress noisy third-party loggers.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3"):
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


@cli.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show authenticated account info and config summary."""
    cfg = ctx.obj["config"]
    _print_banner()

    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    try:
        creds = get_credentials(credentials_path, token_path)
        client = GmailClient(creds)
        profile = client.get_profile()
        console.print(f"\n  [bold green]✓[/bold green] Authenticated as [bold]{profile.get('emailAddress')}[/bold]")
        console.print(f"  Messages total: [bold]{profile.get('messagesTotal', 'N/A'):,}[/bold]")
        console.print(f"  Threads total:  [bold]{profile.get('threadsTotal', 'N/A'):,}[/bold]\n")
    except FileNotFoundError as exc:
        console.print(f"\n  [red]✗ {exc}[/red]\n")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n  [red]✗ Authentication failed: {exc}[/red]\n")
        sys.exit(1)


@cli.command("scan")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True,
              help="Dry run: scan only, no actions taken (default: true).")
@click.pass_context
def scan(ctx: click.Context, dry_run: bool) -> None:
    """
    Scan the inbox and produce a pending actions report.

    By default this is a dry run — no emails are deleted or unsubscribed.
    Review output/pending_actions.json and run --execute to apply confirmed actions.
    """
    cfg = ctx.obj["config"]
    _print_banner()

    run_id = _make_run_id()
    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    # ── Auth ──────────────────────────────────────────────────────────────────
    try:
        creds = get_credentials(credentials_path, token_path)
    except FileNotFoundError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    # ── Init services ─────────────────────────────────────────────────────────
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

    # ── Update sender records in DB ───────────────────────────────────────────
    _update_sender_records(db, messages)

    # ── Unsubscribe engine ────────────────────────────────────────────────────
    unsubscribe_candidates = []
    if cfg["unsubscribe"]["enabled"]:
        engine = UnsubscribeEngine(
            db=db,
            min_email_count=cfg["unsubscribe"]["min_email_count"],
            blocked_senders=cfg.get("blocked_senders", []),
        )
        unsubscribe_candidates = engine.get_new_candidates(messages)

    # ── Report ────────────────────────────────────────────────────────────────
    generate_report(
        run_id=run_id,
        emails_scanned=len(messages),
        unsubscribe_candidates=unsubscribe_candidates,
        dry_run=dry_run,
    )

    # ── Log actions to DB (status=pending) ────────────────────────────────────
    for candidate in unsubscribe_candidates:
        import json
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

    # ── Finalise run record ───────────────────────────────────────────────────
    db.complete_run(
        run_id=run_id,
        emails_scanned=len(messages),
        actions_pending=len(unsubscribe_candidates),
        actions_executed=0,
        actions_failed=0,
    )


@cli.command("execute")
@click.option("--session", default="output/pending_actions.json", show_default=True,
              help="Path to the pending_actions.json file from a scan run.")
@click.pass_context
def execute(ctx: click.Context, session: str) -> None:
    """
    Execute confirmed actions from a pending_actions.json file.

    Open the file, set "confirmed": true on the items you want to act on,
    then run this command.
    """
    _print_banner()

    report = load_pending_actions(session)
    if not report:
        sys.exit(1)

    confirmed_unsub = [
        item for item in report.get("unsubscribe", [])
        if item.get("confirmed") is True
    ]

    if not confirmed_unsub:
        console.print(
            "\n  [yellow]No confirmed actions found.[/yellow]\n"
            f'  Open [cyan]{session}[/cyan] and set [cyan]"confirmed": true[/cyan] '
            "on the items you want to process.\n"
        )
        return

    console.print(f"\n  [bold]{len(confirmed_unsub)}[/bold] action(s) confirmed. Executing...\n")

    # Execute phase will be fully implemented in Phase 2 (gmail/executor.py).
    # For now, print what would be executed.
    for item in confirmed_unsub:
        console.print(
            f"  [bold green]→[/bold green] Unsubscribe: [bold]{item['display_name']}[/bold] "
            f"([dim]{item['domain']}[/dim], method={item['method']})"
        )

    console.print(
        "\n  [yellow]Note:[/yellow] Full HTTP/mailto execution will be implemented in Phase 2.\n"
        "  These actions have been logged as 'confirmed' in the database.\n"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _print_banner() -> None:
    console.print()
    console.print("[bold bright_blue]GmailCleaner[/bold bright_blue]  [dim]v0.1.0 · Phase 1[/dim]")
    console.print("[dim]─────────────────────────────────────[/dim]")


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _update_sender_records(db: Database, messages) -> None:
    """
    Upsert sender records in the DB based on the current scan.
    Groups messages by domain and writes aggregate counts.
    """
    from collections import defaultdict, Counter
    by_domain = defaultdict(list)
    for msg in messages:
        if msg.sender_domain:
            by_domain[msg.sender_domain].append(msg)

    for domain, msgs in by_domain.items():
        # Pick most common display name for the domain.
        names = [m.sender_name for m in msgs if m.sender_name]
        display_name = Counter(names).most_common(1)[0][0] if names else None
        db.upsert_sender(
            domain=domain,
            display_name=display_name,
            email_count=len(msgs),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Entry
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()