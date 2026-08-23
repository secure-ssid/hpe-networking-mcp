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


def test_ndcg_perfect_and_imperfect_rankings():
    run_eval = _load_run_eval()
    graded = [
        {"match": "primary-source", "gain": 3},
        {"match": "secondary-source", "gain": 2},
    ]
    perfect = [
        {"source": "a", "text": "from primary-source"},
        {"source": "b", "text": "from secondary-source"},
    ]
    inverted = list(reversed(perfect))

    assert run_eval._ndcg_at_k(perfect, graded, k=5) == 1.0
    assert 0.0 < run_eval._ndcg_at_k(inverted, graded, k=5) < 1.0


def test_ndcg_zero_when_no_graded_source_retrieved():
    run_eval = _load_run_eval()
    graded = [{"match": "primary-source", "gain": 3}]
    hits = [{"source": "unrelated", "text": "nothing relevant"}]

    assert run_eval._ndcg_at_k(hits, graded, k=5) == 0.0


def test_aggregate_reports_ndcg_mean_and_graded_count():
    run_eval = _load_run_eval()
    base = {
        "id": "x", "type": "howto", "source_hit": True, "rank": 1,
        "keyword_hit": True, "mrr": 1.0, "latency_ms": None,
        "latency_hit": None, "duplicate_ratio": None,
        "duplicate_hit": None, "ndcg": None,
    }
    rows = [
        {**base, "id": "g1", "ndcg": 1.0},
        {**base, "id": "g2", "ndcg": 0.5},
        {**base, "id": "plain", "ndcg": None},
    ]

    summary = run_eval._aggregate(rows)

    assert summary["graded_n"] == 2
    assert summary["ndcg@k"] == 0.75


def test_threshold_failures_treat_none_metric_as_zero():
    """An opt-in metric that is legitimately ``None`` (e.g. ``ndcg@k`` with
    zero graded rows) must fail the gate cleanly, not raise ``TypeError``."""
    run_eval = _load_run_eval()

    failures = run_eval._threshold_failures({"ndcg@k": None}, {"ndcg@k": 0.5})

    assert failures == ["ndcg@k=0.000 < 0.500"]


def test_aggregate_summary_keeps_deferred_n_key():
    """Regression guard: a bad line-cut once dropped ``deferred_n`` from the
    summary while leaving a duplicate ``latency_n`` key behind (ruff F601)."""
    run_eval = _load_run_eval()
    row = {
        "id": "x", "type": "howto", "source_hit": True, "rank": 1,
        "keyword_hit": True, "mrr": 1.0, "latency_ms": None,
        "latency_hit": None, "duplicate_ratio": None,
        "duplicate_hit": None, "ndcg": None,
    }

    summary = run_eval._aggregate([row])

    assert "deferred_n" in summary
    keys = [k for k in summary if k != "rows"]
    assert len(keys) == len(set(keys))


def test_graded_sources_declare_positive_gains_and_matches():
    run_eval = _load_run_eval()
    for question in run_eval.load_questions():
        for entry in question.get("graded_sources", []):
            assert int(entry["gain"]) > 0, question["id"]
            assert str(entry["match"]).strip(), question["id"]
