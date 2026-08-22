---
title: "Milvus Lite pilot"
nav_order: 2
parent: "Archive"
---

# Milvus Lite pilot

Milvus Lite is an **opt-in pilot** and is not wired into `rag.py`, the router,
or the normal LanceDB ingestion path. The repository therefore retains its
offline, no-service default behavior.

## Install and use

Milvus is an optional package extra:

```bash
uv sync --extra milvus-lite
```

The adapter is `hpe_networking_mcp.pipeline.clients.milvus_client`. It opens
only a local `.db` path, creating parent directories as needed:

```python
from hpe_networking_mcp.pipeline.clients.milvus_client import MilvusLiteStore

store = MilvusLiteStore("data/milvus-lite.db")
store.upsert([{
    "id": "chunk-1",
    "text": "WPA3 uses SAE",
    "source": "developer_docs",
    "vector": [0.1, 0.2],
}])
hits = store.search([0.1, 0.2], metadata_filter={"source": "developer_docs"})
```

`HPE_MCP_MILVUS_PATH` supplies the default path when the constructor argument
is omitted. IDs are stable across re-embedding: supplied IDs are preserved,
otherwise a SHA-256 ID is derived from content/provenance fields.

Dense retrieval and bounded scalar equality/`in` metadata filters are tested
without Milvus installed. Hybrid retrieval is capability-detected. Because
PyMilvus hybrid request/ranker constructors vary and Milvus Lite support can
differ by release, the pilot accepts installed-client-native request objects
and raises a clear `MilvusCapabilityError` when `hybrid_search` is unavailable
or incompatible. It never silently falls back to a remote service.

If the optional package is absent, construction raises
`MilvusDependencyError` with the install command; importing the module itself
continues to work.

## Corpus comparison

Build a local pilot from the existing LanceDB vectors and compare dense Milvus
retrieval on the active how-to evaluation questions:

```bash
uv sync --extra milvus-lite
uv run python scripts/benchmark_milvus.py \
  --milvus-path data/milvus-lite.db \
  --rebuild \
  --json milvus-benchmark.json
```

The report separates the one-time copy cost from warm search p50/p95 and
records source-hit, keyword, and MRR results. Compare it with
`scripts/benchmark_rag.py` and `tests/eval/run_eval.py`; do not promote Milvus
to the default unless it preserves the full hybrid quality gate, not merely
dense-search latency.

### Current local result

On the 262,104-row migrated corpus, the dense-only pilot copied the corpus in
about 94 seconds and measured **283.7 ms warm p50 / 331.6 ms p95** across the
16 active how-to questions, with **0.812 source-hit@5, 0.750 keyword-hit, and
0.740 MRR**. The LanceDB baseline remains **0.972 source-hit@5, 0.938
how-to-recall@5, and 0.972 MRR** on the full evaluation gate, with roughly
10 ms warm p50 on the representative local benchmark. Milvus Lite therefore
remains an opt-in experiment; its dense-only result is neither faster nor
accurate enough to replace LanceDB.
