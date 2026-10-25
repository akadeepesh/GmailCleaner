"""
GmailCleaner — Inbox Health Export
Exports the state GmailCleaner has accumulated to CSV files for spreadsheets
or long-term record keeping.

Datasets:
  summary  — headline metrics (senders seen, unsubscribed, triage buckets, …)
  senders  — every sender domain seen, with counts and unsubscribe status
  actions  — full action log (pending / executed / failed / superseded)
  runs     — scan and execute run history

Only metadata already stored in the local DB is exported — never email bodies.
"""

import csv
import io
import logging
from pathlib import Path

from store.db import Database

logger = logging.getLogger(__name__)

DATASETS = ("summary", "senders", "actions", "runs")

_COLUMNS = {
    "summary": ["metric", "value"],
    "senders": [
        "domain", "display_name", "email_count", "first_seen", "last_seen",
        "unsubscribed_at", "unsubscribe_method", "trusted", "blocked",
    ],
    "actions": [
        "id", "run_id", "action_type", "target", "status",
        "created_at", "executed_at", "error", "target_meta",
    ],
    "runs": [
        "id", "mode", "started_at", "completed_at", "emails_scanned",
        "actions_pending", "actions_executed", "actions_failed",
    ],
}


def dataset_rows(db: Database, dataset: str) -> list[dict]:
    """Return the rows for one dataset as a list of dicts."""
    if dataset == "summary":
        summary = db.get_dashboard_summary()
        rows = [{"metric": k, "value": v} for k, v in summary.items()]
        rows += [
            {"metric": f"triage_{bucket.lower()}", "value": count}
            for bucket, count in db.get_triage_counts().items()
        ]
        return rows
    if dataset == "senders":
        return db.list_senders()
    if dataset == "actions":
        return db.list_actions()
    if dataset == "runs":
        return db.get_recent_runs(limit=1_000_000)
    raise ValueError(f"Unknown dataset {dataset!r}. Choose from: {', '.join(DATASETS)}")


def to_csv(db: Database, dataset: str) -> str:
    """Render one dataset as a CSV string (used for dashboard downloads)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS[dataset], extrasaction="ignore")
    writer.writeheader()
    for row in dataset_rows(db, dataset):
        writer.writerow(row)
    return buf.getvalue()


def export_csv(db: Database, out_dir: str) -> list[Path]:
    """
    Write every dataset to <out_dir>/<dataset>.csv.

    Returns:
        Paths of the files written.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for dataset in DATASETS:
        path = directory / f"{dataset}.csv"
        path.write_text(to_csv(db, dataset), encoding="utf-8", newline="")
        written.append(path)
        logger.info("Exported %s → %s", dataset, path)
    return written
