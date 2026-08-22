"""Pin the parallel-embedding contract of ingest_tools.main_lancedb.

The strict-index CI job spends ~96% of its time embedding the tool catalog.
main_lancedb must use the data-parallel path (iter_embed_documents with a
non-None ``parallel`` kwarg) rather than the single-session embed_document
path; these tests make a silent revert fail loudly. They also pin the two
boundary contracts that make the switch safe: the HPE_MCP_INGEST_PARALLEL
override and output ordering (fastembed's ordered_map re-keys by input index,
so the zip in main_lancedb pairs each row with its own vector).
"""

from __future__ import annotations

import importlib
import os

from scripts import ingest_tools

_FAKE_PAIRS = [
    (
        "central-config",
        {"name": "alpha_tool", "description": "a", "schema": {}, "params": []},
    ),
    (
        "central-config",
        {"name": "beta_tool", "description": "b", "schema": {}, "params": []},
    ),
]


def _run_main_lancedb(monkeypatch, env_parallel: str | None = None) -> dict:
    """Run main_lancedb with the embed client and LanceDB store mocked out."""
    if env_parallel is None:
        monkeypatch.delenv("HPE_MCP_INGEST_PARALLEL", raising=False)
    else:
        monkeypatch.setenv("HPE_MCP_INGEST_PARALLEL", env_parallel)

    monkeypatch.setattr(ingest_tools, "_collect", lambda products=None: list(_FAKE_PAIRS))

    calls: dict = {}

    class FakeEmbedClient:
        def embed_document(self, texts, batch_size=64):
            calls["embed_document"] = True
            return [[1.0, 0.0] for _ in texts]

        def iter_embed_documents(self, texts, batch_size=32, parallel=None):
            calls["parallel"] = parallel
            texts = list(texts)
            calls["iter_texts"] = texts
            # Distinct vector per position so a re-ordered zip is detectable.
            return iter([[float(i), 1.0] for i in range(len(texts))])

    embed_module = importlib.import_module(
        "hpe_networking_mcp.pipeline.clients.embed_client"
    )
    monkeypatch.setattr(embed_module, "EmbedClient", FakeEmbedClient)

    lance_module = importlib.import_module(
        "hpe_networking_mcp.pipeline.clients.lance_client"
    )
    monkeypatch.setattr(lance_module, "connect", lambda: object())
    monkeypatch.setattr(
        lance_module,
        "create_tools_table",
        lambda db, rows: calls.setdefault("rows", rows),
    )

    assert ingest_tools.main_lancedb("all") == 0
    return calls


def test_main_lancedb_uses_parallel_embedding_path(monkeypatch):
    calls = _run_main_lancedb(monkeypatch)

    assert "embed_document" not in calls, "single-session path must not be used"
    assert calls["parallel"] is not None, "parallel kwarg must not revert to None"
    assert calls["parallel"] >= 1


def test_parallel_kwarg_defaults_to_capped_cpu_count(monkeypatch):
    calls = _run_main_lancedb(monkeypatch)

    # Uncapped cpu_count OOMs many-core hosts (one model copy per session);
    # the default must stay capped at 8.
    assert calls["parallel"] == min(os.cpu_count() or 1, 8)


def test_parallel_kwarg_honors_env_override(monkeypatch):
    calls = _run_main_lancedb(monkeypatch, env_parallel="3")

    assert calls["parallel"] == 3


def test_rows_preserve_input_order_through_the_zip(monkeypatch):
    calls = _run_main_lancedb(monkeypatch)

    rows = calls["rows"]
    assert [r["id"] for r in rows] == [
        ingest_tools._stable_id(server, t["name"]) for server, t in _FAKE_PAIRS
    ]
    # Each row carries the vector produced for its own position, not a
    # neighbour's — this is what ordered_map re-keying guarantees for real.
    assert [r["vector"] for r in rows] == [[0.0, 1.0], [1.0, 1.0]]
