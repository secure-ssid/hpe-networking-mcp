"""Per-backend compatibility/evidence artifacts for the optional product
backends (Apstra, ClearPass, EdgeConnect, Mist, UXI, Axis).

This is the v0.7 ``v07-optional-depth`` counterpart to the Central/GLP/AOS8
workstreams: one bounded, redacted evidence artifact per optional backend,
built through :mod:`hpe_networking_mcp.pipeline.artifact_contracts` (never a hand-rolled JSON
shape) and gated through :mod:`hpe_networking_mcp.pipeline.live_test_config` (never an ad hoc
env var convention).

Two independent kinds of evidence are produced per platform:

1. **Compatibility** (always, offline, no credentials): the committed
   manifest for a platform is compared against the same file's content at
   the repository's last commit (``git show HEAD:<path>``). This answers
   "does the manifest I am about to ship differ from what was last
   released, and is that difference explained?" without requiring network
   access or a live target. A platform with no manifest changes reports a
   trivially compatible, zero-delta result; a platform whose manifest was
   intentionally extended (e.g. Apstra in v0.7) reports the operation
   counts added and the documented reasons for the change.
2. **Live read evidence** (opt-in only): when
   ``live_test_config.live_test_read_enabled(platform)`` is true *and*
   credentials are configured, a single bounded, non-network configuration
   smoke-check is run through that platform's own ``<platform>_status``
   tool (or, for Axis, its ``_axis_config()`` helper -- Axis has no
   non-network status tool) and recorded as a
   :data:`hpe_networking_mcp.pipeline.artifact_contracts.LIVE_LIFECYCLE_EVIDENCE` artifact.
   This step never issues a live HTTP request itself (it only confirms the
   configured/unconfigured state used to gate any *actual* live call a
   human-operated harness -- e.g. ``scripts/evaluate_axis_lab.py`` -- would
   make). This never runs automatically -- the read gate defaults to
   disabled for every platform (see ``src/hpe_networking_mcp/pipeline/live_test_config.py``), and
   this module never enables it itself.

Neither artifact ever contains a raw API response, tenant/workspace
identifier, or credential value -- ``contracts.write_artifact`` redacts
before writing, and this module never passes ``redact=False``.
"""

from __future__ import annotations

import hashlib
import importlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest, manifest_path
from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import live_test_config

# Repo-level (non-package) paths: resolved centrally so the src-layout
# depth cannot drift again -- see hpe_networking_mcp._paths.
REPO_ROOT = repo_root()
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs" / "optional-product-evidence"

# Every optional backend covered by v07-optional-depth. Central/GLP/AOS8 are
# owned by concurrent v0.7 workstreams and are deliberately not included here.
OPTIONAL_PLATFORMS: tuple[str, ...] = (
    "apstra",
    "clearpass",
    "edgeconnect",
    "mist",
    "uxi",
    "axis",
)

# Permanent, source-confirmed coverage omissions that are not (yet, or ever)
# recorded in a platform's own manifest ``provenance`` dict -- e.g. because
# regenerating that platform's manifest/provenance pin requires a live
# network fetch (see ``scripts/build_optional_product_manifests.py``) that
# this offline evidence module never performs. Each entry here was verified
# by inspecting the committed manifest directly (never guessed).
PERMANENT_OMISSIONS: dict[str, tuple[str, ...]] = {
    "uxi": (
        "UXI service tests are read-only in the committed manifest: only "
        "`GET /networking-uxi/v1alpha1/service-tests` is modeled. The public "
        "UXI API has no create/update/delete operation for a service test "
        "definition itself (only its group *assignment* is writable via "
        "`uxi_assign_service_test_to_group`); this is a permanent upstream "
        "API omission, not a missing curated wrapper.",
    ),
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_head_bytes(relative_path: Path) -> bytes | None:
    """Return the committed (``HEAD``) bytes for a repo-relative path, or None."""
    rel = relative_path.resolve().relative_to(REPO_ROOT)
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{rel.as_posix()}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _operation_keys(manifest: dict[str, Any]) -> set[str]:
    return {
        f"{op.get('method', '')} {op.get('path', '')}"
        for op in manifest.get("operations", [])
        if isinstance(op, dict)
    }


def build_compatibility_entry(platform: str) -> contracts.PlatformCompatibilityEntry:
    """Build one platform's compatibility entry against its git HEAD baseline.

    ``compatible`` is always True here (this compares the working tree to
    its own repository history, never an external target), but
    ``operations_added``/``operations_removed`` and ``reasons`` still
    surface exactly what changed so a reviewer never has to guess.
    """
    path = manifest_path(platform)
    current_bytes = path.read_bytes()
    current_manifest = load_manifest(platform)
    baseline_bytes = _git_head_bytes(path)

    reasons: list[str] = []
    if baseline_bytes is None:
        reasons.append(
            f"no committed HEAD revision found for {path.name}; treating the working "
            "tree manifest as the first committed baseline"
        )
        added = removed = changed = 0
        baseline_sha = ""
    else:
        baseline_manifest = contracts_json_or_none(baseline_bytes)
        baseline_keys = _operation_keys(baseline_manifest) if baseline_manifest else set()
        current_keys = _operation_keys(current_manifest)
        added_keys = current_keys - baseline_keys
        removed_keys = baseline_keys - current_keys
        added, removed, changed = len(added_keys), len(removed_keys), 0
        baseline_sha = _sha256_bytes(baseline_bytes)
        if added or removed:
            reasons.append(
                f"{added} operation(s) added and {removed} removed versus the last "
                "committed manifest revision"
            )

    gaps = _coverage_gaps(current_manifest)
    gaps.extend(PERMANENT_OMISSIONS.get(platform, ()))
    reasons.extend(gaps)
    if not reasons:
        reasons.append("manifest is unchanged versus the last committed revision")

    return contracts.PlatformCompatibilityEntry(
        platform=platform,
        compatible=True,
        reasons=reasons[: contracts.MAX_COMPAT_REASONS],
        operations_added=added,
        operations_removed=removed,
        operations_changed=changed,
        source_sha256=_sha256_bytes(current_bytes),
        baseline_sha256=baseline_sha,
    )


def contracts_json_or_none(data: bytes) -> dict[str, Any] | None:
    import json

    try:
        parsed = json.loads(data)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coverage_gaps(manifest: dict[str, Any]) -> list[str]:
    """Surface a manifest's own documented, source-grounded coverage gaps.

    Never invents a gap -- only republishes what the manifest's own
    ``provenance.coverage_gaps``/``provenance.note`` already states (see
    e.g. ``scripts/_apstra_operations.py``'s ``build_apstra_manifest``).
    """
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        return []
    gaps: list[str] = []
    declared = provenance.get("coverage_gaps")
    if isinstance(declared, list):
        gaps.extend(str(item) for item in declared)
    note = provenance.get("note")
    if isinstance(note, str) and "not full" in note.lower():
        gaps.append(note)
    return gaps


def _status_step(platform: str) -> dict[str, Any]:
    """Attempt one bounded, read-only configuration/status smoke-check.

    Every optional backend except Axis exposes a plain, non-network
    ``<platform>_status()`` tool that reports whether the platform is
    configured; Axis has no equivalent curated tool (only generated,
    network-calling ones), so it is checked directly via its own
    ``_axis_config()`` helper instead. Never issues a live HTTP request and
    never raises -- a probe failure is recorded as a step, not an
    exception.
    """
    try:
        module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{platform}")
        if platform == "axis":
            base_url, token = module._axis_config()
            configured = bool(base_url and token)
            return {"name": "axis_config_check", "status": "ok" if configured else "unconfigured"}
        status_fn = getattr(module, f"{platform}_status")
        result = status_fn()
        status = "ok" if isinstance(result, dict) else "error"
        return {"name": f"{platform}_status", "status": status}
    except Exception as exc:  # noqa: BLE001 - a probe failure is evidence, not a crash
        return {"name": f"{platform}_status", "status": "error", "detail": str(exc)[:200]}


def build_live_read_evidence(platform: str) -> contracts.LiveLifecycleEvidence | None:
    """Return bounded read-only evidence for ``platform``, or None if not gated on.

    Returns None (produces no artifact) unless
    ``live_test_config.live_test_read_enabled(platform)`` is True *and*
    credentials are configured -- this function never enables the gate
    itself and never falls back to guessing credential state.
    """
    if not live_test_config.live_test_read_enabled(platform):
        return None
    if not live_test_config.credentials_configured(platform):
        return None
    step = _status_step(platform)
    return contracts.LiveLifecycleEvidence(
        platform=platform,
        mode="read_only",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        steps=(step,),
        summary={"probe": "status_only", "step_count": 1},
    )


def write_backend_evidence(
    platform: str, *, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> list[contracts.ManifestEntry]:
    """Write the one-or-two evidence artifacts for one optional backend."""
    output_dir.mkdir(parents=True, exist_ok=True)
    entries: list[contracts.ManifestEntry] = []

    compatibility = build_compatibility_entry(platform)
    compat_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": [contracts.to_json_dict(compatibility)],
    }
    entries.append(
        contracts.write_artifact(
            output_dir / f"{platform}-compatibility.json",
            contracts.PLATFORM_COMPATIBILITY_RESULT,
            compat_payload,
        )
    )

    live_evidence = build_live_read_evidence(platform)
    if live_evidence is not None:
        entries.append(
            contracts.write_artifact(
                output_dir / f"{platform}-live-evidence.json",
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                contracts.to_json_dict(live_evidence),
            )
        )
    return entries


def write_all_backend_evidence(
    *, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict[str, list[contracts.ManifestEntry]]:
    """Write evidence artifacts for every platform in :data:`OPTIONAL_PLATFORMS`."""
    return {
        platform: write_backend_evidence(platform, output_dir=output_dir)
        for platform in OPTIONAL_PLATFORMS
    }
