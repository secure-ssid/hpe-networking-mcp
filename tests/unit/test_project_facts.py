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
    profile exposes 27 tools, matching (but independently measured from) the
    "every toolset" ``default`` scenario -- docs previously claimed 16.
    """
    router_modes = TRACKED["router_modes"]
    tools = router_modes["tools"]

    assert tools["default_recommended_profile"] == 27
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

    assert tools["registered_total"] == 6731  # complete registered backend identities
    assert tools["platform_backend_total"] == 6712  # platform API total / compatibility floor
    assert router_tools["minimal"] == 3
    assert router_tools["default"] == 27
    assert router_tools["direct_all"] == 6743
    assert tools["non_api_local"] == {
        "glp-core": 1,
        "rag-core": 1,
        "catalog-core": 2,
    }


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


def test_compare_ignore_indexes_skips_locally_built_drift():
    drifted = json.loads(json.dumps(TRACKED))
    drifted["indexes"][project_facts.LOCALLY_BUILT]["docs_lance"]["rows"] = 1

    ignored = project_facts.compare(
        TRACKED, drifted, require_indexes=True, ignore_indexes=True
    )
    compared = project_facts.compare(TRACKED, drifted, require_indexes=True)

    assert ignored == []
    assert any("docs_lance.rows" in problem for problem in compared)


def test_ignore_indexes_cannot_silence_an_offline_count():
    """The escape hatch covers the scraped corpus, never the committed one.

    ``--ignore-index-facts`` exists because a pinned older bundle's *scrape*
    is not the developer's. ``vendor/openapi`` is in the tree, so a clone
    that disagrees about its counts is wrong, not merely different.
    """
    drifted = json.loads(json.dumps(TRACKED))
    drifted["indexes"][project_facts.OFFLINE_DERIVABLE]["specs_sqlite"]["endpoints"] += 1

    problems = project_facts.compare(
        TRACKED, drifted, require_indexes=False, ignore_indexes=True
    )

    assert any("offline_derivable.specs_sqlite.endpoints" in problem for problem in problems)


def test_a_spec_only_index_satisfies_strict_validation():
    """A clone that ran the offline build owes nothing further.

    Strict validation used to demand ``data/docs.lance`` too -- a scrape
    this project does not distribute -- so the strict path was unreachable
    for anyone but its author.
    """
    spec_only = json.loads(json.dumps(TRACKED))
    spec_only["indexes"][project_facts.LOCALLY_BUILT] = {}

    assert project_facts.compare(TRACKED, spec_only, require_indexes=True) == []
    assert project_facts.unbuilt_index_families(spec_only) == [
        "docs_lance",
        "specs_sqlite (advisories/lifecycle_events)",
        "tools_lance",
    ]


def test_a_wrong_offline_count_fails_even_without_require_indexes():
    """The gate has to bite, or partitioning only made it quieter."""
    wrong = json.loads(json.dumps(TRACKED))
    wrong["indexes"][project_facts.OFFLINE_DERIVABLE]["specs_sqlite"]["fields"] = 1

    problems = project_facts.compare(TRACKED, wrong, require_indexes=False)

    assert any("offline_derivable.specs_sqlite.fields" in problem for problem in problems)


def test_compare_requires_the_offline_index_in_strict_mode():
    index_free = json.loads(json.dumps(TRACKED))
    index_free["indexes"] = None

    strict = project_facts.compare(TRACKED, index_free, require_indexes=True)
    lenient = project_facts.compare(TRACKED, index_free, require_indexes=False)

    assert any("data/specs.sqlite is not built" in problem for problem in strict)
    assert any("scripts/build_spec_index.py" in problem for problem in strict)
    assert lenient == []


def test_merge_keeps_locally_built_facts_a_checkout_cannot_measure():
    """Regenerating without the scrape must not delete what it cannot see."""
    fresh = {
        "data_dir": "data",
        project_facts.OFFLINE_DERIVABLE: {"specs_sqlite": {"endpoints": 2734}},
        project_facts.LOCALLY_BUILT: {},
    }

    merged = project_facts.merge_unbuilt_index_facts(fresh, TRACKED["indexes"])

    assert merged[project_facts.OFFLINE_DERIVABLE] == fresh[project_facts.OFFLINE_DERIVABLE]
    assert (
        merged[project_facts.LOCALLY_BUILT]
        == TRACKED["indexes"][project_facts.LOCALLY_BUILT]
    )


def test_merge_never_republishes_offline_counts_nothing_measured():
    """A no-data checkout must not re-emit counts it did not rebuild.

    ``current is None`` is the no-data-checkout signal: no ``data/specs.sqlite``
    and no Lance tables. Carrying the tracked document whole across it kept
    ``offline_derivable`` -- counts derived from the committed
    ``vendor/openapi`` corpus -- so ``vendor/openapi`` could change, facts be
    regenerated without building the index, and the endpoint/schema/field
    numbers ship unchanged while looking freshly derived. The partition exists
    precisely so those three numbers are verifiable from committed inputs.
    """
    merged = project_facts.merge_unbuilt_index_facts(None, TRACKED["indexes"])

    assert project_facts.OFFLINE_DERIVABLE not in merged
    assert (
        merged[project_facts.LOCALLY_BUILT]
        == TRACKED["indexes"][project_facts.LOCALLY_BUILT]
    )
    assert merged["data_dir"] == TRACKED["indexes"]["data_dir"]


def test_merge_reports_no_index_when_only_offline_facts_are_tracked():
    """Nothing measured and nothing carryable stays the no-data signal.

    An offline-only tracked block has nothing a git-ignored artifact could
    excuse, so the merge yields ``None`` -- the same value :func:`collect`
    records for a fresh clone -- rather than an index document asserting
    counts this checkout never touched.
    """
    offline_only = {
        "data_dir": "data",
        project_facts.OFFLINE_DERIVABLE: {"specs_sqlite": {"endpoints": 2734}},
        project_facts.LOCALLY_BUILT: {},
    }

    assert project_facts.merge_unbuilt_index_facts(None, offline_only) is None
    assert project_facts.merge_unbuilt_index_facts(None, {}) is None
    assert project_facts.merge_unbuilt_index_facts(None, None) is None


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


def test_tracked_offline_counts_match_a_build_of_the_committed_corpus(tmp_path):
    """The integrity property the partition exists to create.

    ``indexes.offline_derivable`` claims counts that any clone reproduces
    from ``vendor/openapi`` with ``scripts/build_spec_index.py``. This test
    is the proof: it builds that corpus and compares. Nothing here reads
    ``data/``, so CI verifies the published OpenAPI numbers from committed
    inputs alone -- which is exactly what the previous numbers (4,106
    endpoints, derived from one workstation's scrape) could never support.
    """
    from scripts.build_spec_index import VENDOR_DIR, build_spec_index

    built = build_spec_index(VENDOR_DIR, tmp_path / "specs.sqlite")
    tracked = TRACKED["indexes"][project_facts.OFFLINE_DERIVABLE]["specs_sqlite"]

    assert tracked == {table: built[table] for table in tracked}
    assert set(tracked) == set(project_facts.OFFLINE_DERIVABLE_SPECS_TABLES)


def test_every_tracked_index_fact_picked_a_side():
    """A fact added without choosing a side must not default into leniency."""
    indexes = TRACKED["indexes"]
    assert set(indexes) == {
        "data_dir",
        project_facts.OFFLINE_DERIVABLE,
        project_facts.LOCALLY_BUILT,
    }
    assert set(indexes[project_facts.OFFLINE_DERIVABLE]) == {"specs_sqlite"}
    assert set(indexes[project_facts.LOCALLY_BUILT]) <= {
        "specs_sqlite",
        *project_facts.LOCALLY_BUILT_INDEX_FAMILIES,
    }
    tables = set(indexes[project_facts.LOCALLY_BUILT].get("specs_sqlite") or {})
    assert tables <= set(project_facts.LOCALLY_BUILT_SPECS_TABLES)


def test_a_lance_table_without_the_expected_column_reads_as_unbuilt():
    """A foreign artifact under ``data/`` must not take fact derivation down.

    A stub ``docs.lance`` written by another tool has no ``source`` column,
    and counting it raised ``Schema error: No field named source`` in the
    middle of :func:`index_facts` -- taking every code-derived fact with it.
    Unreadable is the same answer as unbuilt.
    """

    class _Stub:
        schema = type("_Schema", (), {"names": ["id", "vector"]})()

        def count_rows(self):  # pragma: no cover - must never be reached
            raise AssertionError("counted a table whose schema was rejected")

    assert project_facts._column_counts(_Stub(), "source") is None


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


def test_cli_requires_indexes_by_default():
    from scripts import project_facts as facts_cli

    args = facts_cli._build_parser().parse_args([])

    assert args.require_indexes is True


def test_cli_no_require_indexes_is_the_opt_out():
    from scripts import project_facts as facts_cli

    args = facts_cli._build_parser().parse_args(["--no-require-indexes"])

    assert args.require_indexes is False


def test_write_refuses_index_free_facts_by_default(monkeypatch, capsys):
    from scripts import project_facts as facts_cli

    snapshot = json.loads(json.dumps(TRACKED))
    snapshot["indexes"] = None
    monkeypatch.setattr(
        facts_cli.project_facts, "collect", lambda include_router_modes=True: snapshot
    )
    wrote: list[object] = []
    monkeypatch.setattr(
        facts_cli.project_facts,
        "write",
        lambda current, path=None: wrote.append(current) or project_facts.FACTS_PATH,
    )
    monkeypatch.setattr(sys, "argv", ["project_facts.py", "--write"])

    assert facts_cli.main() == 1
    assert wrote == []
    assert "Refusing to write" in capsys.readouterr().err


def test_write_warns_when_carrying_unbuilt_locally_built_facts(monkeypatch, capsys):
    from scripts import project_facts as facts_cli

    fresh = json.loads(json.dumps(TRACKED))
    fresh["indexes"] = {
        "data_dir": "data",
        project_facts.OFFLINE_DERIVABLE: TRACKED["indexes"][project_facts.OFFLINE_DERIVABLE],
        project_facts.LOCALLY_BUILT: {},
    }
    monkeypatch.setattr(
        facts_cli.project_facts,
        "collect",
        lambda include_router_modes=True: json.loads(json.dumps(fresh)),
    )
    monkeypatch.setattr(
        facts_cli.project_facts, "load", lambda: json.loads(json.dumps(TRACKED))
    )
    written: list[object] = []
    monkeypatch.setattr(
        facts_cli.project_facts,
        "write",
        lambda current, path=None: written.append(current) or project_facts.FACTS_PATH,
    )
    monkeypatch.setattr(sys, "argv", ["project_facts.py", "--write"])

    assert facts_cli.main() == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "tools_lance" in err
    assert written
