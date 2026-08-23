#!/usr/bin/env python3
"""Build and compare a local Milvus Lite pilot against the LanceDB RAG path.

The comparison is intentionally dense-only for Milvus unless the installed
PyMilvus release exposes compatible native hybrid request constructors. The
default LanceDB path remains the quality baseline.

Usage:
    uv run python scripts/benchmark_milvus.py \
      --milvus-path /tmp/hpe-networking-milvus.db --json /tmp/milvus.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yaml  # noqa: E402

from hpe_networking_mcp.pipeline.clients import lance_client, milvus_client  # noqa: E402
from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient  # noqa: E402

DEFAULT_BATCH_SIZE = 1024
DEFAULT_TOP_K = 5
DEFAULT_WARM_RUNS = 5
OUTPUT_FIELDS = ("source", "file_path", "text")


def _percentile(values: Iterable[float], quantile: float) -> float:
    sample = sorted(float(value) for value in values)
    if not sample:
        raise ValueError("percentile requires at least one value")
    position = (len(sample) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sample) - 1)
    return sample[lower] + (sample[upper] - sample[lower]) * (position - lower)


def _summary(values: Iterable[float]) -> dict[str, float | int]:
    sample = [float(value) for value in values]
    return {
        "n": len(sample),
        "p50_ms": round(_percentile(sample, 0.50), 3),
        "p95_ms": round(_percentile(sample, 0.95), 3),
        "mean_ms": round(sum(sample) / len(sample), 3),
    }


def _rows_in_batches(table: Any, columns: list[str], batch_size: int):
    offset = 0
    while True:
        batch = table.search().select(columns).limit(batch_size).offset(offset).to_arrow()
        if batch.num_rows == 0:
            return
        yield batch.to_pylist()
        offset += batch.num_rows


def build_store(
    *,
    data_dir: Path,
    milvus_path: Path,
    batch_size: int,
) -> dict[str, int | float]:
    """Copy the existing LanceDB corpus into Milvus Lite without re-embedding."""
    db = lance_client.connect(data_dir)
    table = lance_client.docs_table(db)
    if table is None:
        raise FileNotFoundError(f"missing LanceDB docs table under {data_dir}")

    columns = list(table.schema.names)
    store = milvus_client.MilvusLiteStore(milvus_path)
    started = time.perf_counter()
    rows_seen = 0
    for rows in _rows_in_batches(table, columns, batch_size):
        records = []
        for row in rows:
            record = {
                key: value
                for key, value in row.items()
                if key != "_rowid" and value is not None
            }
            record["vector"] = list(record["vector"])
            records.append(record)
        store.upsert(records)
        rows_seen += len(records)
        if rows_seen % (batch_size * 10) == 0 or rows_seen == table.count_rows():
            print(f"  copied {rows_seen}/{table.count_rows()}", flush=True)
    return {
        "rows": rows_seen,
        "build_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _load_howto_questions() -> list[dict[str, Any]]:
    spec = yaml.safe_load((ROOT / "tests/eval/rag_eval.yaml").read_text())
    return [question for question in spec["questions"] if question["type"] == "howto"]


def _quality_row(
    hits: list[dict[str, Any]],
    question: dict[str, Any],
    top_k: int,
) -> dict[str, Any]:
    blob = " ".join(json.dumps(hit, sort_keys=True, default=str) for hit in hits[:top_k]).lower()
    expected_sources = [str(value).lower() for value in question.get("expect_sources", [])]
    source_rank = 0
    for index, hit in enumerate(hits[:top_k], start=1):
        hit_blob = json.dumps(hit, sort_keys=True, default=str).lower()
        if any(source in hit_blob for source in expected_sources):
            source_rank = index
            break
    keyword_hit = any(
        str(keyword).lower() in blob
        for keyword in question.get("expect_keywords", [])
    )
    return {
        "id": question["id"],
        "source_hit": source_rank > 0,
        "rank": source_rank,
        "keyword_hit": keyword_hit,
    }


def compare(
    *,
    data_dir: Path,
    milvus_path: Path,
    top_k: int,
    warm_runs: int,
    rebuild: bool,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if warm_runs < 1:
        raise ValueError("warm_runs must be at least 1")
    if not milvus_client.availability()["available"]:
        raise milvus_client.MilvusDependencyError(
            "Milvus Lite is unavailable; install it with `uv sync --extra milvus-lite`"
        )

    build = None
    if rebuild or not milvus_path.exists():
        build = build_store(
            data_dir=data_dir,
            milvus_path=milvus_path,
            batch_size=DEFAULT_BATCH_SIZE,
        )

    store = milvus_client.MilvusLiteStore(milvus_path)
    embedder = EmbedClient()
    questions = _load_howto_questions()
    vector_cache: dict[str, list[float]] = {}
    timings: list[float] = []
    quality: list[dict[str, Any]] = []
    for question in questions:
        query = question["query"]
        key = query.strip().casefold()
        if key not in vector_cache:
            vector_cache[key] = list(embedder.embed_query(query))
        vector = vector_cache[key]
        started = time.perf_counter()
        store.search(vector, top_k=top_k, output_fields=OUTPUT_FIELDS)
        cold_ms = (time.perf_counter() - started) * 1000
        warm_samples = []
        for _ in range(warm_runs):
            started = time.perf_counter()
            warm_hits = store.search(vector, top_k=top_k, output_fields=OUTPUT_FIELDS)
            warm_samples.append((time.perf_counter() - started) * 1000)
        timings.extend(warm_samples)
        quality.append(_quality_row(warm_hits, question, top_k))
        print(
            f"  {question['id']}: cold={cold_ms:.1f}ms "
            f"warm_p50={_percentile(warm_samples, 0.5):.1f}ms",
            flush=True,
        )

    source_hit = sum(row["source_hit"] for row in quality) / len(quality)
    keyword_hit = sum(row["keyword_hit"] for row in quality) / len(quality)
    mrr = sum((1 / row["rank"]) if row["rank"] else 0 for row in quality) / len(quality)
    return {
        "backend": "milvus-lite-dense",
        "milvus_path": str(milvus_path),
        "top_k": top_k,
        "warm_runs": warm_runs,
        "build": build,
        "timing": _summary(timings),
        "quality_howto": {
            "n": len(quality),
            "source_hit@k": round(source_hit, 3),
            "keyword_hit": round(keyword_hit, 3),
            "mrr": round(mrr, 3),
        },
        "rows": quality,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--milvus-path", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--warm-runs", type=int, default=DEFAULT_WARM_RUNS)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--json", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = compare(
            data_dir=args.data_dir,
            milvus_path=args.milvus_path,
            top_k=args.top_k,
            warm_runs=args.warm_runs,
            rebuild=args.rebuild,
        )
    except (FileNotFoundError, ValueError, milvus_client.MilvusPilotError) as exc:
        print(f"Milvus benchmark failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
        print(f"JSON report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
