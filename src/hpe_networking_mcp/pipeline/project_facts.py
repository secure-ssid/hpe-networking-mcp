"""Derived, machine-readable canonical facts about this project.

Every published number in this repository -- package version, MCP server
IDs, per-backend tool counts, generated-operation counts, the exact
structured API SQLite counts, and the RAG artifact/source counts -- has
historically been re-typed by hand into README.md, docs/tool-catalog.md,
docs/capability-gap-matrix.md, docs/release-indexes.md, and release notes.
Hand-entered copies drift: the local index manifests claimed 9 RAG sources
while the tracked manifest declared 16, and the tool index still carried
pre-rename ``aruba-*`` server IDs long after the backends were renamed to
``central-*``/``glp-core``.

This module is the single derivation point instead. Nothing here is
hand-entered: counts come from importing the backend registries, reading the
committed generated manifests, parsing ``pyproject.toml``, and querying the
local indexes. :func:`collect` returns that snapshot,
``docs/project-facts.json`` is its tracked serialization, and
:func:`compare` diffs the two so ``scripts/project_facts.py --check`` (and
``scripts/validate_release.py``'s strict mode) fail on drift instead of
letting a stale number ship.

Index-derived facts are optional by design: a fresh checkout with no
``data/`` directory can still validate every code-derived fact, while strict
release validation requires the index sections too.

Never fetches anything: all inputs are local files and local imports.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from hpe_networking_mcp._paths import repo_root

REPO_ROOT = repo_root()
FACTS_PATH = REPO_ROOT / "docs" / "project-facts.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MANIFEST_DIR = (
    REPO_ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "openapi_gen" / "manifests"
)
SOURCE_MANIFEST_PATH = REPO_ROOT / "ingestion" / "source_manifest.json"
DATA_DIR = REPO_ROOT / "data"
SPECS_DB_PATH = DATA_DIR / "specs.sqlite"

#: Bumped when the fact document's shape changes, so a stale tracked copy is
#: rejected loudly rather than compared field-by-field against a new schema.
SCHEMA_VERSION = 4

#: The section holding facts any clone reproduces exactly from committed
#: inputs. They are compared whenever the artifact exists, and a mismatch is
#: a failure: nothing about them depends on one machine's scrape.
OFFLINE_DERIVABLE = "offline_derivable"

#: The section holding facts that need an artifact this repository does not
#: distribute -- the scraped prose corpus, and the LanceDB tables built from
#: it. They are compared only when that artifact is present locally, because
#: "not built here" is not evidence that anything changed.
LOCALLY_BUILT = "locally_built"

#: Structured-index tables ``scripts/build_spec_index.py`` writes from the
#: committed ``vendor/openapi`` corpus. Any clone reproduces these exactly.
OFFLINE_DERIVABLE_SPECS_TABLES = ("endpoints", "schemas", "fields")

#: Structured-index tables that come from ``ingestion/ingest_docs.py`` -- a
#: crawl of vendor documentation this project does not redistribute.
LOCALLY_BUILT_SPECS_TABLES = ("advisories", "lifecycle_events")

#: Tables whose exact row counts are part of the strict release contract.
SPECS_TABLES = OFFLINE_DERIVABLE_SPECS_TABLES + LOCALLY_BUILT_SPECS_TABLES

#: Which side every non-``specs_sqlite`` index family falls on. A family in
#: neither set is compared unconditionally, so a fact added without choosing
#: a side fails the gate rather than quietly landing on the lenient one.
LOCALLY_BUILT_INDEX_FAMILIES = ("docs_lance", "tools_lance")

#: Backends that ship no vendor API surface (local translation/diagram
#: helpers). They are registered tools but are excluded from the published
#: platform API catalog.
CREDENTIAL_FREE_LOCAL_SERVERS = ("design-core", "interop-core")

#: Protocol-only vendor surfaces are callable backends but are not represented
#: by the REST/OpenAPI operation manifests used for platform coverage totals.
PROTOCOL_ONLY_SERVERS = ("central-streaming",)

#: Cross-platform aggregators are real registered tools, but they compose other
#: backends rather than exposing a vendor API of their own, so they are not part
#: of the per-platform API catalog or its capability benchmark.
NON_PLATFORM_AGGREGATOR_SERVERS = ("site-health",)

#: Curated diagnostics that intentionally inspect local configuration/cache
#: state and make no vendor API call, even though they live beside vendor
#: workflows in a platform backend.
NON_API_LOCAL_TOOLS = {
    "glp-core": frozenset({"glp_preflight"}),
    # ``corpus_provenance`` reads ``vendor/openapi/MANIFEST.json`` and the
    # local index artifacts to report what backed an answer. It reaches no
    # vendor API, so counting it in the published platform-API benchmark
    # (``platform_backend_total``, the 6,711 in docs/capability-gap-matrix.md)
    # would inflate that number with a tool that describes the catalog rather
    # than extending it.
    "rag-core": frozenset({"corpus_provenance"}),
}

FULL_WRITE_GATE_ENV = {
    "HPE_MCP_READONLY": "0",
    "HPE_MCP_CENTRAL_WRITES": "1",
    "HPE_MCP_GLP_V2BETA1_WRITES": "1",
    "HPE_MCP_AOS8_WRITES": "1",
    "HPE_MCP_EDGECONNECT_WRITES": "1",
    "HPE_MCP_APSTRA_WRITES": "1",
    "HPE_MCP_MIST_WRITES": "1",
    "HPE_MCP_CLEARPASS_WRITES": "1",
    "HPE_MCP_UXI_WRITES": "1",
    "HPE_MCP_AXIS_WRITES": "1",
}

GENERATED_TOOL_PLATFORMS = (
    "CENTRAL",
    "GLP",
    "AOS8",
    "EDGECONNECT",
    "APSTRA",
    "MIST",
    "CLEARPASS",
    "UXI",
)
GENERATED_TOOL_ENV = {
    f"HPE_MCP_{platform}_GENERATED_TOOLS": "1"
    for platform in GENERATED_TOOL_PLATFORMS
}

#: Environment the tool catalog must be collected under so the count is
#: reproducible: every guarded write and generated operation registered.
CATALOG_ENV = {
    "HPE_MCP_ACCESS_PROFILE": "full-read-write",
    "HPE_MCP_PRODUCT_ACCESS": "read-write",
    **FULL_WRITE_GATE_ENV,
    **GENERATED_TOOL_ENV,
}

#: Environment router-mode tool counts are measured under: every toolset
#: (``all`` -- NOT ``HPE_MCP_PRODUCTS=all``, which is invalid; ``all`` is a
#: toolset-only value, see ``_VALID_PRODUCTS`` in tool_router.py), every
#: guarded write/generated surface open. This is the same "complete write
#: catalog" scenario CATALOG_ENV pins for ``tool_facts()``, plus the
#: aligned legacy write gates. Pinning those gates prevents an exported
#: per-platform override or repository ``.env`` from contradicting the
#: aggregate profile, keeping ``direct_all - registered_total`` at the fixed
#: "+7 router-native tools" identity documented throughout the tool catalog.
ROUTER_MODE_ENV = {
    **CATALOG_ENV,
    "HPE_MCP_TOOLSETS": "all",
}

#: The documented, recommended default client profile (``.mcp.json.example``,
#: README quickstart, ``.cursor/mcp.json``, etc.): only the always-on core
#: backends (``central``, ``glp``, ``rag``, plus the credential-free
#: ``interop-core`` backend that every profile loads regardless of
#: ``HPE_MCP_TOOLSETS``). Measured separately from :data:`ROUTER_MODE_ENV`'s
#: "every toolset/product" scenario because ``default`` mode's convenience
#: wrapper count depends only on whether ``central-monitoring``/``rag-core``
#: are loaded -- true under both scenarios today -- but a future wrapper
#: gated on some other backend could make them diverge, and the actually
#: documented client profile is this one, not ``HPE_MCP_TOOLSETS=all``.
RECOMMENDED_PROFILE_ENV = {
    "HPE_MCP_ACCESS_PROFILE": "custom",
    "HPE_MCP_TOOLSETS": "central,glp,rag",
}


class ProjectFactsError(RuntimeError):
    """Raised when facts cannot be derived from the local checkout."""


# ---------------------------------------------------------------------------
# Package / server identity
# ---------------------------------------------------------------------------


def package_facts() -> dict[str, Any]:
    """Return package name, version, and console-script entry points."""
    name = version = ""
    scripts: dict[str, str] = {}
    section = ""
    for raw in PYPROJECT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        if section == "[project]" and line.startswith("name") and not name:
            name = line.split("=", 1)[1].strip().strip('"')
        elif section == "[project]" and line.startswith("version") and not version:
            version = line.split("=", 1)[1].strip().strip('"')
        elif section == "[project.scripts]" and "=" in line:
            key, value = line.split("=", 1)
            scripts[key.strip().strip('"')] = value.strip().strip('"')
    if not name or not version:
        raise ProjectFactsError(f"could not parse name/version from {PYPROJECT_PATH}")
    return {"name": name, "version": version, "console_scripts": dict(sorted(scripts.items()))}


def _router_module() -> ModuleType:
    from hpe_networking_mcp.mcp_servers import tool_router

    return tool_router


def server_facts() -> dict[str, Any]:
    """Return the router's MCP server ID and every backend server ID.

    Derived from ``tool_router``'s backend maps rather than a second list, so
    a backend added to the router is automatically part of the contract.
    """
    router = _router_module()
    optional = {product: server for product, (server, _) in router._OPTIONAL_BACKENDS.items()}
    return {
        "router_server_id": router.mcp.name,
        "core_backends": dict(sorted(router._BACKENDS_BASE.items())),
        "generated_backends": dict(sorted(router._GENERATED_BACKENDS.items())),
        "always_on_backends": dict(sorted(router._ALWAYS_ON_BACKENDS.items())),
        "optional_backends": dict(sorted(optional.items())),
        "server_ids": sorted(
            set(router._BACKENDS_BASE)
            | set(router._GENERATED_BACKENDS)
            | set(router._ALWAYS_ON_BACKENDS)
            | set(optional.values())
        ),
        "products": sorted(router._VALID_PRODUCTS),
        "toolsets": sorted(router._VALID_TOOLSETS),
        "server_platforms": dict(sorted(router._SERVER_PLATFORMS.items())),
    }


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------


def _server_modules() -> list[tuple[str, str]]:
    router = _router_module()
    return [
        *sorted(router._BACKENDS_BASE.items()),
        *sorted(router._ALWAYS_ON_BACKENDS.items()),
        *sorted(router._GENERATED_BACKENDS.items()),
        *sorted(router._OPTIONAL_BACKENDS.values()),
    ]


def _assert_catalog_env() -> None:
    wrong = {
        key: os.environ.get(key)
        for key, expected in CATALOG_ENV.items()
        if os.environ.get(key) != expected
    }
    if wrong:
        raise ProjectFactsError(
            "the complete tool catalog must be collected with "
            + ", ".join(f"{key}={value}" for key, value in sorted(CATALOG_ENV.items()))
            + f"; got {wrong}. Run scripts/project_facts.py, which pins them."
        )


def registered_tool_identities() -> dict[str, list[str]]:
    """Return ``{server_id: [tool_name, ...]}`` for the complete catalog.

    Imports each backend module and reads its MCP tool registry, so the
    result is exactly what the router can dispatch -- the same identity set
    ``scripts/ingest_tools.py`` embeds and ``scripts/validate_release.py``
    compares the LanceDB tools table against.
    """
    _assert_catalog_env()
    import importlib

    from hpe_networking_mcp.mcp_servers import _sdk_compat

    identities: dict[str, list[str]] = {}
    for server, module_path in _server_modules():
        module = importlib.import_module(module_path)
        identities[server] = _sdk_compat.tool_names(module.mcp)
    return dict(sorted(identities.items()))


def generated_operation_facts() -> dict[str, Any]:
    """Return committed generated-operation counts and names per platform."""
    paths = sorted(MANIFEST_DIR.glob("*.json"))
    if not paths:
        raise ProjectFactsError(f"no generated manifests under {MANIFEST_DIR}")
    by_platform: dict[str, int] = {}
    names: set[str] = set()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        operations = document.get("operations") or []
        by_platform[str(document.get("platform", path.stem))] = len(operations)
        names.update(str(operation["name"]) for operation in operations)
    return {
        "manifest_count": len(paths),
        "by_platform": dict(sorted(by_platform.items())),
        "total": sum(by_platform.values()),
        "_names": names,
    }


def tool_facts(*, identities: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Return per-server, generated/curated, and headline catalog totals.

    Args:
        identities: Pre-computed ``registered_tool_identities()`` result, to
            reuse a single import pass when a caller (``collect()``) also
            needs the flattened name set for another derivation. Computed
            fresh when omitted, so existing callers/tests are unaffected.
    """
    identities = identities if identities is not None else registered_tool_identities()
    generated_names: set[str] = generated_operation_facts()["_names"]

    by_server = {server: len(names) for server, names in identities.items()}
    registered_names = {name for names in identities.values() for name in names}
    registered_generated = registered_names & generated_names
    local_total = sum(by_server.get(server, 0) for server in CREDENTIAL_FREE_LOCAL_SERVERS)
    protocol_only_total = sum(
        by_server.get(server, 0) for server in PROTOCOL_ONLY_SERVERS
    )
    aggregator_total = sum(
        by_server.get(server, 0) for server in NON_PLATFORM_AGGREGATOR_SERVERS
    )
    non_api_local: dict[str, int] = {}
    for server, tool_names in NON_API_LOCAL_TOOLS.items():
        registered = set(identities.get(server, []))
        non_api_local[server] = len(registered & tool_names)
    non_api_local_total = sum(non_api_local.values())
    total = sum(by_server.values())
    return {
        "catalog_env": dict(sorted(CATALOG_ENV.items())),
        "registered_total": total,
        "by_server": dict(sorted(by_server.items())),
        "generated_registered": len(registered_generated),
        "generated_excluded": len(generated_names - registered_names),
        "curated_total": total - len(registered_generated),
        "credential_free_local": {
            server: by_server.get(server, 0) for server in CREDENTIAL_FREE_LOCAL_SERVERS
        },
        "protocol_only": {
            server: by_server.get(server, 0) for server in PROTOCOL_ONLY_SERVERS
        },
        "non_api_local": non_api_local,
        "platform_backend_total": (
            total
            - local_total
            - protocol_only_total
            - non_api_local_total
            - aggregator_total
        ),
        "platform_curated_total": (
            total
            - len(registered_generated)
            - local_total
            - protocol_only_total
            - non_api_local_total
            - aggregator_total
        ),
        "interop_tools": by_server.get("interop-core", 0),
        "non_platform_aggregators": {
            server: by_server.get(server, 0)
            for server in NON_PLATFORM_AGGREGATOR_SERVERS
        },
    }


# ---------------------------------------------------------------------------
# Router-mode client-visible tool counts
# ---------------------------------------------------------------------------


def probe_environment(
    base_env: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a reproducible child environment for a router-mode probe.

    Every ambient ``HPE_MCP_*`` variable is dropped before ``base_env`` and
    ``extra_env`` are applied, so the measured counts describe the pinned
    scenario and nothing else. Without that, a developer's exported
    ``HPE_MCP_PRODUCTS``/``HPE_MCP_ROUTER_MODE`` -- or, because
    ``mcp_servers.shared`` calls ``load_dotenv(override=False)`` at import
    time, an unrelated repo-root ``.env`` -- would silently change which
    backends the child registers and make ``docs/project-facts.json``
    machine-specific. The strict direct-all environment
    (:data:`ROUTER_MODE_ENV`) is preserved exactly: it is re-applied on top
    of the cleared variables rather than merged with whatever was inherited.

    Args:
        base_env: The pinned scenario environment (:data:`ROUTER_MODE_ENV`
            or :data:`RECOMMENDED_PROFILE_ENV`).
        extra_env: Per-call overrides layered last (the probe's own
            ``HPE_MCP_ROUTER_MODE``).

    Returns:
        A complete environment mapping for :func:`subprocess.run`, with
        ``PYTHONPATH`` prefixed by this checkout's ``src/`` so the child
        imports the working tree rather than an installed copy.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("HPE_MCP_")}
    env.update(base_env)
    env.update(extra_env or {})
    src_dir = str(REPO_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir
    )
    return env


def _run_router_mode_probe(
    extra_env: dict[str, str],
    *,
    register_direct: bool,
    base_env: dict[str, str] = ROUTER_MODE_ENV,
) -> dict[str, Any]:
    """Import ``tool_router`` fresh in a subprocess and report tool names.

    ``tool_router`` (and every backend module it loads) makes irreversible
    module-level registration decisions from ``HPE_MCP_ROUTER_MODE`` /
    ``HPE_MCP_TOOLSETS`` / etc. at *import* time. Re-importing an
    already-cached module in this process to try a second mode would just
    silently keep whatever the first import decided -- a fresh subprocess
    is the only reliable way to measure more than one router-mode scenario.

    Args:
        extra_env: Environment overrides layered onto ``base_env`` (plus
            this call's own ``HPE_MCP_ROUTER_MODE``).
        register_direct: Also call the module's own
            ``_register_direct_backend_tools()`` after import and report the
            resulting tool names. Calling it under ``HPE_MCP_ROUTER_MODE``
            values other than ``"direct"`` still reproduces direct mode's
            exact registration, because every ``if _ROUTER_MODE != "minimal"``
            router-native wrapper block earlier in the module already ran at
            import time the same way it would have under ``"direct"``; only
            the final direct-only registration step itself needs to be
            invoked explicitly. This lets one subprocess report both the
            "default" and "direct-all" tool sets instead of needing a second
            import.
        base_env: The environment layered under ``extra_env``. Defaults to
            :data:`ROUTER_MODE_ENV` (the "every toolset, every write/generated
            gate open" complete-catalog scenario); pass
            :data:`RECOMMENDED_PROFILE_ENV` to instead measure the documented
            ``central,glp,rag`` client profile.

    Returns:
        ``{"tools": [name, ...]}`` (sorted, after import), plus
        ``"direct_all_tools"`` (sorted names after direct-mode registration)
        when ``register_direct`` is True.
    """
    # Executed source, not prose: it must use the same public helper as
    # in-process code so an SDK rename breaks here too, and visibly.
    probe = (
        "import json\n"
        "from hpe_networking_mcp.mcp_servers import _sdk_compat as c\n"
        "from hpe_networking_mcp.mcp_servers import tool_router as r\n"
        "result = {'tools': c.tool_names(r.mcp)}\n"
    )
    if register_direct:
        probe += (
            "r._register_direct_backend_tools()\n"
            "result['direct_all_tools'] = c.tool_names(r.mcp)\n"
        )
    probe += "print(json.dumps(result))\n"

    env = probe_environment(base_env, extra_env)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # The child process imports several heavyweight optional-product/RAG
    # modules that may print their own startup diagnostics to stdout before
    # our final JSON line -- only the last line is the contract.
    facts: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return facts


def router_mode_facts(*, registered_names: set[str] | None = None) -> dict[str, Any]:
    """Return client-visible router tool counts for minimal/default/direct.

    The ``minimal``/``default``/``direct_all`` scenario shares
    :data:`ROUTER_MODE_ENV` (every toolset, every write/generated gate open)
    so those three describe one coherent "complete catalog" scenario instead
    of differently-scoped numbers:

    - ``minimal``: always exactly 3 (``find_tool``, ``invoke_read_tool``,
      ``invoke_tool``) -- the only tools registered unconditionally,
      independent of which backends/toolsets are loaded.
    - ``default``: minimal plus the read-only convenience wrappers
      (``list_sites``, ``ask_docs``, ``plan_tool_workflow``, etc.) that are
      registered whenever their backing backend (``central-monitoring``,
      ``rag-core``) is loaded, whatever mode is active.
    - ``direct_all``: default plus every enabled backend tool registered
      directly, skipping any name that already collided with a router-native
      wrapper above.

    A fourth, independently measured value, ``default_recommended_profile``,
    re-runs the ``default`` scenario under :data:`RECOMMENDED_PROFILE_ENV`
    (``HPE_MCP_TOOLSETS=central,glp,rag`` -- the profile every committed MCP
    client config actually ships) rather than :data:`ROUTER_MODE_ENV`'s
    "every toolset" scenario. Both currently measure 18 because ``default``
    mode's wrapper count depends only on ``central-monitoring``/``rag-core``
    being loaded (true in both scenarios), but they are measured
    independently and cross-checked so a future change that makes them
    diverge is caught immediately instead of only the "every toolset"
    number being kept up to date while the actually-documented recommended
    profile silently drifts.

    Args:
        registered_names: The flattened set of every backend tool identity
            (``design-core`` + ``interop-core`` + every platform API backend
            -- the union of ``registered_tool_identities()``'s values) to
            reconcile direct-mode's client-visible tool set against. When
            given, ``direct_all_added_native_tools`` is computed as the
            *set difference* ``direct_all_names - registered_names`` (router
            tool names with no backend-identity equivalent at all -- the
            genuinely additive router-only surface), which is independent of
            any aggregate-count arithmetic and so cannot silently paper over
            a renamed/colliding tool the way subtracting two totals could.

    Returns:
        ``{"env": ..., "recommended_profile_env": ...,
        "tools": {"minimal": 3, "default": N, "direct_all": M,
        "default_recommended_profile": N}, "direct_all_added_native_tools":
        len(direct_all_names - registered_names)}``.
    """
    minimal = _run_router_mode_probe({"HPE_MCP_ROUTER_MODE": "minimal"}, register_direct=False)
    default_and_direct = _run_router_mode_probe(
        {"HPE_MCP_ROUTER_MODE": "default"}, register_direct=True
    )
    recommended_default = _run_router_mode_probe(
        {"HPE_MCP_ROUTER_MODE": "default"},
        register_direct=False,
        base_env=RECOMMENDED_PROFILE_ENV,
    )

    minimal_names = set(minimal["tools"])
    default_names = set(default_and_direct["tools"])
    direct_all_names = set(default_and_direct["direct_all_tools"])
    recommended_default_names = set(recommended_default["tools"])

    tools = {
        "minimal": len(minimal_names),
        "default": len(default_names),
        "direct_all": len(direct_all_names),
        "default_recommended_profile": len(recommended_default_names),
    }

    if tools["default_recommended_profile"] != tools["default"]:
        raise ProjectFactsError(
            "default-mode tool count diverges between the documented "
            f"HPE_MCP_TOOLSETS=central,glp,rag profile "
            f"({tools['default_recommended_profile']} tools) and the "
            f"every-toolset scenario ({tools['default']} tools). "
            "docs/tool-catalog.md and docs/tool-router.md's router-modes "
            "tables document a single 'default' count assuming these always "
            "match; re-derive both by hand and update this module's "
            "docstring before trusting a single shared number again."
        )

    if registered_names is not None:
        direct_all_added_native = len(direct_all_names - registered_names)
        # Independent cross-check: direct_all is exactly the union of the
        # router-native default set and the full backend identity set, so
        # its size must equal len(registered_names) + the names in
        # default_names that have no backend-identity equivalent at all.
        # A mismatch means direct-mode registration skipped/added something
        # neither set accounts for (e.g. a write-gated backend tool that
        # ROUTER_MODE_ENV should have unlocked but didn't).
        expected_direct_all = len(registered_names | default_names)
        if len(direct_all_names) != expected_direct_all:
            raise ProjectFactsError(
                "direct-all router tool count is inconsistent: measured "
                f"{len(direct_all_names)} tools, but "
                f"len(registered_names | default_names) = {expected_direct_all}. "
                "This usually means ROUTER_MODE_ENV no longer unlocks every "
                "gated write tool for direct-mode registration, or a "
                "router-native wrapper was renamed; re-derive "
                "docs/tool-catalog.md's router-modes table by hand before "
                "trusting this number."
            )
    else:
        direct_all_added_native = None

    return {
        "env": dict(sorted(ROUTER_MODE_ENV.items())),
        "recommended_profile_env": dict(sorted(RECOMMENDED_PROFILE_ENV.items())),
        "tools": tools,
        "direct_all_added_native_tools": direct_all_added_native,
    }


# ---------------------------------------------------------------------------
# RAG / structured index facts
# ---------------------------------------------------------------------------


def declared_source_facts() -> dict[str, Any]:
    """Return the RAG sources declared by ingestion/source_manifest.json."""
    document = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    sources = sorted(
        str(entry["source"])
        for entry in document
        if isinstance(entry, dict) and entry.get("source")
    )
    return {"count": len(sources), "sources": sources}


def specs_tables_present(path: Path = SPECS_DB_PATH) -> set[str]:
    """Which :data:`SPECS_TABLES` the index at ``path`` actually carries.

    Resolved from the database rather than assumed, because the two builders
    produce different table sets: ``scripts/build_spec_index.py`` indexes the
    committed OpenAPI corpus and writes ``endpoints``/``schemas``/``fields``,
    while ``advisories`` and ``lifecycle_events`` come only from
    ``ingestion/ingest_docs.py``, which needs a scrape. A spec-only index is
    therefore a legitimate artifact, not a damaged one -- it is exactly what
    ships baked into the container image.

    Returns an empty set for a missing file, a non-SQLite file, or a
    placeholder database carrying none of the contract tables.

    Args:
        path: The shared structured index; defaults to ``data/specs.sqlite``.
    """
    if not path.is_file():
        return set()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - unreadable file
        return set()
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        present = {row[0] for row in rows}
    except sqlite3.DatabaseError:  # not a sqlite database at all
        return set()
    finally:
        connection.close()
    return present & set(SPECS_TABLES)


def specs_counts(path: Path = SPECS_DB_PATH) -> dict[str, int]:
    """Return exact row counts for the structured tables ``path`` carries.

    A table the index does not have is **omitted**, never reported as ``0``.
    The distinction is the whole point of a facts module: ``advisories: 0``
    asserts the corpus was consulted and held nothing, when in truth it was
    never built. An absent key says "not present"; a zero says "present and
    empty". ``scripts/package_indexes.py``'s ``_sqlite_counts`` already omits
    on the same reasoning.

    Counting every name in :data:`SPECS_TABLES` unconditionally instead
    raised ``OperationalError: no such table: advisories`` on a spec-only
    index -- a supported artifact -- aborting fact derivation mid-way.

    Keys follow :data:`SPECS_TABLES` order, so a full index still renders in
    the canonical order the committed facts file uses.

    Args:
        path: The shared structured index; defaults to ``data/specs.sqlite``.
    """
    present = specs_tables_present(path)
    if not present:
        return {}
    counts: dict[str, int] = {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in SPECS_TABLES:
            if table not in present:
                continue
            # table is always one of the hardcoded SPECS_TABLES constant
            # above, never external/user input.
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # nosec B608
            ).fetchone()[0]
    finally:
        connection.close()
    return counts


def specs_index_present(path: Path = SPECS_DB_PATH) -> bool:
    """True when ``path`` is a real structured index, not a placeholder.

    ``sqlite3.connect`` creates an empty database on first write, so a
    zero-byte ``data/specs.sqlite`` can appear in a checkout that has no
    corpus at all. Such a file satisfies ``is_file()`` while carrying none of
    :data:`SPECS_TABLES`, and counting it raises ``OperationalError`` in the
    middle of fact derivation instead of taking the documented
    no-data-checkout path.

    A file holding *some* of the expected tables is still a real index: the
    offline build writes the three OpenAPI tables and no more. It stays
    present here, and :func:`specs_counts` reports exactly what it carries
    rather than failing or inventing zeros for the rest.

    Args:
        path: The shared structured index; defaults to ``data/specs.sqlite``.
    """
    return bool(specs_tables_present(path))


def _column_counts(table: Any, column: str) -> dict[str, int] | None:
    """Value frequencies for ``column``, or None when the table lacks it.

    A LanceDB directory that does not carry the expected column is not this
    project's index -- a stub left by another tool, or an artifact from a
    different schema version. Counting it raises mid-derivation and takes
    the whole fact snapshot down; reporting it as unbuilt is the same
    no-data path a missing directory takes.
    """
    if column not in set(table.schema.names):
        return None
    rows = table.count_rows()
    values = table.search().select([column]).limit(rows).to_arrow().column(column).to_pylist()
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def index_facts() -> dict[str, Any] | None:
    """Return local index-derived counts, or None when no indexes exist.

    The counts are split by who can reproduce them. Everything under
    :data:`OFFLINE_DERIVABLE` any clone rebuilds byte-for-byte from committed
    inputs, so drift there is a real defect. Everything under
    :data:`LOCALLY_BUILT` needs the scraped prose corpus, which this project
    does not redistribute, so its absence means "not built here" and can only
    be compared where the artifact exists. Keeping both in one undifferentiated
    section is what let ``endpoints: 4106`` -- a number derived from one
    workstation's scrape, reproducible by nobody -- sit in a file named
    ``project-facts.json``.

    ``None`` is the no-data-checkout signal: callers that require indexes
    (strict release validation) treat it as a failure, while a fresh clone
    can still validate every code-derived fact.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client

    present_tables = specs_tables_present()
    if not present_tables and not (DATA_DIR / "docs.lance").is_dir():
        return None

    counts = specs_counts()
    offline: dict[str, Any] = {}
    local: dict[str, Any] = {}
    offline_specs = {t: counts[t] for t in OFFLINE_DERIVABLE_SPECS_TABLES if t in counts}
    local_specs = {t: counts[t] for t in LOCALLY_BUILT_SPECS_TABLES if t in counts}
    if offline_specs:
        offline["specs_sqlite"] = offline_specs
    if local_specs:
        local["specs_sqlite"] = local_specs

    db = lance_client.connect(DATA_DIR)
    docs = lance_client.docs_table(db)
    rows_by_source = _column_counts(docs, "source") if docs is not None else None
    if rows_by_source is not None:
        local["docs_lance"] = {
            "rows": docs.count_rows(),
            "source_count": len(rows_by_source),
            "rows_by_source": rows_by_source,
        }
    tools = lance_client.tools_table(db)
    rows_by_server = _column_counts(tools, "server") if tools is not None else None
    if rows_by_server is not None:
        local["tools_lance"] = {
            "rows": tools.count_rows(),
            "rows_by_server": rows_by_server,
        }
    return {"data_dir": DATA_DIR.name, OFFLINE_DERIVABLE: offline, LOCALLY_BUILT: local}


# ---------------------------------------------------------------------------
# Snapshot assembly / comparison
# ---------------------------------------------------------------------------


def collect(include_indexes: bool = True, include_router_modes: bool = True) -> dict[str, Any]:
    """Return the full canonical fact snapshot.

    Args:
        include_indexes: Read the local ``data/`` indexes for the RAG and
            structured-API counts. When False (or when no index exists) the
            ``indexes`` key is ``None`` and only code-derived facts are
            reported.
        include_router_modes: Measure minimal/default/direct-all
            client-visible router tool counts (see :func:`router_mode_facts`).
            This spawns two subprocesses (~10-20s) because each router mode
            makes irreversible import-time registration decisions that a
            single process cannot try more than once. Set False for fast,
            router-mode-agnostic fact checks (e.g. package/server identity
            tests); the tracked ``router_modes`` key is left in place from
            ``tracked`` when this is False and no fresh value is available
            (see :func:`write`).

    Returns:
        A JSON-serializable dict; ``indexes`` is ``None`` for a no-data
        checkout, and ``router_modes`` is ``None`` when
        ``include_router_modes`` is False.
    """
    generated = generated_operation_facts()
    generated.pop("_names", None)
    identities = registered_tool_identities()
    tools = tool_facts(identities=identities)
    registered_names = {name for names in identities.values() for name in names}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "scripts/project_facts.py",
        "note": (
            "Derived from code, committed manifests, and local indexes -- never "
            "hand-edited. Regenerate with `uv run python scripts/project_facts.py --write`."
        ),
        "package": package_facts(),
        "servers": server_facts(),
        "tools": tools,
        "router_modes": (
            router_mode_facts(registered_names=registered_names)
            if include_router_modes
            else None
        ),
        "generated_operations": generated,
        "rag_sources": declared_source_facts(),
        "indexes": index_facts() if include_indexes else None,
    }


def load(path: Path = FACTS_PATH) -> dict[str, Any]:
    """Return the tracked fact snapshot."""
    if not path.is_file():
        raise ProjectFactsError(
            f"missing {path}; generate it with `uv run python scripts/project_facts.py --write`"
        )
    snapshot: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return snapshot


def write(snapshot: dict[str, Any], path: Path = FACTS_PATH) -> Path:
    """Write ``snapshot`` to ``path`` as stable, diff-friendly JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return path


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def merge_unbuilt_index_facts(
    current: dict[str, Any] | None,
    tracked: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Carry tracked locally-built facts this checkout could not measure.

    Regenerating on a machine without the scraped corpus must not erase the
    numbers measured on one that has it -- the artifacts are git-ignored, so
    "absent here" is not evidence they changed. Offline-derivable facts are
    never carried over: they are rebuilt from committed inputs or they are
    genuinely unavailable.

    That holds when *nothing* was measured, too. ``current is None`` is
    :func:`index_facts`'s no-data-checkout signal, and carrying the tracked
    document whole across it republished offline counts nobody rebuilt: the
    committed ``vendor/openapi`` corpus could change, facts be regenerated
    without building the index, and the endpoint/schema/field numbers still
    read as freshly derived while describing a corpus that no longer exists.
    Omitting them is the same answer :func:`unbuilt_index_families` gives one
    level down -- say "not built" rather than invent a value -- and it makes a
    wrong published offline count unreachable rather than merely unlikely: an
    offline number can now only enter the document by being measured. A clone
    that wants them present builds the index
    (``python scripts/build_spec_index.py``); a clone that does not can still
    regenerate every code-derived fact, which is why this omits rather than
    fails.

    Returns ``None`` when nothing was measured and there is nothing to carry,
    preserving the no-data-checkout signal for callers that require indexes.
    """
    carried = dict((tracked or {}).get(LOCALLY_BUILT) or {})
    if current is None:
        if not carried:
            return None
        return {"data_dir": DATA_DIR.name, LOCALLY_BUILT: carried}
    if not tracked:
        return current
    carried.update(current.get(LOCALLY_BUILT) or {})
    merged = dict(current)
    merged[LOCALLY_BUILT] = carried
    return merged


#: How a locally-built family is named when it is missing. ``specs_sqlite``
#: needs the longer form: its offline tables can be present while the two
#: scrape-derived ones are not, and "specs_sqlite: not built" would then be a
#: lie about an index that exists.
_UNBUILT_LABELS = {
    "docs_lance": "docs_lance",
    "tools_lance": "tools_lance",
    "specs_sqlite": "specs_sqlite ({})".format("/".join(LOCALLY_BUILT_SPECS_TABLES)),
}


def unbuilt_index_families(current: dict[str, Any]) -> list[str]:
    """Locally-built families a fresh snapshot could not measure.

    Reported by the CLI as "not built", never as drift: the artifacts are
    git-ignored scrape output, so their absence says nothing about whether
    the tracked numbers are still right.
    """
    indexes = current.get("indexes")
    built = set((indexes or {}).get(LOCALLY_BUILT) or {})
    return [label for family, label in sorted(_UNBUILT_LABELS.items()) if family not in built]


def compare(
    tracked: dict[str, Any],
    current: dict[str, Any],
    *,
    require_indexes: bool,
    ignore_indexes: bool = False,
) -> list[str]:
    """Return human-readable drift between a tracked and a fresh snapshot.

    Index facts are compared according to who can reproduce them. The
    :data:`OFFLINE_DERIVABLE` section is compared whenever the structured
    index exists, and ``ignore_indexes`` does not silence it: those counts
    come from ``vendor/openapi``, which is committed, so there is no
    "my corpus differs" excuse for them. The :data:`LOCALLY_BUILT` section is
    compared only where the artifact is present.

    Args:
        tracked: The committed ``docs/project-facts.json`` content.
        current: A freshly derived snapshot from :func:`collect`.
        require_indexes: Fail when the fresh snapshot cannot derive the
            offline facts at all. It does *not* demand the scraped
            artifacts: strict validation must not require a crawl nobody
            else can reproduce.
        ignore_indexes: Skip comparing the locally-built section even when
            its artifacts exist. Used by CI against a pinned older release
            bundle whose corpus is not the developer workstation snapshot.

    Returns:
        A list of difference descriptions; empty means no drift.
    """
    problems: list[str] = []
    if tracked.get("schema_version") != SCHEMA_VERSION:
        return [
            f"docs/project-facts.json schema_version {tracked.get('schema_version')!r} != "
            f"{SCHEMA_VERSION}; regenerate it"
        ]

    indexes = current.get("indexes")
    offline_built = set((indexes or {}).get(OFFLINE_DERIVABLE) or {})
    local_built = set((indexes or {}).get(LOCALLY_BUILT) or {})
    if require_indexes and not offline_built:
        problems.append(
            "data/specs.sqlite is not built: strict validation compares the "
            "offline-derivable OpenAPI counts -- build it with "
            "`python scripts/build_spec_index.py`"
        )

    def compare_index_key(key: str) -> bool:
        if indexes is None:
            return False
        parts = key.split(".")
        section = parts[1] if len(parts) > 1 else ""
        family = parts[2] if len(parts) > 2 else ""
        if section == OFFLINE_DERIVABLE:
            return family in offline_built
        if section == LOCALLY_BUILT:
            return not ignore_indexes and family in local_built
        # ``data_dir``, or a fact added without choosing a side. Comparing it
        # is the strict default on purpose: an unclassified fact must fail
        # the gate rather than land silently in the lenient section.
        return True

    skip_router_modes = current.get("router_modes") is None

    ignored = {"note", "generated_by"}
    tracked_flat = _flatten({k: v for k, v in tracked.items() if k not in ignored})
    current_flat = _flatten({k: v for k, v in current.items() if k not in ignored})
    for key in sorted(set(tracked_flat) | set(current_flat)):
        if key == "indexes" or key.startswith("indexes."):
            if not compare_index_key(key):
                continue
        if skip_router_modes and key.startswith("router_modes"):
            continue
        old = tracked_flat.get(key, "<absent>")
        new = current_flat.get(key, "<absent>")
        if old != new:
            problems.append(f"{key}: tracked {old!r} != derived {new!r}")
    return problems
