# Retrieval Accuracy Plan — Cross-Encoder Reranking

Status: **proposed / not implemented**. Planning document for a future retrieval
accuracy pass. Nothing in this doc is wired into the runtime today.

Companion to [RAG-ARCHITECTURE.md](RAG-ARCHITECTURE.md), which describes the
retrieval stack as it actually ships.

---

## 1. Current state (as built)

| Layer | Implementation | Location |
|---|---|---|
| Embeddings | `nomic-embed-text-v1.5` in-process via fastembed (ONNX), 768 dims | `pipeline/clients/embed_client.py` |
| Document retrieval | LanceDB hybrid — vector + native BM25 FTS, fused with Reciprocal Rank Fusion | `pipeline/clients/lance_client.py::hybrid_search` |
| Post-retrieval ranking | Static source/vendor boost heuristic (`_boost_sources`) | `mcp_servers/rag.py` |
| Exact API lookup | SQLite FTS5 over OpenAPI specs, re-ranked by distinct-term coverage | `pipeline/clients/specs_index.py` |
| Structured lookup | Advisory / lifecycle SQLite indexes, exact-match only | `pipeline/clients/advisory_index.py` |
| Tool discovery | Hybrid search over the tool catalog behind a 3-tool router | `lance_client.py::search_tools`, `mcp_servers/tool_router.py` |
| Evaluation | YAML question set + threshold-gated runner | `tests/eval/rag_eval.yaml`, `tests/eval/run_eval.py` |

**The gap:** ranking today is RRF fusion plus a hand-written source boost. There
is no cross-encoder stage. RRF fuses two *independent* rankings by position — it
never actually reads the query against the candidate text jointly. A reranker
does, which is why it typically recovers the "right chunk was at rank 8" cases
that dominate residual RAG errors.

### Why not fine-tune a generative LLM instead

Recorded here so the question does not get re-litigated:

- This repository contains **no generative LLM**. It is a retrieval + dispatch
  layer; the answering model is whatever MCP client connects to it. Accuracy is
  therefore a *retrieval* property, not a weights property.
- Correctness on API questions comes from exact FTS5 lookups against ingested
  specs. Baking those facts into weights makes them stale by construction —
  `data/refresh_log.jsonl` and the source-drift gates exist precisely because
  upstream specs move.
- Fine-tuning is the right answer for **air-gapped / self-hosted deployment,
  per-token cost, or latency** — compliance and economics drivers. It is not an
  accuracy lever for this architecture.
- If domain adaptation is wanted later, fine-tune the **embedder**, not the LLM.
  See §7.

---

## 2. Objective

Insert an optional cross-encoder reranking stage between hybrid retrieval and
result shaping, default **off**, and prove its value with the existing eval
harness before considering it for default-on.

Success = measurable gain in `min_mrr` and `min_howto_recall` with no regression
in `min_api_exact` or the structured-exact metrics, at acceptable added latency.

---

## 3. Where it plugs in

Single integration point. In `mcp_servers/rag.py::_search_lancedb`, the current
flow is:

```
hybrid_search(top_k = max(top_k * 6, 30))   # deep candidate fetch
  -> _boost_sources(hits, query)            # static vendor/source heuristic
  -> _shape(hits, top_k)                    # truncate + normalize row shape
```

Proposed flow:

```
hybrid_search(top_k = max(top_k * 6, 30))   # unchanged deep fetch
  -> rerank(query, hits)                    # NEW, optional, feature-flagged
  -> _boost_sources(hits, query)            # unchanged
  -> _shape(hits, top_k)                    # unchanged
```

Notes:

- The deep fetch already exists (`top_k * 6`, floor 30) and was written so the
  boost stage has candidates to promote. A reranker consumes the same candidate
  pool — no retrieval changes required.
- Reranking runs **before** `_boost_sources` so the existing authoritative-source
  policy still has the final word. If evals later show the boost is redundant
  once a reranker is present, retiring it becomes a separate, measured decision.
- `_search_redis` is the legacy backend and stays untouched.
- Consider the same treatment for `search_tools` / `find_tool` as a **phase 2**,
  only after the docs path is proven. Tool selection has its own eval section
  (`_run_tool_selection_eval`) and its own latency budget on every router call.

## 4. Model options

All run locally via ONNX; no new service, consistent with the
"clone -> uv sync -> run" default path.

| Model | Size | Notes |
|---|---|---|
| `Xenova/ms-marco-MiniLM-L-6-v2` | ~23M | Fastest; the standard baseline reranker |
| `jinaai/jina-reranker-v1-turbo-en` | ~38M | Better quality, still CPU-viable |
| `BAAI/bge-reranker-base` | ~278M | Strongest, meaningfully slower on CPU |

Start with MiniLM-L-6. It is the cheapest way to find out whether reranking
moves the metrics at all; upgrading the checkpoint afterwards is a one-line
change.

fastembed exposes rerankers through its text cross-encoder API, so this reuses
the ONNX runtime already vendored for embeddings — no new heavyweight
dependency, and the model cache/offline story matches `embed_client.py`.

## 5. Configuration

Follow the established env-var pattern (`resolve_rag_backend` in
`mcp_servers/shared.py`, validated through `reject_unknown_env_choices`):

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `HPE_MCP_RAG_RERANK` | `off` \| `on` | `off` | Enable the cross-encoder stage |
| `HPE_MCP_RAG_RERANK_MODEL` | model id | MiniLM-L-6 | Checkpoint override |
| `HPE_MCP_RAG_RERANK_CANDIDATES` | int | `30` | Candidates scored per query |

Requirements:

- Default `off` means zero behavior change and zero added latency until
  explicitly opted in.
- Lazy-load the model on first rerank call, mirroring `embed_client.py` — an
  unused reranker must not cost import time or memory.
- **Fail open.** If the model is missing or scoring raises, log a warning once
  and return the unreranked candidates. Precedent already exists in
  `hybrid_search`, which degrades to vector-only when the FTS index is absent.
  Retrieval must never hard-fail because an optional ranker is unavailable.

## 6. Validation plan

The harness needed for this already exists.

1. **Baseline.** `run_eval.py --json baseline.json` with rerank off.
2. **Candidate.** Same command, rerank on, `--json rerank.json`.
3. **Compare** per-type: `api-lookup`, `howto`, `advisory`, `lifecycle`,
   `list-*`, `correlate`, `diagnostics`. `howto` is where gains should show;
   `api-lookup` and the structured types route through exact SQLite paths and
   should be **flat** — movement there means something is wrong.
4. **Latency.** Record added ms/query at 30 candidates. Budget: the docs path is
   interactive, so keep p95 overhead in the tens of ms, not hundreds.
5. **Expand the question set first.** The current `rag_eval.yaml` is sized for
   regression detection, not for resolving small ranking deltas. Add `howto`
   cases — especially near-miss queries where the right chunk currently lands
   just outside `top_k` — before trusting any measured improvement.
6. **Gate.** Only once a gain is reproducible, consider raising `--min-mrr` /
   `--min-howto-recall` in the CI thresholds.

Decision rule: if MRR gain is under ~3 points on an expanded set, do not ship it
on by default. Leave the flag for self-hosted users who want maximum accuracy
and accept the latency.

## 7. Follow-on: embedder fine-tuning

Only worth doing after reranking is settled, and only if evals show *retrieval*
misses (right answer absent from the candidate pool) rather than *ranking*
misses (right answer present but ranked low). A reranker fixes ranking misses; a
better embedder fixes retrieval misses. Diagnose which failure mode dominates
before spending effort.

Rationale: `nomic-embed-text-v1.5` is a general-purpose model. Networking
identifiers and jargon — VSX, MPSK, CDA, VRRP, WPA3_SAE, UXI — are exactly where
general embeddings under-separate. Fine-tuning the embedder on
(query, correct-chunk) pairs is orders of magnitude cheaper than LLM fine-tuning
and improves every consumer of the index at once.

Training pairs can be mined from the corpus already ingested plus the eval set;
Unsloth is unnecessary here — sentence-transformers handles embedding fine-tunes
directly. Note the hard constraint: re-embedding requires a **full re-ingest**,
and `EMBEDDING_DIMS = 768` is currently assumed in `lance_client.py`.

## 8. Rollout

| Phase | Work | Exit criteria |
|---|---|---|
| 0 | Expand `rag_eval.yaml` `howto` coverage; capture baseline JSON | Baseline reproducible across runs |
| 1 | `rerank()` helper + env flag, default off, fail-open | Off = byte-identical results to baseline |
| 2 | A/B on docs path; record latency | Documented gain or documented no-gain |
| 3 | Optional: extend to `search_tools` | Tool-selection eval improves, router latency acceptable |
| 4 | Optional: default-on + raised CI thresholds | Sustained gain, no regressions |

Phases 3 and 4 are explicitly optional and each gated on the prior phase's
measurements.

## 9. Risks

- **Latency on the router path.** Reranking every `find_tool` call taxes all
  6,722-tool discovery. Kept out of scope until phase 3, measured separately.
- **Redundancy with `_boost_sources`.** Two ranking policies can fight. Ordering
  in §3 keeps the deterministic policy authoritative; revisit only with data.
- **Model download on first use.** Breaks strict air-gapped installs unless the
  checkpoint is pre-cached. Same constraint as the embedding model — document it
  alongside the existing offline setup guidance.
- **Metric overfitting.** A small eval set plus a tunable ranker invites tuning
  to the test. Expanding the set in phase 0 is a prerequisite, not a nicety.
