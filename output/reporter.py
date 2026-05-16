"""
GmailCleaner — Reporter
Produces two outputs at the end of each scan:

  1. pending_actions.json — machine-readable, consumed by the execute phase.
  2. Rich console report  — human-readable summary printed to stdout.

The reporter is purely a write-only layer. It does not call any Gmail APIs
or modify any state. All inputs are pre-computed by the engines.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

from engines.unsubscribe import UnsubscribeCandidate

logger = logging.getLogger(__name__)

# Output path for the pending actions file.
PENDING_ACTIONS_PATH = "output/pending_actions.json"

console = Console()


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def generate_report(
    run_id: str,
    emails_scanned: int,
    unsubscribe_candidates: list[UnsubscribeCandidate],
    dry_run: bool = True,
    output_path: str = PENDING_ACTIONS_PATH,
) -> dict:
    """
    Generate and persist the pending actions report.

    Args:
        run_id:                  Unique identifier for this run.
        emails_scanned:          Total emails processed in the scan.
        unsubscribe_candidates:  Candidates from the unsubscribe engine.
        dry_run:                 Whether this is a dry-run (no execute will follow).
        output_path:             Path to write pending_actions.json.

    Returns:
        The report dict (same content as written to disk).
    """
    report = _build_report(run_id, emails_scanned, unsubscribe_candidates, dry_run)
    _write_json(report, output_path)
    _print_console_report(report, dry_run)
    return report


def load_pending_actions(path: str = PENDING_ACTIONS_PATH) -> Optional[dict]:
    """
    Load a previously generated pending_actions.json for the execute phase.

    Returns None if the file does not exist or is malformed.
    """
    file = Path(path)
    if not file.exists():
        logger.error("No pending actions file found at %s. Run a scan first.", path)
        return None
    try:
        data = json.loads(file.read_text())
        logger.info("Loaded pending actions: run_id=%s", data.get("run_id"))
        return data
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse pending_actions.json: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Report builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_report(
    run_id: str,
    emails_scanned: int,
    unsubscribe_candidates: list[UnsubscribeCandidate],
    dry_run: bool,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    unsubscribe_items = [
        {
            "domain": c.domain,
            "display_name": c.display_name,
            "sender_email": c.sender_email,
            "email_count": c.email_count,
            "most_recent_date": c.most_recent_date,
            "representative_subject": c.representative_subject,
            "method": c.method_label,
            "http_url": c.unsubscribe.http_url,
            "mailto": c.unsubscribe.mailto,
            "supports_one_click": c.unsubscribe.supports_one_click,
            "sample_message_ids": c.sample_message_ids,
            "confirmed": False,  # User sets this to true before running --execute
        }
        for c in unsubscribe_candidates
        if not c.already_unsubscribed
    ]

    return {
        "run_id": run_id,
        "generated_at": now,
        "dry_run": dry_run,
        "summary": {
            "emails_scanned": emails_scanned,
            "unsubscribe_candidates": len(unsubscribe_items),
        },
        "unsubscribe": unsubscribe_items,
        "_instructions": (
            "To confirm actions: set 'confirmed': true on each item you want to execute, "
            "then run: python main.py --execute --session output/pending_actions.json"
        ),
    }


def _write_json(report: dict, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    logger.info("Pending actions written to %s", output_path)


# ──────────────────────────────────────────────────────────────────────────────
# Console output (Rich)
# ──────────────────────────────────────────────────────────────────────────────

def _print_console_report(report: dict, dry_run: bool) -> None:
    """Print a structured summary to the terminal using Rich."""
    console.print()

    # Header
    mode_label = "[yellow]DRY RUN[/yellow]" if dry_run else "[green]SCAN COMPLETE[/green]"
    console.print(
        Panel(
            f"[bold white]GmailCleaner[/bold white]  ·  {mode_label}\n"
            f"[dim]Run ID: {report['run_id']}[/dim]",
            border_style="bright_blue",
            padding=(1, 4),
        )
    )

    # Summary row
    summary = report["summary"]
    console.print()
    console.print(f"  [dim]Emails scanned:[/dim]          [bold]{summary['emails_scanned']:,}[/bold]")
    console.print(f"  [dim]Unsubscribe candidates:[/dim]  [bold]{summary['unsubscribe_candidates']}[/bold]")
    console.print()

    # Unsubscribe candidates table
    unsubscribe_items = report.get("unsubscribe", [])
    if unsubscribe_items:
        _print_unsubscribe_table(unsubscribe_items)
    else:
        console.print("  [dim]No new unsubscribe candidates found.[/dim]")
        console.print()

    # Footer instructions
    if dry_run:
        console.print(
            Panel(
                "[bold]Next steps[/bold]\n\n"
                f"  1. Open [cyan]output/pending_actions.json[/cyan]\n"
                f'  2. Set [cyan]"confirmed": true[/cyan] on the senders you want to unsubscribe from\n'
                f"  3. Run: [bold green]python main.py --execute --session output/pending_actions.json[/bold green]",
                border_style="dim",
                padding=(1, 4),
            )
        )
    console.print()


def _print_unsubscribe_table(items: list[dict]) -> None:
    table = Table(
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=True,
        padding=(0, 1),
        title="[bold]Unsubscribe Candidates[/bold]",
        title_style="bold white",
        caption=f"{len(items)} sender(s) found — review and confirm in pending_actions.json",
        caption_style="dim",
    )

    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Sender", min_width=22)
    table.add_column("Emails", width=8, justify="right")
    table.add_column("Most Recent", width=12)
    table.add_column("Method", width=18)
    table.add_column("Last Subject", min_width=30, overflow="fold")

    for i, item in enumerate(items, start=1):
        method = item["method"]
        if "One-click" in method:
            method_styled = f"[green]{method}[/green]"
        elif method == "HTTP":
            method_styled = f"[cyan]{method}[/cyan]"
        else:
            method_styled = f"[yellow]{method}[/yellow]"

        table.add_row(
            str(i),
            f"[bold]{item['display_name']}[/bold]\n[dim]{item['domain']}[/dim]",
            str(item["email_count"]),
            item["most_recent_date"],
            method_styled,
            f"[dim]{item['representative_subject'][:60]}[/dim]",
        )

    console.print(table)
    console.print()