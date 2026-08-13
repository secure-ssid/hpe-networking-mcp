from __future__ import annotations

import pytest

from ingestion import ingest_docs


def test_parallel_workers_are_disabled_on_macos(monkeypatch, capsys):
    monkeypatch.setattr(ingest_docs.sys, "platform", "darwin")

    assert ingest_docs.safe_parallel_workers(4) is None
    assert "disabling fastembed multiprocessing" in capsys.readouterr().out


def test_parallel_workers_are_kept_on_linux(monkeypatch):
    monkeypatch.setattr(ingest_docs.sys, "platform", "linux")

    assert ingest_docs.safe_parallel_workers(4) == 4


def test_parallel_workers_reject_non_positive_values():
    with pytest.raises(ValueError, match="at least 1"):
        ingest_docs.safe_parallel_workers(0)


def test_openapi_specs_use_structured_index_for_lancedb():
    assert ingest_docs.source_uses_structured_index("openapi_specs", "lancedb") is True
    assert ingest_docs.source_uses_structured_index("openapi_specs", "redis") is False
    assert ingest_docs.source_uses_structured_index("developer_docs", "lancedb") is False
