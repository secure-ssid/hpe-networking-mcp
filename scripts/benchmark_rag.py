#!/usr/bin/env python3
"""Benchmark the local LanceDB RAG query path.

This benchmark deliberately uses the embedded LanceDB/fastembed path only. It
does not call Central, GLP, or any other network API, and it does not require
Milvus. A "cold" sample is the first query with a fresh lazy embedder; warm
samples reuse that embedder and database connection.

Usage:
    uv run python scripts/benchmark_rag.py
    uv run python scripts/benchmark_rag.py --warm-runs 5 --json benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass(frozen=True)
class BenchmarkCase:
    """A representative query shape from the RAG evaluation plan."""

    name: str
    kind: str
    query: str
    source_filter: str | tuple[str, ...] | None = None


DEFAULT_CASES = (
    BenchmarkCase(
        name="broad",
        kind="broad",
        query="How do I configure a wireless network?",
    ),
    BenchmarkCase(
        name="source_filtered",
        kind="source-filtered",
        query="How do I configure a wireless network?",
        source_filter="developer_docs",
    ),
    BenchmarkCase(
        name="exact_like",
        kind="exact-like",
        query="WPA3_SAE",
    ),
)


def percentile(values: Iterable[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    sample = sorted(float(value) for value in values)
    if not sample:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")
    position = (len(sample) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sample) - 1)
    fraction = position - lower
    return sample[lower] + (sample[upper] - sample[lower]) * fraction


def summarize(values: Iterable[float]) -> dict[str, float | int]:
    """Summarize a timing sample with stable, JSON-friendly fields."""
    sample = [float(value) for value in values]
    if not sample:
        raise ValueError("cannot summarize an empty sample")
    return {
        "n": len(sample),
        "min_ms": round(min(sample), 3),
        "mean_ms": round(sum(sample) / len(sample), 3),
        "p50_ms": round(percentile(sample, 0.50), 3),
        "p95_ms": round(percentile(sample, 0.95), 3),
        "max_ms": round(max(sample), 3),
    }


def _query_once(
    *,
    db: Any,
    embedder: Any,
    lance_client: Any,
    rag: Any,
    case: BenchmarkCase,
    top_k: int,
    vector_cache: dict[str, list[float]] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run the same stages as ``rag._search_lancedb`` with timings attached.

    ``vector_cache`` mirrors the production embedding cache so warm samples
    measure the actual steady-state query path rather than repeatedly timing
    the same model inference.
    """
    started = clock()

    stage_started = clock()
    cache_key = case.query.strip().casefold()
    embedding_cache_hit = vector_cache is not None and cache_key in vector_cache
    if embedding_cache_hit:
        query_vector = vector_cache[cache_key]
    else:
        query_vector = embedder.embed_query(case.query)
        if vector_cache is not None:
            vector_cache[cache_key] = query_vector
    embedding_ms = (clock() - stage_started) * 1000

    stage_started = clock()
    hits = lance_client.hybrid_search(
        db,
        case.query,
        query_vector,
        top_k=max(top_k * 6, 30),
        source_filter=case.source_filter,
    )
    lancedb_hybrid_ms = (clock() - stage_started) * 1000

    stage_started = clock()
    hits = rag._boost_sources(hits, case.query)
    hits = rag._boost_model_match(hits, case.query)
    reranking_ms = (clock() - stage_started) * 1000

    stage_started = clock()
    results = rag._shape(hits, top_k)
    shaping_ms = (clock() - stage_started) * 1000

    return {
        "total_ms": round((clock() - started) * 1000, 3),
        "embedding_cache_hit": embedding_cache_hit,
        "stages": {
            "embedding_ms": round(embedding_ms, 3),
            "lancedb_hybrid_ms": round(lancedb_hybrid_ms, 3),
            "reranking_ms": round(reranking_ms, 3),
            "shaping_ms": round(shaping_ms, 3),
        },
        "result_count": len(results),
    }


def benchmark_case(
    case: BenchmarkCase,
    *,
    data_dir: Path,
    top_k: int,
    warm_runs: int,
) -> dict[str, Any]:
    """Collect one cold sample and repeated warm samples for a case."""
    # Set this before importing rag: importing that module initializes the
    # selected backend, so rejecting Redis here guarantees a local-only run.
    backend = os.environ.get("HPE_MCP_RAG_BACKEND", "").strip().lower()
    if backend not in ("", "lancedb"):
        raise ValueError(
            "benchmark_rag.py only supports HPE_MCP_RAG_BACKEND=lancedb; "
            "it never contacts Redis or a network API"
        )
    os.environ["HPE_MCP_RAG_BACKEND"] = "lancedb"
    # fastembed normally downloads its ONNX model on first use. Keep this
    # benchmark strictly offline: provision the model cache separately.
    os.environ["HF_HUB_OFFLINE"] = "1"

    from hpe_networking_mcp.mcp_servers import rag
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    db = lance_client.connect(data_dir)
    embedder = EmbedClient()
    vector_cache: dict[str, list[float]] = {}
    cold = _query_once(
        db=db,
        embedder=embedder,
        lance_client=lance_client,
        rag=rag,
        case=case,
        top_k=top_k,
        vector_cache=vector_cache,
    )
    warm = [
        _query_once(
            db=db,
            embedder=embedder,
            lance_client=lance_client,
            rag=rag,
            case=case,
            top_k=top_k,
            vector_cache=vector_cache,
        )
        for _ in range(warm_runs)
    ]

    stage_names = tuple(cold["stages"])
    return {
        **asdict(case),
        "cold": cold,
        "warm": {
            "latency": summarize(sample["total_ms"] for sample in warm),
            "stages": {
                stage: summarize(sample["stages"][stage] for sample in warm)
                for stage in stage_names
            },
            "result_count": warm[-1]["result_count"],
        },
    }


def run_benchmark(
    *,
    data_dir: Path,
    top_k: int = 5,
    warm_runs: int = 3,
    cases: Iterable[BenchmarkCase] = DEFAULT_CASES,
) -> dict[str, Any]:
    """Run all benchmark cases and return a serializable report."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if warm_runs < 1:
        raise ValueError("warm_runs must be at least 1")
    return {
        "benchmark": "rag-lancedb",
        "backend": "lancedb",
        "data_dir": str(data_dir),
        "top_k": top_k,
        "warm_runs": warm_runs,
        "cases": [
            benchmark_case(
                case,
                data_dir=data_dir,
                top_k=top_k,
                warm_runs=warm_runs,
            )
            for case in cases
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="LanceDB directory (default: repository data/)",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Results to shape per query")
    parser.add_argument(
        "--warm-runs",
        type=int,
        default=3,
        help="Warm repetitions per query (default: 3)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="Write the full JSON report to PATH instead of only printing a summary",
    )
    return parser


def _print_summary(report: dict[str, Any]) -> None:
    print(
        f"RAG benchmark: {report['backend']} "
        f"(top_k={report['top_k']}, warm_runs={report['warm_runs']})"
    )
    for case in report["cases"]:
        cold = case["cold"]
        warm = case["warm"]
        stages = ", ".join(
            f"{name.removesuffix('_ms')}={value:.1f}ms"
            for name, value in cold["stages"].items()
        )
        print(
            f"{case['name']:<16} cold={cold['total_ms']:.1f}ms "
            f"warm_p50={warm['latency']['p50_ms']:.1f}ms "
            f"warm_p95={warm['latency']['p95_ms']:.1f}ms ({stages})"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_benchmark(
            data_dir=args.data_dir,
            top_k=args.top_k,
            warm_runs=args.warm_runs,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2

    _print_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
        print(f"JSON report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
