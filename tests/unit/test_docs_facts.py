"""Rendered-doc RAG/index count claims stay derived from canonical facts.

``docs/project-facts.json`` is the single source for every published tool
count (covered by ``tests/unit/test_project_facts.py`` and
``tests/unit/test_tool_docstring_counts.py``), but the LanceDB prose-chunk
count and the ``data/specs.sqlite`` structured counts (endpoints, schemas,
fields, advisories, lifecycle records) are hand-copied into several
"current state" pages as prose, not just a table cell. Those copies drifted
once already: several pages still cited a pre-refresh 51,737-chunk /
3,796-endpoint snapshot long after the local index was rebuilt to 392,471
chunks / 4,106 endpoints.

This module is the render-level counterpart to
``test_tool_docstring_counts.py``'s ``PUBLIC_TOOL_COUNT_RE`` check: for each
page that describes the *current* index (not a versioned release note,
which legitimately freezes the count an artifact shipped with, or a
narrative paragraph describing a historical intermediate ingestion step),
it asserts the exact formatted count strings derived from
``docs/project-facts.json["indexes"]`` are present. If the index is
rebuilt and the fact file regenerated, this test fails loudly on every page
that still carries the old numbers instead of silently going stale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline import project_facts

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED = project_facts.load()


def _canonical_counts() -> dict[str, int] | None:
    indexes = TRACKED.get("indexes")
    if not indexes:
        return None
    # Endpoints/schemas/fields are the offline-derivable counts any clone
    # reproduces from vendor/openapi; the prose-chunk, advisory and lifecycle
    # counts come from the scraped corpus. The docs cite both, so read both.
    offline = indexes.get(project_facts.OFFLINE_DERIVABLE) or {}
    local = indexes.get(project_facts.LOCALLY_BUILT) or {}
    specs = offline.get("specs_sqlite") or {}
    scraped_specs = local.get("specs_sqlite") or {}
    docs_lance = local.get("docs_lance") or {}
    counts = {
        "prose_chunks": docs_lance.get("rows"),
        "endpoints": specs.get("endpoints"),
        "schemas": specs.get("schemas"),
        "fields": specs.get("fields"),
        "advisories": scraped_specs.get("advisories"),
        "lifecycle_records": scraped_specs.get("lifecycle_events"),
    }
    if any(value is None for value in counts.values()):
        return None
    return counts


CANONICAL = _canonical_counts()


def _fmt(key: str) -> str:
    return f"{CANONICAL[key]:,}"


# Every entry is a (file, required-substring) pair. Substrings are the exact
# phrasing committed in each "current state" page -- deliberately narrow so a
# historical narrative paragraph elsewhere in the same file (e.g. describing
# an earlier ingestion milestone in docs/architecture/RAG-ARCHITECTURE.md)
# cannot accidentally satisfy the check with a stale number.
def _expected_snippets() -> list[tuple[Path, str]]:
    chunks, endpoints, schemas, fields, advisories, lifecycle = (
        _fmt("prose_chunks"),
        _fmt("endpoints"),
        _fmt("schemas"),
        _fmt("fields"),
        _fmt("advisories"),
        _fmt("lifecycle_records"),
    )
    return [
        (
            REPO_ROOT / "README.md",
            f"{chunks} prose chunks; {endpoints} endpoints, {schemas} schemas, "
            f"{fields} fields, {advisories} advisories, {lifecycle} lifecycle records",
        ),
        (REPO_ROOT / "docs" / "index.md", f"{chunks} prose chunks in LanceDB"),
        (
            REPO_ROOT / "docs" / "index.md",
            f"{endpoints} endpoints, {schemas} schemas, {fields} fields, "
            f"{advisories} advisories, {lifecycle} lifecycle records",
        ),
        (
            REPO_ROOT / "docs" / "getting-started.md",
            f"The current rebuilt snapshot contains {chunks} prose chunks",
        ),
        (
            REPO_ROOT / "docs" / "getting-started.md",
            f"index with {endpoints} endpoints, {schemas} schemas, {fields} fields,",
        ),
        (
            REPO_ROOT / "docs" / "troubleshooting.md",
            f"the prose corpus is {chunks} chunks",
        ),
        (
            REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md",
            f"contains {endpoints} endpoints, {schemas} schemas, and {fields} fields,",
        ),
        (
            REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md",
            f"plus {advisories} advisories and {lifecycle} lifecycle records.",
        ),
        (
            REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md",
            f"**Current indexed corpus:** {chunks} prose chunks",
        ),
        (
            REPO_ROOT / "docs" / "architecture" / "how-it-works.md",
            f"The current corpus is **{chunks}** prose chunks, **{endpoints}** endpoints,",
        ),
        (
            REPO_ROOT / "docs" / "architecture" / "system-overview.md",
            f"the {chunks}-row LanceDB table remains a prose retrieval corpus",
        ),
        (
            REPO_ROOT / "docs" / "release-indexes.md",
            f"| LanceDB prose chunks | {chunks} |",
        ),
        # Labelled "(offline)" in the table: these three are the counts any
        # clone reproduces from the committed corpus, so the label is part of
        # the claim and must not drift away from the number.
        (
            REPO_ROOT / "docs" / "release-indexes.md",
            f"| Exact endpoints (offline) | {endpoints} |",
        ),
        (REPO_ROOT / "docs" / "release-indexes.md", f"| Schemas (offline) | {schemas} |"),
        (REPO_ROOT / "docs" / "release-indexes.md", f"| Fields (offline) | {fields} |"),
        (
            REPO_ROOT / "docs" / "assets" / "platform-coverage.svg",
            f"{chunks} prose chunks",
        ),
        (
            REPO_ROOT / "docs" / "assets" / "platform-coverage.svg",
            f"{endpoints} exact API endpoints",
        ),
    ]


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.skipif(
    CANONICAL is None,
    reason="docs/project-facts.json has no tracked index facts to compare against",
)
def test_current_state_docs_carry_canonical_index_counts():
    # Markdown prose wraps across lines, so compare with whitespace
    # (including newlines) collapsed rather than requiring the snippet to
    # appear on a single physical line.
    missing = [
        f"{path.relative_to(REPO_ROOT)}: missing {snippet!r}"
        for path, snippet in _expected_snippets()
        if _normalize_whitespace(snippet)
        not in _normalize_whitespace(path.read_text(encoding="utf-8", errors="replace"))
    ]

    assert missing == []


def test_release_notes_and_live_eval_pages_are_excluded_from_the_current_state_check():
    """Guard the exclusion list itself: it must name real, still-tracked files."""
    checked = {path.relative_to(REPO_ROOT) for path, _ in _expected_snippets()} or {
        Path("README.md")
    }
    for name in checked:
        assert not name.name.startswith("release-notes-"), name
        assert "aos8-live-" not in name.name, name


# ---------------------------------------------------------------------------
# Router-mode tool-count regressions
#
# A prior drift shipped "6,703 complete backend catalog / 6,710 direct-all"
# across README and most of docs/ -- it conflated the platform-API-only
# subtotal (design-core/interop-core excluded, matching
# docs/capability-gap-matrix.md) with the complete registered backend total
# (design-core + interop-core included). Those historical corrections are
# intentionally preserved here as regression context; the current counts are
# derived from docs/project-facts.json and checked by the live documentation
# assertions below. These checks catch a regression to the old, wrong numbers
# anywhere in the active documentation surface, not just the specific pages
# fixed once.
# ---------------------------------------------------------------------------

#: Pages that intentionally freeze a point-in-time snapshot and must not be
#: scanned for "current" router-mode/catalog claims. CHANGELOG.md is
#: included because it explicitly quotes the historical wrong claim ("6,703
#: complete / 6,710 direct-all") as provenance for what was fixed --
#: excluding it here is the CHANGELOG-equivalent of excluding release notes.
_HISTORICAL_DOC_NAME_MARKERS = ("release-notes-", "aos8-live-", "CHANGELOG.md")

STALE_DIRECT_ALL_RE = re.compile(r"6,?710\b")
#: A real, copy/pasteable env assignment -- not a backtick-quoted mention of
#: the (invalid) value inside explanatory prose warning readers away from it.
INVALID_PRODUCTS_ALL_ENV_RE = re.compile(r"(?<!`)HPE_MCP_PRODUCTS\s*=\s*all\b")
INVALID_PRODUCTS_ALL_JSON_RE = re.compile(r'"HPE_MCP_PRODUCTS"\s*:\s*"all"')
CODE_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def _active_doc_and_config_files() -> list[Path]:
    candidates = [
        *(REPO_ROOT / "docs").rglob("*.md"),
        *(REPO_ROOT / "docs").rglob("*.svg"),
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
        REPO_ROOT / "MIGRATION.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "CLAUDE.md",
        *(REPO_ROOT / "examples").rglob("*"),
        REPO_ROOT / ".mcp.json.example",
        REPO_ROOT / ".mcp.http.json.example",
        REPO_ROOT / ".cursor" / "mcp.json",
        REPO_ROOT / ".cursor" / "mcp.dev.json",
        REPO_ROOT / ".vscode" / "mcp.json.example",
        REPO_ROOT / ".claude" / "launch.json",
    ]
    return sorted(
        {
            path
            for path in candidates
            if path.is_file() and not any(m in path.name for m in _HISTORICAL_DOC_NAME_MARKERS)
        }
    )


def test_no_active_doc_or_config_claims_the_stale_direct_all_count():
    problems = [
        str(path.relative_to(REPO_ROOT))
        for path in _active_doc_and_config_files()
        if STALE_DIRECT_ALL_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    ]

    assert problems == []


def test_no_active_doc_or_config_sets_invalid_products_equals_all():
    """``HPE_MCP_PRODUCTS=all``/``"HPE_MCP_PRODUCTS": "all"`` must never
    appear as a runnable example -- only ``HPE_MCP_TOOLSETS=all`` accepts
    ``all``; ``HPE_MCP_PRODUCTS`` only accepts specific product names and
    raises ``InvalidRuntimeConfigError`` on ``all``. JSON config files are
    checked in full (any occurrence is a runnable example by construction);
    Markdown/SVG files are checked only inside fenced code blocks, so prose
    explicitly warning readers away from the invalid value (in backtick
    inline code) does not trip this check.
    """
    problems = []
    for path in _active_doc_and_config_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            if INVALID_PRODUCTS_ALL_JSON_RE.search(text) or INVALID_PRODUCTS_ALL_ENV_RE.search(
                text
            ):
                problems.append(str(path.relative_to(REPO_ROOT)))
            continue
        for block in CODE_FENCE_RE.findall(text):
            if INVALID_PRODUCTS_ALL_ENV_RE.search(block) or INVALID_PRODUCTS_ALL_JSON_RE.search(
                block
            ):
                problems.append(str(path.relative_to(REPO_ROOT)))
                break

    assert problems == []


@pytest.mark.skipif(
    TRACKED.get("router_modes") is None,
    reason="docs/project-facts.json has no tracked router_modes facts to compare against",
)
def test_current_state_docs_carry_canonical_router_mode_counts():
    router_tools = TRACKED["router_modes"]["tools"]
    registered_total = TRACKED["tools"]["registered_total"]
    minimal, default, direct_all, default_recommended = (
        router_tools["minimal"],
        router_tools["default"],
        router_tools["direct_all"],
        router_tools["default_recommended_profile"],
    )
    platform_backend_total = TRACKED["tools"]["platform_backend_total"]

    snippets = [
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"hpe-networking-mcp registers **{registered_total:,} backend tools**",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"for **{direct_all:,} client-visible tools total**",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"| `direct` + `HPE_MCP_TOOLSETS=all` | {direct_all:,} |",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"| `default` | {default:,} |",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"The `default` count ({default:,}) is measured identically",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"| **Platform API backend total** | **3,159** | **{platform_backend_total:,}** |",
        ),
        (
            REPO_ROOT / "docs" / "tool-catalog.md",
            f"| **Complete backend total** | **3,175** | **{registered_total:,}** |",
        ),
        (
            REPO_ROOT / "docs" / "tool-router.md",
            f"| Default router | {default:,} client-visible tools[^compliance-tool] |",
        ),
        (
            REPO_ROOT / "docs" / "tool-router.md",
            "raising the default-mode count to "
            f"{default:,}.",
        ),
        (
            REPO_ROOT / "docs" / "tool-router.md",
            f"| Complete backend index (platform APIs + Central Streaming + "
            f"`site-health` + local GLP preflight + `design-core` + "
            f"`interop-core`) | {registered_total:,} tools |",
        ),
        (
            REPO_ROOT / "docs" / "tool-router.md",
            f"| Direct-all router | {direct_all:,} client-visible tools |",
        ),
        (
            REPO_ROOT / "docs" / "assets" / "platform-coverage.svg",
            f"6,144 generated | {registered_total:,} backend | {direct_all:,} direct-all",
        ),
    ]
    assert minimal == 3
    # The documented recommended client profile (HPE_MCP_TOOLSETS=central,glp,rag)
    # must expose the same default-mode count as the "every toolset" scenario
    # documented in the tables above -- a prior drift claimed 16 here.
    assert default_recommended == default

    missing = [
        f"{path.relative_to(REPO_ROOT)}: missing {snippet!r}"
        for path, snippet in snippets
        if _normalize_whitespace(snippet)
        not in _normalize_whitespace(path.read_text(encoding="utf-8", errors="replace"))
    ]

    assert missing == []


RAG_ARCHITECTURE = REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md"


def _eval_question_count() -> int:
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((REPO_ROOT / "tests" / "eval" / "rag_eval.yaml").read_text("utf-8"))
    return len(spec["questions"])


def test_rag_architecture_quotes_the_real_eval_set_and_source_counts():
    """The RAG page states the eval-set size and the tracked rebuild-source
    count as prose. Both are derivable (``tests/eval/rag_eval.yaml`` and
    ``docs/project-facts.json``), so pin them here: the page previously
    claimed a 31-question set and 13 rebuild sources long after the set grew
    to 33 questions and the manifest to 16 sources.
    """
    import json

    text = _normalize_whitespace(RAG_ARCHITECTURE.read_text(encoding="utf-8"))
    questions = _eval_question_count()
    manifest = json.loads((REPO_ROOT / "ingestion" / "source_manifest.json").read_text("utf-8"))
    declared_sources = TRACKED["rag_sources"]["count"]

    assert len(manifest) == declared_sources
    missing = [
        snippet
        for snippet in (
            f"`tests/eval/rag_eval.yaml` — {questions} questions",
            f"embedded LanceDB hybrid ({questions} questions)",
            f"The current {questions}-question eval",
            f"current manifest covers {declared_sources} rebuild sources",
        )
        if _normalize_whitespace(snippet) not in text
    ]

    assert missing == []


def test_rag_architecture_scores_are_at_or_above_the_enforced_eval_gate():
    """Published "current" scores must never fall below the thresholds
    ``tests/eval/run_eval.py --ci`` actually enforces, so lowering the gate
    (or copying an optimistic score into the page) fails here.
    """
    import importlib.util

    path = REPO_ROOT / "tests" / "eval" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval_for_docs", path)
    assert spec and spec.loader
    run_eval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_eval)

    text = RAG_ARCHITECTURE.read_text(encoding="utf-8")
    published = {
        "source_hit@k": 1.0,
        "mrr": 1.0,
        "howto_recall@k": 1.0,
        "api_exact": 1.0,
        "structured_exact": 1.0,
        "structured_list_exact": 1.0,
    }

    for metric, score in published.items():
        assert score >= run_eval._DEFAULT_THRESHOLDS[metric], metric
    assert "| `source_hit@k` (overall) | 0.50 | 0.80 | **1.00** |" in text
    assert "| `mrr` | 0.339 | 0.679 | **1.00** |" in text
