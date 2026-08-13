from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hpe_networking_mcp.pipeline import reporter


class _State:
    def get_all_stage_statuses(self, serial, run_id):
        return {
            stage: (
                "success"
                if stage in {"s1_discover", "s2_validate", "s8_verify"}
                else "skipped"
            )
            for stage in reporter.STAGES
        }

    def get_stage_data(self, serial, run_id, stage):
        if stage == "s8_verify":
            return {
                "is_provisioned": True,
                "final_firmware": "10.15.1",
                "site_id": "site-1",
            }
        return {}


def _record():
    return SimpleNamespace(
        serial_number="CN123",
        source_type=SimpleNamespace(value="central"),
        target_account=SimpleNamespace(value="target"),
        site_id="site-fallback",
        notes="<review>",
    )


def test_write_reports_emits_csv_json_and_escaped_html(tmp_path):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=12.5)

    paths = reporter.write_reports(
        [_record()],
        "run-1",
        _State(),
        output_dir=str(tmp_path),
        started_at=started,
        ended_at=ended,
        formats=("csv", "json", "html"),
    )

    assert set(paths) == {"csv", "json", "html"}
    payload = json.loads((tmp_path / paths["json"].split("/")[-1]).read_text())
    assert payload["schema_version"] == 1
    assert payload["devices"][0]["duration_seconds"] == 12.5
    assert payload["devices"][0]["overall_status"] == "done"
    html_text = (tmp_path / paths["html"].split("/")[-1]).read_text()
    assert "&lt;review&gt;" in html_text
    assert "<review>" not in html_text


def test_write_reports_rejects_unknown_or_empty_formats(tmp_path):
    with pytest.raises(ValueError, match="unsupported report"):
        reporter.write_reports(
            [_record()],
            "run-1",
            _State(),
            output_dir=str(tmp_path),
            formats=("pdf",),
        )
    with pytest.raises(ValueError, match="at least one"):
        reporter.write_reports(
            [_record()],
            "run-1",
            _State(),
            output_dir=str(tmp_path),
            formats=(),
        )
