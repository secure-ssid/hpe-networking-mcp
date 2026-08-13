from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_run_eval():
    path = Path(__file__).resolve().parents[1] / "eval" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_threshold_failures_reports_underperforming_metrics():
    run_eval = _load_run_eval()
    summary = {
        "source_hit@k": 0.9,
        "mrr": 0.7,
        "howto_recall@k": 1.0,
        "api_exact": 0.5,
        "structured_exact": 1.0,
    }

    failures = run_eval._threshold_failures(
        summary,
        {"source_hit@k": 0.85, "mrr": 0.85, "api_exact": 0.95},
    )

    assert failures == ["mrr=0.700 < 0.850", "api_exact=0.500 < 0.950"]


def test_threshold_failures_passes_when_metrics_meet_thresholds():
    run_eval = _load_run_eval()
    summary = {
        "source_hit@k": 0.9,
        "mrr": 0.9,
        "howto_recall@k": 0.9,
        "api_exact": 1.0,
        "structured_exact": 1.0,
        "structured_list_exact": 1.0,
    }

    assert run_eval._threshold_failures(summary, run_eval._DEFAULT_THRESHOLDS) == []
