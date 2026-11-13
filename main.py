"""
GmailCleaner — Main Entrypoint
CLI interface for running scans and executing confirmed actions.

Commands:
    python main.py info
    python main.py scan
    python main.py scan --no-dry-run
    python main.py execute --session output/pending_actions.json
    python main.py export --out exports/
    python main.py setup-watch

Global options:
    --account NAME   Manage a separate Gmail account with its own token, DB and report.
    --config PATH    Use a different settings.yaml.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from dotenv import load_dotenv  # type: ignore
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

load_dotenv()
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Legacy code pages (e.g. cp1252 when piping on Windows) can't encode the
# box-drawing characters used in console output; degrade instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

from auth.oauth import load_credentials
from core.pipeline import FetchFailed, PipelineError, run_execute, run_scan
from core.settings import load_config
from engines.rules import TrustedRules
from gmail.client import GmailClient
from gmail.executor import confirmed_items
from gmail.fetcher import GmailFetcher  # noqa: F401  (patched by tests; used via the pipeline)
from output.exporter import export_csv
from output.reporter import load_pending_actions
from store.db import Database
from version import __version__

console = Console()
logger = logging.getLogger("gmailcleaner")


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO", log_cfg: dict | None = None) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [
        RichHandler(
            console=console,
            rich_tracebacks=True,
            show_path=False,
            markup=True,
        )
    ]

    log_cfg = log_cfg or {}
    # Cloud Run captures stdout; a log file inside the container would be lost.
    on_cloud_run = bool(os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"))
    if log_cfg.get("file_enabled") and not on_cloud_run:
        log_path = Path(log_cfg.get("file_path", "logs/gmailcleaner.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=handlers,
        force=True,
    )
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(__version__, prog_name="GmailCleaner")
@click.option("--config", default=None, help="Path to settings.yaml (default: config/settings.yaml)")
@click.option("--account", default=None, envvar="GMAILCLEANER_ACCOUNT",
              help="Account name for multi-account use (separate token, DB and report).")
@click.option("--log-level", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)")
@click.pass_context
def cli(ctx: click.Context, config: str, account: str, log_level: str) -> None:
    """GmailCleaner — Self-hosted Gmail intelligence engine."""
    try:
        cfg = load_config(config, account=account)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))
    log_cfg = cfg.get("logging", {})
    setup_logging(log_level or os.getenv("LOG_LEVEL") or log_cfg.get("level", "INFO"), log_cfg)
    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg


# ── info ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show authenticated account info and config summary (runs OAuth on first use)."""
    cfg = ctx.obj["config"]
    _print_banner(cfg)

    try:
        client = GmailClient(load_credentials(cfg))
        profile = client.get_profile()
    except FileNotFoundError as exc:
        console.print(f"\n  [red]✗ {exc}[/red]\n")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n  [red]✗ Authentication failed: {exc}[/red]\n")
        sys.exit(1)

    rules = TrustedRules.from_config(cfg)
    console.print(f"\n  [bold green]✓[/bold green] Authenticated as [bold]{profile.get('emailAddress')}[/bold]")
    console.print(f"  Messages total: [bold]{_fmt_count(profile.get('messagesTotal'))}[/bold]")
    console.print(f"  Threads total:  [bold]{_fmt_count(profile.get('threadsTotal'))}[/bold]")
    console.print()
    console.print(f"  [dim]Scan window:[/dim]     {cfg['scan']['lookback_days']} days")
    console.print(f"  [dim]Max emails:[/dim]      {cfg['scan']['max_emails']:,}")
    console.print(f"  [dim]Unsubscribe:[/dim]     {_on_off(cfg['unsubscribe']['enabled'])}")
    console.print(f"  [dim]Bulk cleanup:[/dim]    {_on_off(cfg['bulk_cleanup']['enabled'])}")
    console.print(f"  [dim]AI triage:[/dim]       {_on_off(cfg['triage']['enabled'])}")
    console.print(f"  [dim]Daily digest:[/dim]    {_on_off(cfg['digest']['enabled'])}")
    console.print(f"  [dim]Confirm first:[/dim]   {_on_off(cfg.get('confirm_first', True))}"
                  + (f"  [yellow]({len(rules.rules)} trusted rule(s) active)[/yellow]" if rules.active else ""))
    console.print(f"  [dim]Database:[/dim]        {cfg['database']['path']}")
    console.print(f"  [dim]Pending report:[/dim]  {cfg['output']['pending_actions_path']}")
    console.print()


# ── scan ──────────────────────────────────────────────────────────────────────

@cli.command("scan")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True,
              help="Dry run: produce the report only. With --no-dry-run, actions "
                   "matched by trusted rules are executed (requires confirm_first: false).")
@click.pass_context
def scan(ctx: click.Context, dry_run: bool) -> None:
    """
    Scan the inbox and produce a pending actions report.

    Runs the unsubscribe, bulk cleanup and triage engines, sends the digest,
    then writes the pending actions report for your review. Nothing is
    unsubscribed or trashed unless you confirm it — or, with --no-dry-run,
    it matches one of your trusted_rules.
    """
    cfg = ctx.obj["config"]
    _print_banner(cfg)

    # ── Auth ──────────────────────────────────────────────────────────────────
    try:
        creds = load_credentials(cfg)
    except (FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    def _announce_auto_execute(count: int) -> None:
        console.print(f"  [cyan]→[/cyan] Executing {count} action(s) approved by trusted rules\n")

    try:
        result = run_scan(
            cfg,
            dry_run=dry_run,
            client=GmailClient(creds),
            on_result=_print_result,
            on_auto_execute=_announce_auto_execute,
        )
    except FetchFailed:
        sys.exit(1)  # already logged by the pipeline
    except PipelineError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    if result.emails_scanned == 0:
        console.print("\n  [yellow]No messages found in the scan window.[/yellow]\n")
    elif result.auto_confirmed and dry_run:
        console.print(
            f"  [yellow]{result.auto_confirmed} action(s) match trusted rules and were "
            "auto-confirmed. Re-run with --no-dry-run to execute them automatically.[/yellow]\n"
        )


# ── execute ───────────────────────────────────────────────────────────────────

@cli.command("execute")
@click.option("--session", default=None,
              help="Path to the pending actions report (default: the configured report path).")
@click.option("--yes", "-y", is_flag=True, help="Skip the interactive confirmation prompt.")
@click.pass_context
def execute(ctx: click.Context, session: str | None, yes: bool) -> None:
    """
    Execute confirmed actions from a pending actions report.

    Set "confirmed": true on the items you want to process (in the JSON file
    or on the dashboard's Pending page), then run this command. Confirmed
    unsubscribes are sent and confirmed cleanup groups are moved to Trash.
    """
    cfg = ctx.obj["config"]
    _print_banner(cfg)
    session = session or cfg["output"]["pending_actions_path"]

    report = load_pending_actions(session)
    if not report:
        sys.exit(1)

    confirmed_unsub, confirmed_delete = confirmed_items(report)
    if not confirmed_unsub and not confirmed_delete:
        console.print(
            "\n  [yellow]No confirmed actions found.[/yellow]\n"
            f'  Open [cyan]{session}[/cyan] and set [cyan]"confirmed": true[/cyan] '
            "on the items you want to process.\n"
        )
        return

    trash_total = sum(item.get("email_count", 0) for item in confirmed_delete)
    console.print(
        f"\n  [bold]{len(confirmed_unsub)}[/bold] unsubscribe(s) confirmed  "
        f"[bold]{len(confirmed_delete)}[/bold] cleanup group(s) confirmed "
        f"([dim]{trash_total:,} emails → Trash[/dim])\n"
    )
    if not yes and sys.stdin.isatty():
        click.confirm("  Proceed?", default=True, abort=True)

    # ── Auth & init ───────────────────────────────────────────────────────────
    try:
        creds = load_credentials(cfg)
    except (FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        summary = run_execute(
            cfg,
            report_path=session,
            client=GmailClient(creds),
            on_result=_print_result,
        )
    except PipelineError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            f"[bold]Execution complete[/bold]\n\n"
            f"  Unsubscribed:     [green]{summary.total_unsubscribed}[/green] / {len(confirmed_unsub)}\n"
            f"  Moved to Trash:   [green]{summary.total_deleted:,}[/green] emails\n"
            f"  Failed:           "
            + (f"[red]{summary.total_failed}[/red]" if summary.total_failed else "0"),
            border_style="green" if summary.total_failed == 0 else "yellow",
            padding=(1, 4),
        )
    )


# ── export ────────────────────────────────────────────────────────────────────

@cli.command("export")
@click.option("--out", "out_dir", default=None,
              help="Output directory (default: exports/<YYYY-MM-DD>).")
@click.pass_context
def export(ctx: click.Context, out_dir: str | None) -> None:
    """Export an inbox health report (summary, senders, actions, runs) as CSV."""
    cfg = ctx.obj["config"]
    _print_banner(cfg)

    exports_root = cfg.get("output", {}).get("exports_dir") or "exports"
    out_dir = out_dir or str(Path(exports_root) / datetime.now().strftime("%Y-%m-%d"))
    db = Database(cfg["database"]["path"])
    paths = export_csv(db, out_dir)

    console.print(f"\n  [bold green]✓[/bold green] Exported {len(paths)} file(s) to [cyan]{out_dir}[/cyan]")
    for path in paths:
        console.print(f"    [dim]{path.name}[/dim]")
    console.print()


# ── setup-watch ───────────────────────────────────────────────────────────────

@cli.command("setup-watch")
@click.pass_context
def setup_watch(ctx: click.Context) -> None:
    """
    Register the Gmail Pub/Sub watch for real-time inbox notifications.

    Run once after deploying the dashboard service. Gmail expires watches
    after 7 days; Cloud Scheduler renews it daily via POST /api/watch/renew.
    """
    cfg = ctx.obj["config"]
    _print_banner(cfg)

    topic = cfg.get("pubsub", {}).get("topic_name", "")
    if not topic:
        console.print("[red]✗ pubsub.topic_name is not set (settings.yaml or PUBSUB_TOPIC)[/red]")
        sys.exit(1)

    try:
        creds = load_credentials(cfg)
    except (FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    client = GmailClient(creds)
    db = Database(cfg["database"]["path"])

    from pubsub.watcher import WatchManager, _format_expiry
    manager = WatchManager(client, db, topic_name=topic)

    try:
        reg = manager.register()
    except RuntimeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    # Store baseline historyId so the first webhook call has a starting point.
    db.save_history_id(reg.history_id)
    console.print(
        Panel(
            f"[bold green]✓ Gmail watch registered[/bold green]\n\n"
            f"  Topic:      [cyan]{reg.topic_name}[/cyan]\n"
            f"  historyId:  [dim]{reg.history_id}[/dim]\n"
            f"  Expires:    [dim]{_format_expiry(reg.expiration_ms)}[/dim]",
            border_style="green",
            padding=(1, 4),
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _print_banner(cfg: dict | None = None) -> None:
    account = (cfg or {}).get("account")
    suffix = f" · account: {account}" if account else ""
    console.print()
    console.print(f"[bold bright_blue]GmailCleaner[/bold bright_blue]  [dim]v{__version__}{suffix}[/dim]")
    console.print("[dim]─────────────────────────────────────[/dim]")


def _print_result(action_type: str, item: dict, result) -> None:
    if action_type == "unsubscribe":
        label = f"Unsubscribe [bold]{item.get('display_name') or item['domain']}[/bold] ([dim]{item['domain']}[/dim])"
        status = "[green]✓[/green]" if result.success else f"[red]✗ {result.error}[/red]"
    else:
        label = f"Trash [bold]{item.get('label', item['group_id'])}[/bold] ([dim]{item.get('email_count', 0)} emails[/dim])"
        if result.failed == 0:
            status = f"[green]✓ {result.deleted:,} moved to Trash[/green]"
        else:
            status = f"[yellow]⚠ {result.deleted} moved, {result.failed} failed[/yellow]"
    console.print(f"  [cyan]→[/cyan] {label}  {status}")


def _fmt_count(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "N/A"


def _on_off(flag) -> str:
    return "enabled" if flag else "disabled"


# ──────────────────────────────────────────────────────────────────────────────
# Entry
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
