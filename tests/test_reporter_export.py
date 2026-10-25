"""
Tests for output/reporter.py persistence helpers and output/exporter.py

Covers:
  - save/load round-trip with non-ASCII content (UTF-8 on every platform)
  - load_pending_actions: missing file and malformed JSON return None
  - merge_unsubscribe_candidates: adds new domains, skips known ones, no-op without a report
  - generate_report writes to the requested path and embeds it in the instructions
  - CSV export: one file per dataset with headers and rows
"""

import csv
import json

from engines.unsubscribe import UnsubscribeCandidate
from gmail.parser import UnsubscribeTarget
from output.exporter import DATASETS, export_csv, to_csv
from output.reporter import (
    generate_report,
    load_pending_actions,
    merge_unsubscribe_candidates,
    save_pending_actions,
)
from store.db import Database


def _candidate(domain: str) -> UnsubscribeCandidate:
    return UnsubscribeCandidate(
        domain=domain,
        display_name=domain.split(".")[0].title(),
        sender_email=f"hi@{domain}",
        email_count=3,
        unsubscribe=UnsubscribeTarget(http_url=f"https://{domain}/u", supports_one_click=True),
        representative_subject="Hello",
        most_recent_date="2024-05-16",
    )


class TestPersistence:
    def test_round_trip_utf8(self, tmp_path):
        path = tmp_path / "nested" / "p.json"
        report = {"run_id": "r1", "unsubscribe": [{"display_name": "Café Ünïcode ✉"}]}
        save_pending_actions(report, str(path))
        assert load_pending_actions(str(path)) == report
        assert not list(path.parent.glob(".pending-*"))  # temp file cleaned up

    def test_missing_returns_none(self, tmp_path):
        assert load_pending_actions(str(tmp_path / "nope.json"), missing_ok=True) is None

    def test_malformed_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_pending_actions(str(path)) is None


class TestMerge:
    def test_adds_new_domains_only(self, tmp_path):
        path = str(tmp_path / "p.json")
        save_pending_actions({"run_id": "r1", "unsubscribe": [{"domain": "a.com"}], "summary": {}}, path)

        added = merge_unsubscribe_candidates([_candidate("a.com"), _candidate("b.com")], path)

        assert added == 1
        report = load_pending_actions(path)
        assert [i["domain"] for i in report["unsubscribe"]] == ["a.com", "b.com"]
        assert report["unsubscribe"][1]["confirmed"] is False
        assert report["summary"]["unsubscribe_candidates"] == 2

    def test_no_report_is_noop(self, tmp_path):
        path = tmp_path / "p.json"
        assert merge_unsubscribe_candidates([_candidate("a.com")], str(path)) == 0
        assert not path.exists()


class TestGenerateReport:
    def test_writes_to_custom_path(self, tmp_path):
        path = tmp_path / "acct" / "pending.json"
        report = generate_report(
            run_id="r1",
            emails_scanned=10,
            unsubscribe_candidates=[_candidate("a.com")],
            output_path=str(path),
        )
        assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "r1"
        assert str(path) in report["_instructions"]


class TestExport:
    def test_export_all_datasets(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.upsert_sender("news.example.com", "News", 12)
        db.create_run("r1", mode="scan")
        db.log_action("r1", "unsubscribe", "news.example.com")

        paths = export_csv(db, str(tmp_path / "out"))

        assert sorted(p.stem for p in paths) == sorted(DATASETS)
        with open(tmp_path / "out" / "senders.csv", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["domain"] == "news.example.com"
        assert rows[0]["email_count"] == "12"

        with open(tmp_path / "out" / "summary.csv", encoding="utf-8", newline="") as f:
            metrics = {r["metric"]: r["value"] for r in csv.DictReader(f)}
        assert metrics["total_senders"] == "1"
        assert metrics["pending_actions"] == "1"
        assert "triage_needs_reply" in metrics

    def test_empty_db_has_headers(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        assert to_csv(db, "actions").splitlines()[0].startswith("id,run_id,action_type")
