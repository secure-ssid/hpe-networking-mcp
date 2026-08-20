"""Unit tests for ingestion.scrape_report — complete vs partial scrape status."""

from __future__ import annotations

import json

from ingestion.scrape_report import write_scrape_report


def test_complete_report_has_no_errors(tmp_path):
    inventory = tmp_path / "urls.json"
    inventory.write_text('["https://example.test/a"]\n', encoding="utf-8")

    path = write_scrape_report(
        "junos_cli",
        inventory_path=inventory,
        document_count=1,
        ok=1,
        skipped=0,
        report_dir=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "junos_cli.json"
    assert payload["complete"] is True
    assert payload["document_count"] == 1
    assert payload["ok"] == 1
    assert payload["error_count"] == 0
    assert payload["parser_error_count"] == 0
    assert payload["inventory_sha256"]


def test_parser_or_fetch_errors_mark_report_incomplete(tmp_path):
    path = write_scrape_report(
        "mist_api_docs",
        document_count=10,
        ok=8,
        skipped=0,
        errors=["ERROR $h/api: disk full"],
        parser_errors=["PARSER_ERROR $h/ws: missing title"],
        report_dir=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["complete"] is False
    assert payload["error_count"] == 1
    assert payload["parser_error_count"] == 1
    assert payload["ok"] == 8
    assert "PARSER_ERROR" in payload["parser_errors"][0]
