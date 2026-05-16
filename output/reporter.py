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
from engines.bulk_cleanup import CleanupGroup

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
    cleanup_groups: Optional[list[CleanupGroup]] = None,
    dry_run: bool = True,
    output_path: str = PENDING_ACTIONS_PATH,
) -> dict:
    """
    Generate and persist the pending actions report.

    Args:
        run_id:                  Unique identifier for this run.
        emails_scanned:          Total emails processed in the scan.
        unsubscribe_candidates:  Candidates from the unsubscribe engine.
        cleanup_groups:          Groups from the bulk cleanup engine.
        dry_run:                 Whether this is a dry-run (no execute will follow).
        output_path:             Path to write pending_actions.json.

    Returns:
        The report dict (same content as written to disk).
    """
    report = _build_report(run_id, emails_scanned, unsubscribe_candidates,
                           cleanup_groups or [], dry_run)
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
    cleanup_groups: list[CleanupGroup],
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
            "confirmed": False,
        }
        for c in unsubscribe_candidates
        if not c.already_unsubscribed
    ]

    delete_group_items = [
        {
            "group_id": g.group_id,
            "group_type": g.group_type,
            "label": g.label,
            "domain": g.domain,
            "email_count": g.email_count,
            "oldest_date": g.oldest_date,
            "newest_date": g.newest_date,
            "all_unread": g.all_unread,
            "has_unsubscribe": g.has_unsubscribe,
            "score": round(g.score, 3),
            "score_label": g.score_label,
            "sample_subjects": g.sample_subjects,
            "message_ids": g.message_ids,
            "confirmed": False,
        }
        for g in cleanup_groups
    ]

    return {
        "run_id": run_id,
        "generated_at": now,
        "dry_run": dry_run,
        "summary": {
            "emails_scanned": emails_scanned,
            "unsubscribe_candidates": len(unsubscribe_items),
            "cleanup_groups": len(delete_group_items),
            "cleanup_emails_total": sum(g["email_count"] for g in delete_group_items),
        },
        "unsubscribe": unsubscribe_items,
        "delete_groups": delete_group_items,
        "_instructions": (
            "To confirm actions: set 'confirmed': true on each item you want to execute, "
            "then run: python main.py execute --session output/pending_actions.json"
        ),
    }


def _write_json(report: dict, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Pending actions written to %s", output_path)


# ──────────────────────────────────────────────────────────────────────────────
# Console output (Rich)
# ──────────────────────────────────────────────────────────────────────────────

def _print_console_report(report: dict, dry_run: bool) -> None:
    """Print a structured summary to the terminal using Rich."""
    console.print()

    mode_label = "[yellow]DRY RUN[/yellow]" if dry_run else "[green]SCAN COMPLETE[/green]"
    console.print(
        Panel(
            f"[bold white]GmailCleaner[/bold white]  ·  {mode_label}\n"
            f"[dim]Run ID: {report['run_id']}[/dim]",
            border_style="bright_blue",
            padding=(1, 4),
        )
    )

    summary = report["summary"]
    console.print()
    console.print(f"  [dim]Emails scanned:[/dim]          [bold]{summary['emails_scanned']:,}[/bold]")
    console.print(f"  [dim]Unsubscribe candidates:[/dim]  [bold]{summary['unsubscribe_candidates']}[/bold]")
    console.print(f"  [dim]Cleanup groups:[/dim]          [bold]{summary['cleanup_groups']}[/bold]"
                  f"  [dim]({summary['cleanup_emails_total']:,} emails)[/dim]")
    console.print()

    unsubscribe_items = report.get("unsubscribe", [])
    if unsubscribe_items:
        _print_unsubscribe_table(unsubscribe_items)
    else:
        console.print("  [dim]No new unsubscribe candidates found.[/dim]")
        console.print()

    delete_groups = report.get("delete_groups", [])
    if delete_groups:
        _print_cleanup_table(delete_groups)
    else:
        console.print("  [dim]No bulk cleanup groups found.[/dim]")
        console.print()

    if dry_run:
        console.print(
            Panel(
                "[bold]Next steps[/bold]\n\n"
                f"  1. Open [cyan]output/pending_actions.json[/cyan]\n"
                f'  2. Set [cyan]"confirmed": true[/cyan] on each item you want to action\n'
                f"  3. Run: [bold green]python main.py execute --session output/pending_actions.json[/bold green]",
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
        caption=f"{len(items)} sender(s) — review and confirm in pending_actions.json",
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


def _print_cleanup_table(items: list[dict]) -> None:
    table = Table(
        box=box.ROUNDED,
        border_style="dark_orange",
        header_style="bold yellow",
        show_lines=True,
        padding=(0, 1),
        title="[bold]Bulk Cleanup Groups[/bold]",
        title_style="bold white",
        caption=f"{len(items)} group(s) — review and confirm in pending_actions.json",
        caption_style="dim",
    )

    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Group", min_width=28)
    table.add_column("Emails", width=8, justify="right")
    table.add_column("Oldest", width=12)
    table.add_column("Risk", width=8)
    table.add_column("Flags", width=20)
    table.add_column("Sample Subjects", min_width=30, overflow="fold")

    SCORE_COLORS = {"high": "red", "medium": "yellow", "low": "green"}

    for i, item in enumerate(items, start=1):
        score_label = item["score_label"]
        score_colored = f"[{SCORE_COLORS[score_label]}]{score_label.upper()}[/{SCORE_COLORS[score_label]}]"

        flags = []
        if item["all_unread"]:
            flags.append("[dim]never opened[/dim]")
        if item["has_unsubscribe"]:
            flags.append("[dim]promotional[/dim]")

        type_icon = "🏷" if item["group_type"] == "domain" else "📄"
        subjects_preview = " · ".join(item["sample_subjects"][:2])

        table.add_row(
            str(i),
            f"{type_icon} [bold]{item['label'][:36]}[/bold]\n"
            f"[dim]{item['group_type']} group[/dim]",
            str(item["email_count"]),
            item["oldest_date"],
            score_colored,
            "\n".join(flags) if flags else "[dim]—[/dim]",
            f"[dim]{subjects_preview[:60]}[/dim]",
        )

    console.print(table)
    console.print()