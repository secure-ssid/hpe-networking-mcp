"""Canonical project facts stay derived, complete, and drift-detecting.

``docs/project-facts.json`` is the single machine-readable source every doc
and gate reads counts from. These tests pin the three properties that make
it trustworthy: it is *derived* (never hand-entered), it *agrees with the
other derivation points* (the router backend map and the ingest catalog),
and its comparison helper actually *fails* on drift instead of shrugging.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from hpe_networking_mcp.pipeline import project_facts
from scripts import ingest_tools

TRACKED = project_facts.load()


def test_complete_catalog_pins_every_generated_tool_flag():
    expected = {
        "HPE_MCP_CENTRAL_GENERATED_TOOLS",
        "HPE_MCP_GLP_GENERATED_TOOLS",
        "HPE_MCP_AOS8_GENERATED_TOOLS",
        "HPE_MCP_EDGECONNECT_GENERATED_TOOLS",
        "HPE_MCP_APSTRA_GENERATED_TOOLS",
        "HPE_MCP_MIST_GENERATED_TOOLS",
        "HPE_MCP_CLEARPASS_GENERATED_TOOLS",
        "HPE_MCP_UXI_GENERATED_TOOLS",
    }

    assert set(project_facts.GENERATED_TOOL_ENV) == expected
    assert project_facts.GENERATED_TOOL_ENV.items() <= project_facts.CATALOG_ENV.items()


def test_tracked_facts_use_current_schema_version():
    assert TRACKED["schema_version"] == project_facts.SCHEMA_VERSION


def test_tracked_package_facts_match_pyproject():
    assert TRACKED["package"] == project_facts.package_facts()


def test_tracked_server_ids_match_router_backend_map():
    assert TRACKED["servers"] == project_facts.server_facts()


def test_tracked_generated_operation_counts_match_committed_manifests():
    derived = project_facts.generated_operation_facts()
    derived.pop("_names")

    assert TRACKED["generated_operations"] == derived


def test_tracked_rag_sources_match_declared_source_manifest():
    assert TRACKED["rag_sources"] == project_facts.declared_source_facts()


def test_declared_sources_match_ingestion_manifest_entries():
    declared = project_facts.declared_source_facts()
    manifest = json.loads(project_facts.SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert declared["count"] == len(manifest)
    assert declared["count"] == len(declared["sources"])


def test_project_facts_server_map_matches_ingest_tools_catalog():
    """The facts module and the index builder must not name servers differently.

    ``project_facts`` derives servers from ``tool_router``'s backend maps while
    ``scripts/ingest_tools.py`` keeps its own list; a rename applied to only
    one of them is exactly how the tools index ended up holding pre-rename
    ``aruba-*`` server IDs.
    """
    facts_servers = set(project_facts.server_facts()["server_ids"])
    ingest_servers = {server for server, _ in ingest_tools.SERVERS}
    ingest_servers |= {server for server, _ in ingest_tools.OPTIONAL_SERVERS.values()}

    assert facts_servers == ingest_servers


def test_tracked_tool_totals_are_internally_consistent():
    tools = TRACKED["tools"]

    assert tools["registered_total"] == sum(tools["by_server"].values())
    assert tools["curated_total"] == tools["registered_total"] - tools["generated_registered"]
    local = sum(tools["credential_free_local"].values())
    protocol_only = sum(tools["protocol_only"].values())
    non_api_local = sum(tools["non_api_local"].values())
    aggregators = sum(tools["non_platform_aggregators"].values())
    assert tools["platform_backend_total"] == (
        tools["registered_total"] - local - protocol_only - non_api_local - aggregators
    )
    assert tools["interop_tools"] == tools["by_server"]["interop-core"]
    assert tools["non_platform_aggregators"]["site-health"] == 1


def test_tracked_router_modes_are_internally_consistent():
    """Router-mode counts stay a coherent minimal <= default <= direct-all chain.

    Regression guard for the "6,703 complete / 6,710 direct-all" drift:
    those figures ignored that direct mode adds *router-native* tools on top
    of the *complete* registered-identity total (design-core + interop-core +
    every platform API backend), not just the platform-only subtotal.
    """
    router_modes = TRACKED["router_modes"]
    tools = router_modes["tools"]

    assert tools["minimal"] == 3
    assert tools["minimal"] < tools["default"] < tools["direct_all"]
    assert (
        tools["direct_all"]
        == TRACKED["tools"]["registered_total"] + router_modes["direct_all_added_native_tools"]
    )
    # The env this is measured under must be able to load every toolset AND
    # every optional product without HPE_MCP_PRODUCTS -- "all" is a
    # HPE_MCP_TOOLSETS-only value; HPE_MCP_PRODUCTS=all is invalid.
    assert router_modes["env"]["HPE_MCP_TOOLSETS"] == "all"
    assert "HPE_MCP_PRODUCTS" not in router_modes["env"]


def test_tracked_default_recommended_profile_matches_documented_client_configs():
    """The documented client profile's default-mode count is measured, not assumed.

    Regression guard for a live probe that found the actually-documented
    ``HPE_MCP_ROUTER_MODE=default``/``HPE_MCP_TOOLSETS=central,glp,rag``
    profile exposes 18 tools, matching (but independently measured from) the
    "every toolset" ``default`` scenario -- docs previously claimed 16.
    """
    router_modes = TRACKED["router_modes"]
    tools = router_modes["tools"]

    assert tools["default_recommended_profile"] == 18
    assert tools["default_recommended_profile"] == tools["default"]
    assert router_modes["recommended_profile_env"] == {
        "HPE_MCP_ACCESS_PROFILE": "custom",
        "HPE_MCP_TOOLSETS": "central,glp,rag",
    }


@pytest.mark.slow
def test_tracked_router_modes_match_a_fresh_probe():
    """The expensive (~15-20s, all-subprocess) end-to-end regression check.

    Shells out to ``scripts/project_facts.py --print`` in a brand-new
    process rather than calling ``registered_tool_identities()`` /
    ``router_mode_facts()`` in-process: this test file runs inside the full
    ``pytest tests/unit`` suite, where other test modules already imported
    ``hpe_networking_mcp.mcp_servers.glp``/``aos8``/etc. under different
    (or no) ``HPE_MCP_GLP_GENERATED_TOOLS``/``HPE_MCP_PRODUCT_ACCESS``
    values before this test runs -- Python caches those imports, so calling
    the in-process functions here would silently measure whatever the
    *first* import in the whole test session decided, not what this test
    asks for. ``scripts/project_facts.py`` avoids exactly this by pinning
    the env before importing anything, in its own fresh interpreter --
    the same reason ``scripts/validate_release.py``'s own
    ``_registered_tool_identities()`` shells out for the ``products="all"``
    case instead of importing in-process.
    """
    script = project_facts.REPO_ROOT / "scripts" / "project_facts.py"
    result = subprocess.run(
        [sys.executable, str(script), "--print"],
        cwd=project_facts.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            **{name: "0" for name in project_facts.GENERATED_TOOL_ENV},
        },
    )

    assert result.returncode == 0, result.stderr
    fresh = json.loads(result.stdout)

    assert fresh["tools"]["registered_total"] == TRACKED["tools"]["registered_total"]
    assert fresh["router_modes"] == TRACKED["router_modes"]


def test_published_canonical_counts_match_the_documented_contract():
    """The five numbers every doc/page publishes, pinned in one place.

    Each is *derived* elsewhere (``tools`` from importing the backend
    registries, ``router_modes`` from fresh subprocess probes) -- this test
    pins the currently published values so a regeneration that changes any
    of them fails loudly here and forces the docs, SVG assets, package
    description, and release-validation floor to be updated in the same
    change, instead of the fact file quietly moving ahead of the prose.
    """
    tools = TRACKED["tools"]
    router_tools = TRACKED["router_modes"]["tools"]

    assert tools["registered_total"] == 6727  # complete registered backend identities
    assert tools["platform_backend_total"] == 6711  # platform API total / compatibility floor
    assert router_tools["minimal"] == 3
    assert router_tools["default"] == 18
    assert router_tools["direct_all"] == 6734
    assert tools["non_api_local"] == {"glp-core": 1}


def test_router_mode_probe_environment_is_reproducible():
    """Ambient ``HPE_MCP_*`` values must never leak into a router-mode probe.

    ``mcp_servers.shared`` calls ``load_dotenv(override=False)`` at import
    time, so an inherited ``HPE_MCP_PRODUCTS``/``HPE_MCP_ROUTER_MODE`` (or a
    developer's repo-root ``.env``) would otherwise change which backends the
    child registers and make ``docs/project-facts.json`` machine-specific.
    """
    original = os.environ.copy()
    os.environ["HPE_MCP_PRODUCTS"] = "clearpass"
    os.environ["HPE_MCP_ROUTER_MODE"] = "direct"
    os.environ["HPE_MCP_TOOLSETS"] = "rag"
    try:
        env = project_facts.probe_environment(
            project_facts.ROUTER_MODE_ENV, {"HPE_MCP_ROUTER_MODE": "minimal"}
        )
    finally:
        os.environ.clear()
        os.environ.update(original)

    assert "HPE_MCP_PRODUCTS" not in env
    assert env["HPE_MCP_ROUTER_MODE"] == "minimal"
    # The strict direct-all environment survives intact.
    for key, value in project_facts.ROUTER_MODE_ENV.items():
        if key != "HPE_MCP_ROUTER_MODE":
            assert env[key] == value
    assert env["PYTHONPATH"].startswith(str(project_facts.REPO_ROOT / "src"))


def test_router_mode_probe_environment_supports_the_recommended_profile():
    env = project_facts.probe_environment(project_facts.RECOMMENDED_PROFILE_ENV)

    assert env["HPE_MCP_TOOLSETS"] == "central,glp,rag"
    assert "HPE_MCP_PRODUCT_ACCESS" not in env


def test_tracked_generated_registered_never_exceeds_manifest_operations():
    tools, generated = TRACKED["tools"], TRACKED["generated_operations"]

    assert tools["generated_registered"] + tools["generated_excluded"] == generated["total"]


def test_compare_reports_no_drift_for_identical_snapshots():
    assert project_facts.compare(TRACKED, TRACKED, require_indexes=True) == []


def test_compare_detects_a_changed_count():
    drifted = json.loads(json.dumps(TRACKED))
    drifted["tools"]["registered_total"] += 1

    problems = project_facts.compare(drifted, TRACKED, require_indexes=False)

    assert any("tools.registered_total" in problem for problem in problems)


def test_compare_detects_a_renamed_server_id():
    drifted = json.loads(json.dumps(TRACKED))
    drifted["tools"]["by_server"]["aruba-glp"] = drifted["tools"]["by_server"].pop("glp-core")

    problems = project_facts.compare(drifted, TRACKED, require_indexes=False)

    assert any("aruba-glp" in problem for problem in problems)
    assert any("glp-core" in problem for problem in problems)


def test_compare_rejects_a_stale_schema_version():
    stale = json.loads(json.dumps(TRACKED))
    stale["schema_version"] = project_facts.SCHEMA_VERSION - 1

    problems = project_facts.compare(stale, TRACKED, require_indexes=False)

    assert len(problems) == 1
    assert "schema_version" in problems[0]


def test_compare_ignore_indexes_skips_corpus_drift():
    drifted = json.loads(json.dumps(TRACKED))
    drifted["indexes"]["docs_lance"]["rows"] = 1

    ignored = project_facts.compare(
        TRACKED, drifted, require_indexes=True, ignore_indexes=True
    )
    compared = project_facts.compare(TRACKED, drifted, require_indexes=True)

    assert ignored == []
    assert any("indexes.docs_lance.rows" in problem for problem in compared)


def test_compare_requires_indexes_in_strict_mode():
    index_free = json.loads(json.dumps(TRACKED))
    index_free["indexes"] = None

    strict = project_facts.compare(TRACKED, index_free, require_indexes=True)
    lenient = project_facts.compare(TRACKED, index_free, require_indexes=False)

    assert any("no local indexes found" in problem for problem in strict)
    assert lenient == []


def test_registered_identities_require_the_pinned_catalog_env(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    with pytest.raises(project_facts.ProjectFactsError) as excinfo:
        project_facts.registered_tool_identities()

    assert "HPE_MCP_PRODUCT_ACCESS=read-write" in str(excinfo.value)


def test_specs_counts_cover_every_contract_table(tmp_path):
    import sqlite3

    db_path = tmp_path / "specs.sqlite"
    with sqlite3.connect(db_path) as connection:
        for table in project_facts.SPECS_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
            connection.execute(f"INSERT INTO {table} VALUES (1)")

    assert project_facts.specs_counts(db_path) == dict.fromkeys(project_facts.SPECS_TABLES, 1)


def test_placeholder_specs_db_is_not_mistaken_for_an_index(tmp_path):
    """An empty ``specs.sqlite`` must read as *absent*, not as an index.

    ``sqlite3.connect`` creates the file on first write, so a checkout with
    no corpus can still end up with a zero-byte ``data/specs.sqlite``.
    Counting it raises ``OperationalError`` mid-derivation instead of taking
    the documented no-data path, which is how a stray file turns into a hard
    CI failure.
    """
    import sqlite3

    empty = tmp_path / "specs.sqlite"
    sqlite3.connect(empty).close()
    assert empty.is_file()
    assert project_facts.specs_index_present(empty) is False

    foreign = tmp_path / "not-sqlite.sqlite"
    foreign.write_text("this is not a database", encoding="utf-8")
    assert project_facts.specs_index_present(foreign) is False

    real = tmp_path / "real.sqlite"
    with sqlite3.connect(real) as connection:
        for table in project_facts.SPECS_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
    assert project_facts.specs_index_present(real) is True


@pytest.mark.skipif(
    project_facts.specs_tables_present() != set(project_facts.SPECS_TABLES),
    reason=(
        "the committed counts describe a complete index; a checkout with no "
        "index, or with the spec-only index the offline build and the "
        "container image produce, is a different artifact they do not describe"
    ),
)
def test_tracked_specs_counts_match_the_local_structured_index():
    assert TRACKED["indexes"]["specs_sqlite"] == project_facts.specs_counts()


def test_spec_only_index_counts_what_it_has_and_omits_what_it_does_not(tmp_path):
    """The regression guard for the index baked into the container image.

    ``scripts/build_spec_index.py`` indexes the committed ``vendor/openapi``
    corpus offline and writes ``endpoints``/``schemas``/``fields``.
    ``advisories`` and ``lifecycle_events`` come only from a scrape, so they
    are absent by construction, not by damage. Counting all five
    unconditionally raised ``OperationalError: no such table: advisories``
    and aborted fact derivation the moment that index existed.

    Built with the real builder, not a hand-rolled fixture: a fixture with
    three empty tables would pass while the shipped artifact still failed.

    The absent tables must be *omitted*. ``advisories: 0`` would assert the
    corpus was consulted and held nothing, which is a fabricated fact.
    """
    from scripts.build_spec_index import VENDOR_DIR, build_spec_index

    db_path = tmp_path / "specs.sqlite"
    built = build_spec_index(VENDOR_DIR, db_path)

    assert project_facts.specs_index_present(db_path) is True
    assert project_facts.specs_tables_present(db_path) == {"endpoints", "schemas", "fields"}

    counts = project_facts.specs_counts(db_path)

    assert counts == {
        "endpoints": built["endpoints"],
        "schemas": built["schemas"],
        "fields": built["fields"],
    }
    assert "advisories" not in counts
    assert "lifecycle_events" not in counts
    # Canonical order, so a full index still renders as the facts file does.
    assert list(counts) == [t for t in project_facts.SPECS_TABLES if t in counts]


def test_placeholder_specs_db_counts_nothing_instead_of_raising(tmp_path):
    import sqlite3

    empty = tmp_path / "specs.sqlite"
    sqlite3.connect(empty).close()

    assert project_facts.specs_counts(empty) == {}
    assert project_facts.specs_counts(tmp_path / "absent.sqlite") == {}
