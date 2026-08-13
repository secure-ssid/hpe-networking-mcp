"""Unit tests for scripts/refresh_rag_sources.py -- orchestration without network.

No refresh is actually run anywhere in this file: every test either builds a
plan (which executes nothing) or runs a plan through an injected fake runner.
That is deliberate -- the orchestration contract is exactly what must stay
verifiable while external source refresh is disabled.

Covered:
- the plan is declarative over ``ingestion/source_manifest.json``'s
  ``scraper`` + ``extra_scripts`` and the structured step table;
- fetching steps require explicit ``--refresh-sources`` consent;
- the snapshot covers docs.lance, tools.lance, specs.sqlite, the generated
  operation manifests, and both local data manifests; and
- any failing step (including the eval gate) restores every one of them,
  including deleting artifacts the failed run created.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from hpe_networking_mcp.pipeline import project_facts
from scripts import refresh_rag_sources as refresh


@pytest.fixture
def manifest():
    return [
        {
            "source": "vsg_docs",
            "output_dir": "ingestion/sources/vsg_docs",
            "scraper": "ingestion/scrape_vsg.py",
            "extra_scripts": ["ingestion/discover_vsg_urls.py"],
        },
        {
            "source": "feature_navigator",
            "output_dir": "ingestion/sources/feature_navigator",
            "scraper": None,
        },
        {
            "source": "product_specs",
            "output_dir": "ingestion/sources/product_specs",
            "scraper": "ingestion/scrape_apinext_specs.py",
        },
        {
            "source": "security_advisories",
            "output_dir": "ingestion/sources/security_advisories",
            "scraper": None,
        },
    ]


class TestPlanIsDeclarative:
    def test_scraper_and_extra_scripts_come_from_the_manifest(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)
        commands = [step["command"] for step in plan["steps"]]

        assert ["ingestion/scrape_vsg.py"] in commands
        assert ["ingestion/discover_vsg_urls.py"] in commands
        kinds = {step["kind"] for step in plan["steps"]}
        assert refresh.STEP_SCRAPER in kinds
        assert refresh.STEP_EXTRA_SCRIPT in kinds

    def test_source_without_a_scraper_is_reported_unrunnable_not_skipped(self, manifest):
        plan = refresh.build_plan(["feature_navigator"], manifest=manifest, refresh_sources=True)

        assert plan["steps"] == []
        assert plan["unrunnable"][0]["source"] == "feature_navigator"
        assert "no scraper" in plan["unrunnable"][0]["reason"]

    def test_unknown_source_is_reported_unrunnable(self, manifest):
        plan = refresh.build_plan(["not_a_source"], manifest=manifest, refresh_sources=True)

        assert plan["unrunnable"][0]["reason"].startswith("not declared")

    def test_structured_security_lifecycle_step_is_source_triggered(self, manifest):
        plan = refresh.build_plan(
            ["security_advisories"], manifest=manifest, refresh_sources=True
        )
        kinds = [step["kind"] for step in plan["steps"]]

        assert refresh.STEP_SECURITY_LIFECYCLE in kinds

    def test_product_spec_step_is_source_triggered(self, manifest):
        plan = refresh.build_plan(["product_specs"], manifest=manifest, refresh_sources=True)
        kinds = [step["kind"] for step in plan["steps"]]

        assert refresh.STEP_PRODUCT_SPEC in kinds

    def test_generated_tool_index_and_manifest_steps_follow_a_refresh(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)
        kinds = [step["kind"] for step in plan["steps"]]

        assert refresh.STEP_GENERATED_TOOL in kinds
        assert refresh.STEP_INDEX_REBUILD in kinds
        assert refresh.STEP_TOOL_INDEX_REBUILD in kinds
        assert refresh.STEP_LOCAL_MANIFESTS in kinds
        assert kinds[-1] == refresh.STEP_EVAL_GATE

    def test_eval_gate_is_the_last_step_and_flagged_as_a_gate(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)

        assert plan["steps"][-1]["gate"] is True

    def test_skip_eval_gate_removes_only_the_gate(self, manifest):
        plan = refresh.build_plan(
            ["vsg_docs"], manifest=manifest, refresh_sources=True, skip_eval_gate=True
        )
        kinds = [step["kind"] for step in plan["steps"]]

        assert refresh.STEP_EVAL_GATE not in kinds
        assert refresh.STEP_INDEX_REBUILD in kinds

    def test_no_changed_sources_produces_no_post_refresh_steps(self, manifest):
        plan = refresh.build_plan([], manifest=manifest, refresh_sources=True)

        assert plan["steps"] == []

    def test_fetching_steps_require_explicit_refresh_consent(self, manifest):
        plan = refresh.build_plan(
            ["product_specs"], manifest=manifest, refresh_sources=False
        )
        reasons = [item["reason"] for item in plan["unrunnable"]]

        assert any("--refresh-sources" in reason for reason in reasons)
        assert plan["refresh_sources"] is False
        assert "no network call" in plan["notes"]

    def test_plan_records_every_snapshot_target(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)
        labels = {target["label"] for target in plan["snapshot_targets"]}

        assert labels == {
            "docs.lance",
            "tools.lance",
            "specs.sqlite",
            "generated_manifests",
            "SOURCE-MANIFEST.json",
            "INDEX-MANIFEST.json",
        }

    def test_plan_is_json_serializable(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)

        assert json.loads(json.dumps(plan))["changed_sources"] == ["vsg_docs"]

    def test_committed_manifest_plans_without_error(self):
        """The real manifest must produce a plan for every declared source."""
        sources = [entry["source"] for entry in refresh.load_manifest()]
        plan = refresh.build_plan(sources, refresh_sources=True)

        assert plan["steps"]
        planned = {
            step["command"][0] for step in plan["steps"] if step["kind"] == refresh.STEP_SCRAPER
        }
        declared = {
            entry["scraper"] for entry in refresh.load_manifest() if entry.get("scraper")
        }
        assert planned == declared


@pytest.fixture
def snapshot_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    docs = data / "docs.lance"
    docs.mkdir()
    (docs / "part.bin").write_text("docs-v1", encoding="utf-8")
    tools = data / "tools.lance"
    tools.mkdir()
    (tools / "part.bin").write_text("tools-v1", encoding="utf-8")
    specs = data / "specs.sqlite"
    specs.write_text("specs-v1", encoding="utf-8")
    generated = tmp_path / "manifests"
    generated.mkdir()
    (generated / "central.json").write_text("central-v1", encoding="utf-8")
    source_manifest = data / "SOURCE-MANIFEST.json"
    source_manifest.write_text("source-v1", encoding="utf-8")
    index_manifest = data / "INDEX-MANIFEST.json"

    targets = (
        ("docs.lance", docs),
        ("tools.lance", tools),
        ("specs.sqlite", specs),
        ("generated_manifests", generated),
        ("SOURCE-MANIFEST.json", source_manifest),
        ("INDEX-MANIFEST.json", index_manifest),  # deliberately absent
    )
    monkeypatch.setattr(refresh, "DATA_DIR", data)
    monkeypatch.setattr(refresh, "SNAPSHOT_TARGETS", targets)
    return {
        "data": data,
        "docs": docs,
        "tools": tools,
        "specs": specs,
        "generated": generated,
        "source_manifest": source_manifest,
        "index_manifest": index_manifest,
        "targets": targets,
    }


class TestTransactionalSnapshot:
    def test_snapshot_copies_every_target(self, snapshot_env):
        snap = refresh.create_snapshot(snapshot_env["targets"])

        assert (snap / "docs.lance" / "part.bin").read_text() == "docs-v1"
        assert (snap / "tools.lance" / "part.bin").read_text() == "tools-v1"
        assert (snap / "specs.sqlite").read_text() == "specs-v1"
        assert (snap / "generated_manifests" / "central.json").read_text() == "central-v1"
        assert (snap / "SOURCE-MANIFEST.json").read_text() == "source-v1"
        assert json.loads((snap / "_absent.json").read_text()) == ["INDEX-MANIFEST.json"]

    def test_restore_puts_every_target_back(self, snapshot_env):
        snap = refresh.create_snapshot(snapshot_env["targets"])

        # Simulate a half-finished refresh mutating everything.
        (snapshot_env["docs"] / "part.bin").write_text("docs-v2", encoding="utf-8")
        (snapshot_env["tools"] / "part.bin").write_text("tools-v2", encoding="utf-8")
        snapshot_env["specs"].write_text("specs-v2", encoding="utf-8")
        (snapshot_env["generated"] / "central.json").write_text("central-v2", encoding="utf-8")
        snapshot_env["source_manifest"].write_text("source-v2", encoding="utf-8")
        snapshot_env["index_manifest"].write_text("index-created", encoding="utf-8")

        restored = refresh.restore_snapshot(snap, snapshot_env["targets"])

        assert (snapshot_env["docs"] / "part.bin").read_text() == "docs-v1"
        assert (snapshot_env["tools"] / "part.bin").read_text() == "tools-v1"
        assert snapshot_env["specs"].read_text() == "specs-v1"
        assert (snapshot_env["generated"] / "central.json").read_text() == "central-v1"
        assert snapshot_env["source_manifest"].read_text() == "source-v1"
        # An artifact the failed run created is removed, not left behind.
        assert not snapshot_env["index_manifest"].exists()
        assert "INDEX-MANIFEST.json" in restored

    def test_discard_removes_the_snapshot_directory(self, snapshot_env):
        snap = refresh.create_snapshot(snapshot_env["targets"])
        refresh.discard_snapshot(snap)

        assert not snap.exists()


class TestRunPlan:
    def _plan(self, manifest):
        return refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)

    def test_successful_run_executes_every_step_and_keeps_output(self, manifest, snapshot_env):
        executed = []
        outcome = refresh.run_plan(
            self._plan(manifest),
            runner=lambda step: executed.append(step.name) or 0,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: pytest.fail("must not restore on success"),
        )

        assert outcome["result"] == "success"
        assert len(executed) == len(self._plan(manifest)["steps"])

    def test_failing_step_restores_everything_and_stops(self, manifest, snapshot_env):
        executed = []

        def runner(step):
            executed.append(step.name)
            return 1 if step.kind == refresh.STEP_INDEX_REBUILD else 0

        restored = []
        outcome = refresh.run_plan(
            self._plan(manifest),
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: restored.append(snap) or ["docs.lance"],
        )

        assert outcome["result"] == "step_failed_restored"
        assert outcome["failed_kind"] == refresh.STEP_INDEX_REBUILD
        assert restored, "a failing step must restore the snapshot"
        assert executed[-1] == outcome["failed_step"]

    def test_failing_eval_gate_is_reported_as_a_gate_failure(self, manifest, snapshot_env):
        def runner(step):
            return 1 if step.gate else 0

        outcome = refresh.run_plan(
            self._plan(manifest),
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert outcome["result"] == "gate_failed_restored"

    def test_real_restore_runs_when_a_step_fails(self, manifest, snapshot_env):
        def runner(step):
            snapshot_env["specs"].write_text("specs-corrupt", encoding="utf-8")
            return 1

        refresh.run_plan(
            self._plan(manifest),
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert snapshot_env["specs"].read_text() == "specs-v1"

    def test_exception_mid_run_still_restores(self, manifest, snapshot_env):
        restored = []

        def runner(step):
            raise RuntimeError("scraper crashed")

        with pytest.raises(RuntimeError):
            refresh.run_plan(
                self._plan(manifest),
                runner=runner,
                snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
                restore=lambda snap: restored.append(snap) or [],
            )

        assert restored

    def test_empty_plan_does_not_snapshot(self, manifest):
        outcome = refresh.run_plan(
            refresh.build_plan([], manifest=manifest, refresh_sources=True),
            runner=lambda step: pytest.fail("nothing to run"),
            snapshot_factory=lambda: pytest.fail("must not snapshot for an empty plan"),
        )

        assert outcome["result"] == "no_steps"


# ---------------------------------------------------------------------------
# run_check(): actionable classes are planned from, incomplete ones fail closed
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _check_payload(changed=("vsg_docs",), *, incomplete=False, extra=None):
    payload = {
        "sources": [
            {
                "source": "vsg_docs",
                "resolvable": True,
                "reason": None,
                "result_class": "content_drift",
                "checked": 3,
                "new": 0,
                "changed": 1,
                "unchanged": 2,
                "blocked": 0,
                "errors": 0,
            }
        ],
        "changed_sources": list(changed),
        "drift_report": {"check_incomplete": incomplete, "content_drift_detected": True},
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def _patch_subprocess(monkeypatch, completed):
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return completed

    monkeypatch.setattr(refresh.subprocess, "run", _run)
    return captured


class TestRunCheckClassification:
    def test_invokes_the_check_with_classified_exit_codes(self, monkeypatch):
        captured = _patch_subprocess(monkeypatch, _Completed(0, _check_payload(changed=())))

        refresh.run_check(None, offline=True)

        assert "--exit-code-mode" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--exit-code-mode") + 1] == "classified"
        assert "--offline" in captured["cmd"]

    @pytest.mark.parametrize(
        "exit_code",
        [
            taxonomy.EXIT_OK,
            taxonomy.EXIT_CONTENT_DRIFT,
            taxonomy.EXIT_SOURCE_SET_CHANGED,
            taxonomy.EXIT_POINTER_CHANGE,
            taxonomy.EXIT_STALE_PIN,
        ],
    )
    def test_actionable_classes_are_parsed_into_a_plan_not_raised(self, monkeypatch, exit_code):
        """Content/source-set/pointer/stale-pin findings are exactly what a
        refresh plan is for -- they must never blow up before planning."""
        _patch_subprocess(monkeypatch, _Completed(exit_code, _check_payload()))

        report = refresh.run_check(None, offline=False)

        assert report["changed_sources"] == ["vsg_docs"]
        plan = refresh.build_plan(report["changed_sources"], refresh_sources=True)
        assert plan["steps"]

    @pytest.mark.parametrize(
        "exit_code",
        [taxonomy.EXIT_UNAVAILABLE, taxonomy.EXIT_PARSER_ERROR, taxonomy.EXIT_USAGE],
    )
    def test_incomplete_or_misused_check_fails_closed(self, monkeypatch, exit_code):
        _patch_subprocess(monkeypatch, _Completed(exit_code, _check_payload(), "boom"))

        with pytest.raises(refresh.CheckInvocationError) as excinfo:
            refresh.run_check(None, offline=False)

        assert excinfo.value.exit_code == exit_code

    def test_unrecognized_exit_code_fails_closed(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(42, _check_payload()))

        with pytest.raises(refresh.CheckInvocationError):
            refresh.run_check(None, offline=False)

    def test_malformed_json_fails_closed(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(0, "{not json"))

        with pytest.raises(refresh.CheckInvocationError, match="malformed JSON"):
            refresh.run_check(None, offline=False)

    def test_empty_stdout_fails_closed(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(0, ""))

        with pytest.raises(refresh.CheckInvocationError):
            refresh.run_check(None, offline=False)

    def test_json_missing_required_keys_fails_closed(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(0, json.dumps({"sources": []})))

        with pytest.raises(refresh.CheckInvocationError, match="missing required key"):
            refresh.run_check(None, offline=False)

    def test_non_object_json_fails_closed(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(0, json.dumps([1, 2, 3])))

        with pytest.raises(refresh.CheckInvocationError, match="not an object"):
            refresh.run_check(None, offline=False)

    def test_mixed_report_with_drift_and_incomplete_fails_closed(self, monkeypatch):
        """A run that found real drift *and* could not reach some sources is
        partial: planning from it would re-scrape off incomplete information."""
        _patch_subprocess(
            monkeypatch,
            _Completed(taxonomy.EXIT_CONTENT_DRIFT, _check_payload(incomplete=True)),
        )

        with pytest.raises(refresh.CheckInvocationError, match="check_incomplete"):
            refresh.run_check(None, offline=False)

        assert refresh.run_check(
            None, offline=False, allow_incomplete=True
        )["changed_sources"] == ["vsg_docs"]

    def test_allow_incomplete_downgrades_unavailable_but_not_bad_json(self, monkeypatch):
        _patch_subprocess(
            monkeypatch, _Completed(taxonomy.EXIT_UNAVAILABLE, _check_payload())
        )
        assert refresh.run_check(None, offline=False, allow_incomplete=True)["changed_sources"]

        _patch_subprocess(monkeypatch, _Completed(taxonomy.EXIT_UNAVAILABLE, "{nope"))
        with pytest.raises(refresh.CheckInvocationError):
            refresh.run_check(None, offline=False, allow_incomplete=True)

    def test_usage_error_is_never_downgraded(self, monkeypatch):
        _patch_subprocess(monkeypatch, _Completed(taxonomy.EXIT_USAGE, _check_payload()))

        with pytest.raises(refresh.CheckInvocationError):
            refresh.run_check(None, offline=False, allow_incomplete=True)

    def test_main_propagates_the_fail_closed_exit_code(self, monkeypatch, capsys):
        def _boom(*args, **kwargs):
            raise refresh.CheckInvocationError("nope", exit_code=taxonomy.EXIT_UNAVAILABLE)

        monkeypatch.setattr(refresh, "run_check", _boom)
        monkeypatch.setattr(refresh.sys, "argv", ["prog"])

        exit_code = refresh.main()

        assert exit_code == taxonomy.EXIT_UNAVAILABLE
        assert "failed closed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Post-refresh chain is derived after ALL mutating steps are known
# ---------------------------------------------------------------------------


class TestPostRefreshDerivation:
    def test_scraperless_security_source_still_gets_the_full_downstream_chain(self, manifest):
        """security_advisories has no scraper but triggers the security/
        lifecycle scrape, which mutates the corpus -- the rebuild/gate chain
        must follow it."""
        plan = refresh.build_plan(
            ["security_advisories"], manifest=manifest, refresh_sources=True
        )
        kinds = [step["kind"] for step in plan["steps"]]

        assert kinds[0] == refresh.STEP_SECURITY_LIFECYCLE
        assert refresh.STEP_GENERATED_TOOL in kinds
        assert refresh.STEP_INDEX_REBUILD in kinds
        assert refresh.STEP_TOOL_INDEX_REBUILD in kinds
        assert refresh.STEP_LOCAL_MANIFESTS in kinds
        assert kinds[-1] == refresh.STEP_EVAL_GATE

    def test_scraperless_product_spec_source_also_gets_the_chain(self, manifest):
        plan = refresh.build_plan(["product_specs"], manifest=manifest, refresh_sources=True)
        kinds = [step["kind"] for step in plan["steps"]]

        assert refresh.STEP_PRODUCT_SPEC in kinds
        assert kinds[-1] == refresh.STEP_EVAL_GATE

    def test_no_chain_when_the_triggering_step_is_unrunnable(self, manifest):
        """Without --refresh-sources the security/lifecycle scrape cannot run,
        so nothing mutates and no rebuild/gate should be planned."""
        plan = refresh.build_plan(
            ["security_advisories"], manifest=manifest, refresh_sources=False
        )

        assert plan["steps"] == []
        assert any("--refresh-sources" in item["reason"] for item in plan["unrunnable"])

    def test_no_chain_for_a_source_with_neither_scraper_nor_structured_step(self, manifest):
        plan = refresh.build_plan(["feature_navigator"], manifest=manifest, refresh_sources=True)

        assert plan["steps"] == []


# ---------------------------------------------------------------------------
# Declared step environment
# ---------------------------------------------------------------------------


class TestStepEnvironment:
    def test_tool_index_rebuild_declares_the_strict_catalog_env(self, manifest):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)
        step = next(
            s for s in plan["steps"] if s["kind"] == refresh.STEP_TOOL_INDEX_REBUILD
        )

        assert step["env"] == refresh.STRICT_CATALOG_ENV

    def test_declared_env_survives_plan_serialization(self, manifest):
        plan = json.loads(
            json.dumps(refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True))
        )
        step = next(
            s for s in refresh.plan_steps(plan) if s.kind == refresh.STEP_TOOL_INDEX_REBUILD
        )

        assert all(
            step.env[name] == "1"
            for name in project_facts.GENERATED_TOOL_ENV
        )

    def test_environ_merges_declared_env_over_the_ambient_one(self):
        step = refresh.Step(
            kind=refresh.STEP_TOOL_INDEX_REBUILD,
            name="x",
            command=["scripts/ingest_tools.py"],
            trigger="post_refresh",
            env={"HPE_MCP_PRODUCT_ACCESS": "read-write"},
        )

        merged = step.environ({"PATH": "/bin", "HPE_MCP_PRODUCT_ACCESS": "read-only"})

        assert merged["PATH"] == "/bin"
        assert merged["HPE_MCP_PRODUCT_ACCESS"] == "read-write"

    def test_steps_without_declared_env_inherit_the_ambient_one(self):
        step = refresh.Step(
            kind=refresh.STEP_INDEX_REBUILD,
            name="x",
            command=["ingestion/ingest_docs.py"],
            trigger="post_refresh",
        )

        assert step.environ({"PATH": "/bin"}) == {"PATH": "/bin"}

    def test_default_runner_passes_the_declared_env_to_the_subprocess(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")

        class _Result:
            returncode = 0

        def _run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _Result()

        monkeypatch.setattr(refresh.subprocess, "run", _run)
        step = refresh.Step(
            kind=refresh.STEP_TOOL_INDEX_REBUILD,
            name="x",
            command=["scripts/ingest_tools.py", "--products", "all"],
            trigger="post_refresh",
            env=refresh.STRICT_CATALOG_ENV,
        )

        assert refresh.default_runner(step) == 0
        assert captured["env"]["HPE_MCP_ACCESS_PROFILE"] == "full-read-write"
        assert captured["env"]["HPE_MCP_CENTRAL_WRITES"] == "1"
        assert captured["env"]["HPE_MCP_PRODUCT_ACCESS"] == "read-write"
        assert all(
            captured["env"][name] == "1"
            for name in project_facts.GENERATED_TOOL_ENV
        )
        assert "PATH" in captured["env"]


# ---------------------------------------------------------------------------
# extra_scripts phase ordering
# ---------------------------------------------------------------------------


class TestExtraScriptPhases:
    def _phased_manifest(self):
        return [
            {
                "source": "vsg_docs",
                "output_dir": "ingestion/sources/vsg_docs",
                "scraper": "ingestion/scrape_vsg.py",
                "extra_scripts": [
                    "ingestion/discover_vsg_urls.py",
                    "ingestion/scrape_cnac_spec.py",
                ],
                "extra_script_phases": {
                    "ingestion/discover_vsg_urls.py": "pre",
                    "ingestion/scrape_cnac_spec.py": "post",
                },
            }
        ]

    def test_discovery_runs_before_the_scraper_and_extractors_after(self):
        plan = refresh.build_plan(
            ["vsg_docs"], manifest=self._phased_manifest(), refresh_sources=True
        )
        names = [
            step["name"] for step in plan["steps"] if step["kind"] in
            (refresh.STEP_SCRAPER, refresh.STEP_EXTRA_SCRIPT)
        ]

        assert names == [
            "vsg_docs:discover_vsg_urls",
            "vsg_docs:scraper",
            "vsg_docs:scrape_cnac_spec",
        ]

    def test_phase_is_recorded_on_the_step(self):
        plan = refresh.build_plan(
            ["vsg_docs"], manifest=self._phased_manifest(), refresh_sources=True
        )
        by_name = {step["name"]: step for step in plan["steps"]}

        assert by_name["vsg_docs:discover_vsg_urls"]["phase"] == refresh.PHASE_PRE
        assert by_name["vsg_docs:scrape_cnac_spec"]["phase"] == refresh.PHASE_POST

    def test_unlisted_extras_default_to_post(self):
        manifest = [
            {
                "source": "developer_docs",
                "output_dir": "ingestion/sources/developer_docs",
                "scraper": "ingestion/scrape.py",
                "extra_scripts": ["ingestion/scrape_cnac_spec.py"],
            }
        ]
        plan = refresh.build_plan(["developer_docs"], manifest=manifest, refresh_sources=True)
        names = [
            step["name"] for step in plan["steps"] if step["kind"] in
            (refresh.STEP_SCRAPER, refresh.STEP_EXTRA_SCRIPT)
        ]

        assert names == ["developer_docs:scraper", "developer_docs:scrape_cnac_spec"]

    def test_declared_order_within_a_phase_is_preserved(self):
        manifest = [
            {
                "source": "openapi_specs",
                "output_dir": "ingestion/sources/openapi_specs",
                "scraper": "ingestion/scrape_openapi.py",
                "extra_scripts": [
                    "ingestion/scrape_cnac_spec.py",
                    "ingestion/fetch_mist_openapi.py",
                    "ingestion/fetch_manifest_specs.py",
                ],
                "extra_script_phases": {
                    "ingestion/scrape_cnac_spec.py": "post",
                    "ingestion/fetch_mist_openapi.py": "post",
                    "ingestion/fetch_manifest_specs.py": "post",
                },
            }
        ]
        plan = refresh.build_plan(["openapi_specs"], manifest=manifest, refresh_sources=True)
        extras = [
            step["command"][0]
            for step in plan["steps"]
            if step["kind"] == refresh.STEP_EXTRA_SCRIPT
        ]

        assert extras == [
            "ingestion/scrape_cnac_spec.py",
            "ingestion/fetch_mist_openapi.py",
            "ingestion/fetch_manifest_specs.py",
        ]

    def test_pre_step_runs_even_when_the_source_has_no_scraper(self):
        manifest = [
            {
                "source": "aos_techdocs",
                "output_dir": "ingestion/sources/aos_techdocs",
                "scraper": None,
                "extra_scripts": ["ingestion/discover_aos_urls.py"],
                "extra_script_phases": {"ingestion/discover_aos_urls.py": "pre"},
            }
        ]
        plan = refresh.build_plan(["aos_techdocs"], manifest=manifest, refresh_sources=True)

        assert plan["steps"][0]["name"] == "aos_techdocs:discover_aos_urls"
        assert any("no scraper" in item["reason"] for item in plan["unrunnable"])

    def test_committed_manifest_orders_every_discovery_script_first(self):
        entries = refresh.load_manifest()
        discovery_sources = [
            entry["source"]
            for entry in entries
            if any(
                Path(extra).name.startswith("discover_")
                for extra in entry.get("extra_scripts") or []
            )
        ]
        assert discovery_sources, "expected the committed manifest to declare discovery scripts"

        plan = refresh.build_plan(discovery_sources, refresh_sources=True)
        for source in discovery_sources:
            names = [
                step["name"]
                for step in plan["steps"]
                if step["trigger"] == f"source:{source}"
            ]
            discovery = [n for n in names if ":discover_" in n]
            assert discovery, f"{source} lost its discovery step"
            if f"{source}:scraper" in names:
                assert names.index(discovery[0]) < names.index(f"{source}:scraper")


# ---------------------------------------------------------------------------
# Rollback across the new paths
# ---------------------------------------------------------------------------


class TestRollbackOnNewPaths:
    def test_failing_pre_discovery_step_restores_and_stops(self, snapshot_env):
        manifest = [
            {
                "source": "vsg_docs",
                "output_dir": "ingestion/sources/vsg_docs",
                "scraper": "ingestion/scrape_vsg.py",
                "extra_scripts": ["ingestion/discover_vsg_urls.py"],
                "extra_script_phases": {"ingestion/discover_vsg_urls.py": "pre"},
            }
        ]
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)
        executed = []

        def runner(step):
            executed.append(step.name)
            (snapshot_env["specs"]).write_text("specs-corrupt", encoding="utf-8")
            return 1 if step.phase == refresh.PHASE_PRE else 0

        outcome = refresh.run_plan(
            plan,
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert outcome["failed_step"] == "vsg_docs:discover_vsg_urls"
        assert executed == ["vsg_docs:discover_vsg_urls"]
        assert snapshot_env["specs"].read_text() == "specs-v1"

    def test_failing_tool_index_rebuild_restores_tools_lance(self, manifest, snapshot_env):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)

        def runner(step):
            if step.kind == refresh.STEP_TOOL_INDEX_REBUILD:
                (snapshot_env["tools"] / "part.bin").write_text("tools-v2", encoding="utf-8")
                return 1
            return 0

        outcome = refresh.run_plan(
            plan,
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert outcome["failed_kind"] == refresh.STEP_TOOL_INDEX_REBUILD
        assert (snapshot_env["tools"] / "part.bin").read_text() == "tools-v1"

    def test_failing_security_lifecycle_step_restores_everything(self, manifest, snapshot_env):
        plan = refresh.build_plan(
            ["security_advisories"], manifest=manifest, refresh_sources=True
        )

        def runner(step):
            (snapshot_env["generated"] / "central.json").write_text("v2", encoding="utf-8")
            return 1 if step.kind == refresh.STEP_SECURITY_LIFECYCLE else 0

        outcome = refresh.run_plan(
            plan,
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert outcome["result"] == "step_failed_restored"
        assert (snapshot_env["generated"] / "central.json").read_text() == "central-v1"

    def test_failing_local_manifest_step_restores_both_manifests(self, manifest, snapshot_env):
        plan = refresh.build_plan(["vsg_docs"], manifest=manifest, refresh_sources=True)

        def runner(step):
            if step.kind == refresh.STEP_LOCAL_MANIFESTS:
                snapshot_env["source_manifest"].write_text("source-v2", encoding="utf-8")
                snapshot_env["index_manifest"].write_text("index-v2", encoding="utf-8")
                return 1
            return 0

        refresh.run_plan(
            plan,
            runner=runner,
            snapshot_factory=lambda: refresh.create_snapshot(snapshot_env["targets"]),
            restore=lambda snap: refresh.restore_snapshot(snap, snapshot_env["targets"]),
        )

        assert snapshot_env["source_manifest"].read_text() == "source-v1"
        assert not snapshot_env["index_manifest"].exists()
