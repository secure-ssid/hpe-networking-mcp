from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_run_eval():
    path = Path(__file__).resolve().parent / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_duplicate_ratio_flags_repeated_file_paths():
    run_eval = _load_run_eval()
    hits = [
        {"source": "vsg_docs", "file_path": "vsg_docs/glp.md", "text": "workspace one"},
        {"source": "vsg_docs", "file_path": "vsg_docs/glp.md", "text": "workspace two"},
        {"source": "techdocs_html", "file_path": "techdocs_html/proxy.html", "text": "proxy"},
    ]

    assert run_eval._duplicate_ratio(hits, k=3) == 0.333


def test_aggregate_reports_optional_duplicate_and_latency_guards():
    run_eval = _load_run_eval()
    rows = [
        {
            "id": "dup-pass",
            "type": "howto",
            "source_hit": True,
            "rank": 1,
            "keyword_hit": True,
            "mrr": 1.0,
            "latency_ms": 250.0,
            "latency_hit": True,
            "duplicate_ratio": 0.0,
            "duplicate_hit": True,
        },
        {
            "id": "dup-fail",
            "type": "howto",
            "source_hit": True,
            "rank": 1,
            "keyword_hit": True,
            "mrr": 1.0,
            "latency_ms": 4500.0,
            "latency_hit": False,
            "duplicate_ratio": 0.333,
            "duplicate_hit": False,
        },
    ]

    summary = run_eval._aggregate(rows)

    assert summary["duplicate_n"] == 2
    assert summary["duplicate_guard"] == 0.5
    assert summary["latency_n"] == 2
    assert summary["latency_guard"] == 0.5
