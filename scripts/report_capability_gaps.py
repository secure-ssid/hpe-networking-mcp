#!/usr/bin/env python3
"""Generate or drift-check the reproducible capability gap matrix."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpe_networking_mcp.pipeline.clients.capability_coverage import PROTOCOL_ONLY
from hpe_networking_mcp.pipeline.project_facts import NON_API_LOCAL_TOOLS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "openapi_gen" / "manifests"
BENCHMARK_PATH = ROOT / "docs" / "capability-benchmark-snapshot.json"
REPORT_PATH = ROOT / "docs" / "capability-gap-matrix.md"
CAPABILITIES = ("read", "diagnostic", "write", "destructive")


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    modules: tuple[str, ...]
    source_type: str
    authority: str
    auth: str


PLATFORMS = (
    Platform(
        "central",
        "Aruba Central",
        ("config.py", "monitoring.py", "nac.py", "ops.py"),
        "official OpenAPI + curated",
        "Official HPE APIs",
        "OAuth token manager ready",
    ),
    Platform(
        "glp",
        "GreenLake Platform",
        ("glp.py",),
        "official OpenAPI + curated",
        "Official HPE APIs",
        "Workspace OAuth ready",
    ),
    Platform(
        "rag",
        "RAG / API lookup",
        ("rag.py",),
        "committed application code",
        "Local indexes over cited sources",
        "No platform auth",
    ),
    Platform(
        "clearpass",
        "ClearPass",
        ("clearpass.py",),
        "official OpenAPI + curated",
        "Official HPE API",
        "API credential ready",
    ),
    Platform(
        "mist",
        "Juniper Mist",
        ("mist.py",),
        "official OpenAPI + curated",
        "Official Juniper OpenAPI",
        "REST token/session ready",
    ),
    Platform(
        "apstra",
        "Juniper Apstra",
        ("apstra.py",),
        "official SDK-derived + curated",
        "Official Juniper SDK",
        "Login/token session ready",
    ),
    Platform(
        "aos8",
        "ArubaOS 8",
        ("aos8.py",),
        "official OpenAPI + curated",
        "Official HPE API",
        "UIDARUBA/X-CSRF session ready",
    ),
    Platform(
        "edgeconnect",
        "EdgeConnect",
        ("edgeconnect.py",),
        "instance artifact + curated",
        "Target Orchestrator Swagger",
        "Token/session and doctor ready",
    ),
    Platform(
        "uxi",
        "HPE Aruba UXI",
        ("uxi.py",),
        "official OpenAPI + curated",
        "Official HPE API",
        "OAuth client credentials ready",
    ),
    Platform(
        "axis",
        "Axis Atmos Cloud",
        ("axis.py",),
        "reviewed benchmark-derived registry",
        "Benchmark only; verify with Axis",
        "Static bearer token ready",
    ),
)

GAPS = (
    (
        1,
        "ArubaOS 8",
        "Broader verified migration mappings and live evaluation",
        "Official AOS8 and Central APIs",
        "Source and target auth clients are ready; six resumable migration tools ship",
        "Very high",
        "Preview/create/apply/get/list/verify migration-run tools now execute "
        "guarded New Central and Classic writes with dependency-aware resume, "
        "conflict policies, and bounded verification. The verified target "
        "mapping subset (VLANs, allow-all roles, RADIUS, simple AAA, open "
        "bridged/tunneled WLANs on New Central; open bridged WLAN on Classic) "
        "still needs broadening against live estates, plus AAA/auth-profile, "
        "server-group, and policy-rule target mappings beyond the verified "
        "subset, and end-to-end evaluation against real source exports.",
        "`docs/aos8-migration-contract-matrix.md`, "
        "`src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py`, "
        "`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py`, "
        "`src/hpe_networking_mcp/mcp_servers/aos8.py`, "
        "`docs/product-workflows.md`",
    ),
    (
        2,
        "EdgeConnect",
        "Real current 9.3+ Swagger acquisition and validation",
        "Target Orchestrator's instance Swagger",
        "Token/session, fail-closed compatibility doctor, and `--generate` workflow ready",
        "High",
        "The compatibility/report/generation workflow correctly fails closed and "
        "requires an explicit `--generate` with a provenance digest pin, but a "
        "real current 9.3+ target Swagger has not yet been obtained. Acquire one "
        "from a live Orchestrator instance and run it through the compatibility "
        "doctor to validate and remap production coverage before treating the "
        "generated wrappers as production-ready against that release.",
        "`scripts/generate_edgeconnect_tools.py`, "
        "`src/hpe_networking_mcp/mcp_servers/openapi_gen/compatibility.py`, "
        "`docs/optional-products.md`",
    ),
    (
        3,
        "Axis Atmos Cloud",
        "Official or target-instance verification of the generator inputs",
        "Axis documentation/API behavior required",
        "Static bearer token ready; deterministic SHA-pinned 25-operation generator ships",
        "Medium",
        "`scripts/generate_axis_manifest.py` now builds the manifest deterministically "
        "from digest-pinned local sources, with explicit-fetch and offline-check "
        "modes, so the generator itself is reproducible. The underlying 25 "
        "operations are still a reviewed benchmark-derived registry, not an "
        "official Axis specification or a target-verified capture. Confirm "
        "against Axis-published documentation or a live Axis Atmos instance "
        "when access is available.",
        "`scripts/generate_axis_manifest.py`, "
        "`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/axis.json`, "
        "`src/hpe_networking_mcp/mcp_servers/axis.py`",
    ),
    (
        4,
        "Cross-platform",
        "Broader live workflow evaluation",
        "Official per-platform APIs and target instances",
        "Guarded writes, dry-run confirmation, and gate metadata ready across platforms",
        "Medium",
        "The router's discovery contract, guarded-write gate, and per-platform "
        "generated/curated coverage are in place and unit-tested, but most "
        "workflows are validated against fixtures and manifests rather than "
        "sustained live estates. Expand live, cross-platform evaluation "
        "(Central, GLP, AOS8 migration, Mist diagnostics, EdgeConnect, Axis) as "
        "lab/production access becomes available, and feed findings back into "
        "the verified-mapping and gap lists.",
        "`src/hpe_networking_mcp/mcp_servers/tool_router.py`, "
        "`src/hpe_networking_mcp/mcp_servers/shared.py`, "
        "`docs/product-workflows.md`",
    ),
)


def _annotation_name(decorator: ast.Call) -> str:
    for keyword in decorator.keywords:
        if keyword.arg == "annotations" and isinstance(keyword.value, ast.Name):
            return keyword.value.id.lower()
    return "unclassified"


def curated_capabilities(module_names: tuple[str, ...]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for module_name in module_names:
        path = ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / module_name
        server_name = "glp-core" if path.stem == "glp" else f"{path.stem}-core"
        excluded_tools = NON_API_LOCAL_TOOLS.get(f"{server_name}", frozenset())
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in excluded_tools:
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "mcp"
                ):
                    continue
                annotation = _annotation_name(decorator)
                capability = {
                    "read_only": "read",
                    "read_only_local": "read",
                    "diagnostic": "diagnostic",
                    "write": "write",
                    "idempotent_write": "write",
                    "destructive": "destructive",
                }.get(annotation)
                if capability is None:
                    raise ValueError(
                        f"{path.relative_to(ROOT)}: unsupported tool annotation {annotation}"
                    )
                counts[capability] += 1
    return counts


def _excluded_reason(platform: str, operation: dict[str, Any]) -> str | None:
    path = operation.get("path", "")
    if platform == "glp" and path.startswith(
        ("/devices/v1beta1/", "/subscriptions/v1alpha1/", "/subscriptions/v1beta1/")
    ):
        return "sunset GLP device/subscription version"
    if platform == "clearpass" and path == "/oauth":
        return "credential-returning OAuth endpoint"
    if platform == "apstra" and "Auth" in (operation.get("tags") or []):
        return "login handled by the internal session client"
    return None


def generated_summary(platform: str) -> tuple[int, Counter[str], Counter[str]]:
    if platform == "rag":
        return 0, Counter(), Counter()
    manifest = json.loads((MANIFEST_DIR / f"{platform}.json").read_text())
    operations = manifest["operations"]
    active: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    for operation in operations:
        reason = _excluded_reason(platform, operation)
        if reason is None:
            active[operation["capability"]] += 1
        else:
            excluded[reason] += 1
    return len(operations), active, excluded


def collect_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        generated, generated_caps, excluded = generated_summary(platform.key)
        curated_caps = curated_capabilities(platform.modules)
        capabilities = generated_caps + curated_caps
        rows.append(
            {
                "platform": platform,
                "generated": generated,
                "curated": sum(curated_caps.values()),
                "registered_generated": sum(generated_caps.values()),
                "registered": sum(capabilities.values()),
                "capabilities": capabilities,
                "excluded": excluded,
            }
        )
    return rows


def _fmt(value: int) -> str:
    return f"{value:,}"


def _benchmark() -> dict[str, Any]:
    benchmark = json.loads(BENCHMARK_PATH.read_text())
    platform_total = sum(benchmark["executable_backend_tools_by_platform"].values())
    if platform_total != benchmark["counts"]["executable_backend_tools"]:
        raise ValueError("benchmark per-platform executable counts do not match total")
    if (
        benchmark["indexed_endpoint_reproduction"]["endpoints"]
        != benchmark["counts"]["indexed_endpoints"]
    ):
        raise ValueError("benchmark indexed endpoint counts do not match")
    dynamic = benchmark["dynamic_mode_breakdown"]
    dynamic_total = (
        dynamic["platform_router_tools"]
        + dynamic["direct_cross_platform_tools"]
        + dynamic["skills_tools"]
    )
    if dynamic_total != benchmark["counts"]["dynamic_mode_surface"]:
        raise ValueError("benchmark dynamic-mode breakdown does not match total")
    if dynamic["platform_router_tools"] != (
        dynamic["platform_count"] * dynamic["router_tools_per_platform"]
    ):
        raise ValueError("benchmark platform router breakdown does not match")
    return benchmark


def render_report() -> str:
    rows = collect_rows()
    benchmark = _benchmark()
    generated_total = sum(row["generated"] for row in rows)
    registered_generated_total = sum(row["registered_generated"] for row in rows)
    curated_total = sum(row["curated"] for row in rows)
    registered_total = sum(row["registered"] for row in rows)
    caps_total = sum((row["capabilities"] for row in rows), Counter())

    lines = [
        "# Capability gap matrix",
        "",
        "<!-- Generated by scripts/report_capability_gaps.py; do not edit by hand. -->",
        "",
        "This report compares callable MCP capabilities on an apples-to-apples basis. "
        "**Executable tools**, generated/spec operations, indexed documentation "
        "endpoints, and client-visible router tools are different units and must not "
        "be added together or treated as equivalent coverage.",
        "",
        "Official vendor APIs, SDKs, and target-instance specifications are the "
        "authority for behavior. The pinned `nowireless4u/hpe-networking-mcp` data is "
        "a useful MIT-licensed benchmark, not an API authority.",
        "",
        "`scripts/check_nowireless_source_drift.py` checks whether the GLP vendored-spec, "
        "Axis platform-source, and capability-benchmark path pins referenced here have "
        "advanced past their reviewed commit; drift means review the changed path(s) "
        "upstream and regenerate with `scripts/generate_glp_tools.py`, "
        "`scripts/generate_axis_manifest.py`, or this script (updating "
        "`docs/capability-benchmark-snapshot.json`) before advancing the pin.",
        "",
        "## hpe-networking-mcp executable catalog",
        "",
        "Counts below describe the full read-write registration of the *platform API* "
        "backends only -- the nine vendor surfaces plus RAG. The two credential-free "
        "local backends (`design-core`, `interop-core`) make no vendor API call and are "
        "excluded here, so this page's total is the platform API backend total, not the "
        "complete registered backend total; see "
        "[`docs/tool-catalog.md`](tool-catalog.md) for both. Optional "
        "products remain opt-in, and optional write tools are hidden in read-only mode.",
        "",
        "| Platform | Manifest operations | Active generated tools | Curated tools | "
        "Executable total | Read | Diagnostic | Write | Destructive | Source / provenance |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        platform = row["platform"]
        caps = row["capabilities"]
        lines.append(
            f"| {platform.label} | {_fmt(row['generated'])} | "
            f"{_fmt(row['registered_generated'])} | {_fmt(row['curated'])} | "
            f"**{_fmt(row['registered'])}** | {_fmt(caps['read'])} | "
            f"{_fmt(caps['diagnostic'])} | {_fmt(caps['write'])} | "
            f"{_fmt(caps['destructive'])} | {platform.source_type}; "
            f"{platform.authority}; {platform.auth} |"
        )
    lines.extend(
        [
            f"| **Total** | **{_fmt(generated_total)}** | "
            f"**{_fmt(registered_generated_total)}** | **{_fmt(curated_total)}** | "
            f"**{_fmt(registered_total)}** | **{_fmt(caps_total['read'])}** | "
            f"**{_fmt(caps_total['diagnostic'])}** | **{_fmt(caps_total['write'])}** | "
            f"**{_fmt(caps_total['destructive'])}** | |",
            "",
            f"The {_fmt(generated_total)} manifest records are provenance-bearing generated "
            f"operations. Only {_fmt(registered_generated_total)} register as executable "
            f"generated tools because {_fmt(generated_total - registered_generated_total)} "
            f"are intentionally excluded. Adding {_fmt(curated_total)} curated tools yields "
            f"{_fmt(registered_total)} executable platform API backend tools. The three "
            "minimal-router "
            "tools are a separate client-visible dispatch surface, not three additional "
            "backend capabilities.",
            "",
            "## Protocol-only capabilities",
            "",
            "These capabilities are documented by RAG but are not REST/OpenAPI "
            "operations. They are tracked separately from manifest totals and map "
            "to curated transport/workflow tools when one exists.",
            "",
            "| Platform | Family | Protocol | Subscription | Curated tool | "
            "Endpoints | Documentation sources |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for capability in PROTOCOL_ONLY:
        endpoints = "<br>".join(capability.get("endpoints") or ["—"])
        sources = "<br>".join(capability.get("documentation_sources") or ["—"])
        lines.append(
            f"| {capability.get('platform', '—')} | "
            f"{capability.get('family', '—')} | "
            f"{capability.get('protocol', '—')} | "
            f"{capability.get('subscription', '—')} | "
            f"`{capability['curated_tool']}` | {endpoints} | {sources} |"
        )
    lines.extend(
        [
            "",
            "### Intentional exclusions",
            "",
            "| Platform | Count | Reason |",
            "|---|---:|---|",
        ]
    )
    for row in rows:
        for reason, count in sorted(row["excluded"].items()):
            lines.append(f"| {row['platform'].label} | {_fmt(count)} | {reason} |")
    lines.extend(
        [
            "",
            "The exclusions are read directly from the same operation predicates used "
            "by the runtime modules: 14 sunset GLP operations, ClearPass `/oauth`, and "
            "two Apstra login operations whose credentials are injected internally.",
            "",
            "## Pinned benchmark snapshot",
            "",
            f"Snapshot date: **{benchmark['snapshot_date']}**. Repository: "
            f"[`{benchmark['repository']}@{benchmark['commit'][:7]}`]"
            f"({benchmark['commit_url']}).",
            "",
            "| Benchmark measure | Count | Meaning |",
            "|---|---:|---|",
            "| Executable backend tools | "
            f"**{_fmt(benchmark['counts']['executable_backend_tools'])}** "
            "| Registered call targets reachable through its code/dynamic dispatch |",
            f"| Indexed endpoints | **{_fmt(benchmark['counts']['indexed_endpoints'])}** "
            "| OpenAPI documentation rows built by `scripts/build_spec_index.py`; not tools |",
            "| Default code-mode surface | "
            f"{_fmt(benchmark['counts']['default_code_mode_surface'])} "
            "| Top-level execution/discovery tools |",
            f"| Dynamic-mode surface | {_fmt(benchmark['counts']['dynamic_mode_surface'])} "
            "| 27 platform routers + 7 direct cross-platform tools + 2 skills tools |",
            "",
            "### Benchmark executable tools by platform",
            "",
            "| Platform | Executable tools |",
            "|---|---:|",
        ]
    )
    labels = {
        "mist": "Juniper Mist",
        "central": "Aruba Central",
        "greenlake": "GreenLake Platform",
        "clearpass": "ClearPass",
        "apstra": "Juniper Apstra",
        "axis": "Axis Atmos Cloud",
        "aos8": "ArubaOS 8",
        "uxi": "HPE Aruba UXI",
        "edgeconnect": "EdgeConnect",
    }
    for key, count in benchmark["executable_backend_tools_by_platform"].items():
        lines.append(f"| {labels[key]} | {_fmt(count)} |")
    lines.extend(
        [
            f"| **Total** | **{_fmt(benchmark['counts']['executable_backend_tools'])}** |",
            "",
            "The pinned README's early comparison matrix contains a stale GreenLake "
            "cell (`10`). Its platform tree and startup examples state 919, and 919 "
            "is the value that makes the per-platform sum equal the stated 4,109 total. "
            "The snapshot records that reconciliation explicitly.",
            "",
            "The pinned README's **24-tool dynamic-mode claim is also stale and "
            "contradictory**. The pinned implementation registers three router tools "
            "for each of nine platforms (27), seven direct cross-platform tools "
            "(`health`, two site aggregators, and four translation tools), and two "
            "skills tools, for **36 client-visible tools** with all platforms configured.",
            "",
            "The benchmark's 5,960 endpoint count was reproduced from the pinned tree "
            "with its standard-library index builder: 81 specs, 39,576 responses, "
            "17,836 parameters, 13,578 schemas, 63,111 fields, and 6 skipped inputs. "
            "The committed [snapshot data](capability-benchmark-snapshot.json) "
            "contains the exact source URLs, command, and counts.",
            "",
            "### Why the headline totals are non-equivalent",
            "",
            f"- **{_fmt(registered_total)} platform API tools vs. "
            f"{_fmt(benchmark['counts']['executable_backend_tools'])}** compares executable "
            "backend registries, but generation strategy and curated overlap differ; it "
            "does not prove practical superiority.",
            f"- **{_fmt(generated_total)} vs. "
            f"{_fmt(benchmark['counts']['indexed_endpoints'])}** compares hpe-networking-mcp "
            "generated operation records with the benchmark's documentation index. The "
            "latter includes endpoints that may have no callable tool, so this is not a "
            "tool-count comparison.",
            "- Router/code/dynamic surfaces intentionally expose only discovery and dispatch "
            "tools. A smaller top-level surface can still reach a much larger backend.",
            "- Capability quality depends on authentication, bounded responses, safe writes, "
            "async-result handling, and verified workflows—not raw endpoint quantity.",
            "",
            "## Ranked practical gaps",
            "",
            "| Rank | Platform | Capability gap | Authoritative source | Auth readiness | "
            "User value | Recommended scope | Evidence |",
            "|---:|---|---|---|---|---|---|---|",
        ]
    )
    for rank, platform, capability, authority, auth, value, scope, evidence in GAPS:
        lines.append(
            f"| {rank} | {platform} | {capability} | {authority} | {auth} | "
            f"{value} | {scope} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "These are verified implementation gaps, not differences inferred only from "
            "the benchmark's count. The original five 0.3 priorities (AOS8 migration "
            "execution, Mist WebSocket diagnostic collection, EdgeConnect Swagger "
            "import/compatibility, GreenLake typed workflows, and a reproducible Axis "
            "manifest generator) now ship; the gaps above describe what remains after "
            "that work — broader verified migration mappings and live evaluation, a real "
            "current EdgeConnect 9.3+ Swagger, official/target Axis verification, and "
            "broader live cross-platform workflow evaluation.",
            "",
            "## Reproduce and check drift",
            "",
            "No network access is required:",
            "",
            "```bash",
            "python3 scripts/report_capability_gaps.py --write",
            "python3 scripts/report_capability_gaps.py --check",
            "```",
            "",
            "The script parses committed MCPServer tool decorators, committed generated manifests, "
            "runtime-equivalent exclusion predicates, and the pinned benchmark JSON. "
            "`--check` exits non-zero if this file is stale.",
            "",
        ]
    )
    return "\n".join(lines)


def report_matches(rendered: str, existing: str) -> bool:
    return rendered == existing


def check_report(path: Path, rendered: str) -> bool:
    return path.exists() and report_matches(rendered, path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the checked-in report")
    mode.add_argument("--check", action="store_true", help="fail if the report is stale")
    args = parser.parse_args(argv)
    try:
        rendered = render_report()
    except (OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        print(f"Capability report failed: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if not check_report(REPORT_PATH, rendered):
            print(
                f"{REPORT_PATH.relative_to(ROOT)} is stale; "
                "run scripts/report_capability_gaps.py --write",
                file=sys.stderr,
            )
            return 1
        print(f"{REPORT_PATH.relative_to(ROOT)} is current")
        return 0
    if args.write:
        REPORT_PATH.write_text(rendered)
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
