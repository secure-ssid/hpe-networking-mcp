"""Shared, versioned artifact contracts for v0.7 workstreams.

Every v0.7 workstream that writes a durable evidence/report/manifest file
(live evaluation harnesses, platform-compatibility checks, migration
reporting, capability benchmarking, source-freshness/drift checks, and
release packaging) should build its payload through this module instead of
hand-rolling another ad hoc JSON shape. That gives every artifact:

- An explicit ``schema_version`` per artifact kind (see
  :data:`SCHEMA_VERSIONS`) so a later revision can detect and reject a
  stale/incompatible file instead of silently misreading it.
- Required-field and type validation that fails loudly
  (:class:`ArtifactValidationError`) instead of writing malformed evidence.
- A bound on every collection that can grow (steps, reasons, per-platform
  entries, manifest entries, ...) so one artifact can never blow up disk or
  downstream context usage.
- Mandatory recursive redaction before writing: secrets are stripped by
  :func:`hpe_networking_mcp.mcp_servers.shared.redact_sensitive` (reused, not reimplemented)
  and tenant/workspace/account/scope identifiers are replaced with a
  deterministic, irreversible ``sha256:<hex12>`` placeholder using the same
  convention already established by
  ``scripts/evaluate_aos8_050_readonly.py``'s ``_sanitize_identifier``.
- Deterministic content hashing and an atomic (temp-file + ``os.replace``)
  writer, mirroring the existing pattern in
  ``src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py``'s ``MigrationRunStore``.

Supported artifact kinds (see :data:`ARTIFACT_KINDS`):

1. ``live_lifecycle_evidence`` -- bounded evidence from a live read-only or
   disposable-write lifecycle probe against a real platform.
2. ``platform_compatibility_result`` -- one or more per-platform
   compatibility verdicts (a "result" is a one-entry matrix; a "matrix" is
   a many-entry one, both use this single bounded shape).
3. ``migration_report_metadata`` -- metadata *about* a migration report
   (counts, formats, hashed device references) -- never the raw per-device
   report rows themselves (those stay in ``src/hpe_networking_mcp/pipeline/reporter.py``'s
   CSV/JSON/HTML outputs).
4. ``capability_snapshot`` -- per-platform read/diagnostic/write/destructive
   tool counts, the reproducible core of
   ``scripts/report_capability_gaps.py``'s matrix.
5. ``source_freshness_result`` -- per-source freshness/drift counts versus
   a minimum, the reproducible core of
   ``scripts/check_security_lifecycle_drift.py``.
6. ``release_artifact_manifest`` -- the manifest of artifact files produced
   for a release: filename, kind, schema version, size, sha256, generation
   timestamp, and redaction status for each entry.
7. ``router_dependency_plan`` -- a bounded, deterministic, read-only
   dependency/order plan across catalog-resolved MCP router tools (never an
   executed workflow).
8. ``router_reconciliation_plan`` -- a bounded, read-only, plan-only
   recurring reconciliation schedule specification for enabled router
   backends (never a live OS/GitHub schedule and never a write).
9. ``validation_matrix_result`` -- a bounded, per-category snapshot of the
   ``v07-live-artifacts`` credential-gated validation matrix (one entry per
   platform plus the RAG/source-freshness and router-automation
   categories), each classified as ``offline_fixture``, ``live_read``,
   ``disposable_write``, ``blocked``, ``unavailable``, or ``coverage_gap``
   (see ``src/hpe_networking_mcp/pipeline/validation_matrix.py``). Never a record of an executed
   disposable write -- that classification only ever reflects
   authorization state, matching every other v0.7 evaluator's
   never-invoked-by-default write probe.
10. ``compliance_report`` -- a bounded, declarative compliance-policy
    evaluation report produced by
    ``hpe_networking_mcp.mcp_servers.tool_router.evaluate_compliance_policy`` (see
    ``src/hpe_networking_mcp/pipeline/compliance.py``): per-rule pass/fail/error/skipped results
    plus aggregate counts against caller-supplied, already-retrieved
    observations. Never includes raw secrets and never represents a live
    fetch -- observations must already be in hand before the report is
    built.

Typical usage::

    from hpe_networking_mcp.pipeline import artifact_contracts as contracts

    entry = contracts.write_artifact(
        "outputs/aos8-live-evidence.json",
        contracts.LIVE_LIFECYCLE_EVIDENCE,
        {
            "platform": "aos8",
            "mode": "read_only",
            "generated_at": "2026-07-25T12:00:00+00:00",
            "steps": [{"name": "list_vlans", "status": "ok"}],
        },
    )

``write_artifact`` validates, redacts, and atomically writes the artifact,
returning a :class:`ManifestEntry` ready to fold into a
:class:`ReleaseArtifactManifest`.

This module intentionally has no third-party dependency (plain
``dataclasses``, matching the rest of the src/hpe_networking_mcp/pipeline/mcp_servers code) and
performs no network I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hpe_networking_mcp.mcp_servers.shared import redact_sensitive


class ArtifactValidationError(ValueError):
    """Raised when an artifact payload fails schema, type, or bound validation."""


# ---------------------------------------------------------------------------
# Artifact kinds and schema versions
# ---------------------------------------------------------------------------

LIVE_LIFECYCLE_EVIDENCE = "live_lifecycle_evidence"
PLATFORM_COMPATIBILITY_RESULT = "platform_compatibility_result"
MIGRATION_REPORT_METADATA = "migration_report_metadata"
CAPABILITY_SNAPSHOT = "capability_snapshot"
SOURCE_FRESHNESS_RESULT = "source_freshness_result"
RELEASE_ARTIFACT_MANIFEST = "release_artifact_manifest"
ROUTER_DEPENDENCY_PLAN = "router_dependency_plan"
ROUTER_RECONCILIATION_PLAN = "router_reconciliation_plan"
VALIDATION_MATRIX_RESULT = "validation_matrix_result"
COMPLIANCE_REPORT = "compliance_report"

ARTIFACT_KINDS: tuple[str, ...] = (
    LIVE_LIFECYCLE_EVIDENCE,
    PLATFORM_COMPATIBILITY_RESULT,
    MIGRATION_REPORT_METADATA,
    CAPABILITY_SNAPSHOT,
    SOURCE_FRESHNESS_RESULT,
    RELEASE_ARTIFACT_MANIFEST,
    ROUTER_DEPENDENCY_PLAN,
    ROUTER_RECONCILIATION_PLAN,
    VALIDATION_MATRIX_RESULT,
    COMPLIANCE_REPORT,
)

# Every kind starts at schema version 1. Bump the kind's entry here (and
# validate the new shape in that kind's dataclass) on any breaking change;
# never silently reinterpret an older file under a new shape.
SCHEMA_VERSIONS: dict[str, int] = {kind: 1 for kind in ARTIFACT_KINDS}

# ---------------------------------------------------------------------------
# Bounds -- every growable collection below has a hard ceiling. Validation
# fails closed (raises) instead of silently truncating evidence, because a
# silently-truncated audit artifact is worse than a loud upstream error.
# ---------------------------------------------------------------------------
MAX_EVIDENCE_STEPS = 200
MAX_EVIDENCE_ERRORS = 100
MAX_COMPAT_REASONS = 50
MAX_MATRIX_PLATFORMS = 50
MAX_COMPATIBILITY_OPERATIONS = 5000
MAX_MIGRATION_DEVICE_REFS = 1000
MAX_SNAPSHOT_PLATFORMS = 50
MAX_CAPABILITY_TOOLS_PER_PLATFORM = 5000
MAX_FRESHNESS_DETAILS = 200
MAX_FRESHNESS_DETAIL_CHARS = 500
MAX_MANIFEST_ENTRIES = 500
MAX_KNOWN_SENSITIVE_VALUES = 200
MAX_KNOWN_SENSITIVE_VALUE_CHARS = 2048
MAX_ROUTER_PLAN_STEPS = 25
MAX_ROUTER_PLAN_CYCLES = 10
MAX_ROUTER_PLAN_CYCLE_LENGTH = 26
MAX_ROUTER_RECONCILIATION_ENTRIES = 100
MAX_ROUTER_RECONCILIATION_EXCLUDED = 200
MAX_ROUTER_CADENCE_CHARS = 200
MAX_VALIDATION_MATRIX_ENTRIES = 50
MAX_VALIDATION_MATRIX_DETAIL_CHARS = 500
# Paired with hpe_networking_mcp.pipeline.compliance's own MAX_POLICY_RULES/MAX_OBSERVATIONS/
# MAX_RESULT_ENTRIES bounds -- independently enforced here so a malformed
# caller-built payload (bypassing hpe_networking_mcp.pipeline.compliance entirely) still
# cannot produce an oversized compliance_report artifact.
MAX_COMPLIANCE_RULES = 50
MAX_COMPLIANCE_OBSERVATIONS = 100
MAX_COMPLIANCE_RESULTS = 500
MAX_COMPLIANCE_MESSAGE_CHARS = 500
MAX_COMPLIANCE_VALUE_CHARS = 500
# Overall safety ceiling for one serialized artifact file.
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LIVE_LIFECYCLE_MODES = ("read_only", "disposable_write")

# The five states a scheduled source-freshness check can report for one
# source (see SourceFreshnessEntry.status and
# scripts/check_security_lifecycle_drift.py). Never success-shaped: an
# unreachable or structurally-broken source must show up as
# "unavailable"/"changed", not silently as "fresh".
FRESHNESS_STATUSES: tuple[str, ...] = (
    "fresh",
    "stale",
    "unavailable",
    "changed",
    "coverage_gap",
)

# The six states one category (a live-test platform, or the
# RAG/source-freshness or router-automation category) can report in a
# `validation_matrix_result` (see src/hpe_networking_mcp/pipeline/validation_matrix.py):
#
# - "offline_fixture" -- a bounded, network-free fixture/self-check ran and
#   passed (the default, always-safe state).
# - "live_read" -- a bounded, GET-only live call ran because the operator
#   explicitly set the platform's read opt-in (src/hpe_networking_mcp/pipeline/live_test_config.py)
#   and credentials were configured.
# - "disposable_write" -- both the read and write opt-ins are set and
#   credentials are configured, so the platform's own disposable-write
#   harness is *authorized*. This state is never produced by actually
#   invoking that harness here -- every v0.7 evaluator's write probe stays
#   never-invoked by default (see e.g.
#   scripts/evaluate_central_070_readonly.py's `--live-write` handling).
# - "blocked" -- the read opt-in is not set (the default, safe-by-default
#   state for any category that has no offline fixture path).
# - "unavailable" -- an opt-in was set but credentials are not configured,
#   or the fixture/self-check itself raised.
# - "coverage_gap" -- a permanent, documented limitation (e.g. no live
#   write API exists for this platform) rather than an unset opt-in.
VALIDATION_MATRIX_CLASSIFICATIONS: tuple[str, ...] = (
    "offline_fixture",
    "live_read",
    "disposable_write",
    "blocked",
    "unavailable",
    "coverage_gap",
)

# The four states one compliance-policy rule result can report for one
# observation (see hpe_networking_mcp.pipeline.compliance.RULE_STATUSES, which this mirrors
# independently so an artifact built directly from a plain dict -- bypassing
# hpe_networking_mcp.pipeline.compliance -- is still validated against the same fixed set).
# Never success-shaped: "error" (a type mismatch or missing required field)
# is always distinct from "pass", and a ComplianceReport's own "compliant"
# flag must agree with whether any "fail"/"error" results exist (see
# ComplianceReport.__post_init__).
COMPLIANCE_RULE_STATUSES: tuple[str, ...] = ("pass", "fail", "error", "skipped")


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _require_str(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise ArtifactValidationError(f"{field_name} must not be empty")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactValidationError(f"{field_name} must be a bool, got {type(value).__name__}")
    return value


def _require_int(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactValidationError(f"{field_name} must be an int, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ArtifactValidationError(f"{field_name} must be >= {minimum}")
    return value


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactValidationError(f"{field_name} must be an object/mapping")
    return value


def _require_sequence(value: Any, field_name: str, *, max_items: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ArtifactValidationError(f"{field_name} must be a list")
    if len(value) > max_items:
        raise ArtifactValidationError(
            f"{field_name} has {len(value)} items, exceeding the bound of {max_items}"
        )
    return value


def _require_iso_timestamp(value: Any, field_name: str) -> str:
    text = _require_str(value, field_name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArtifactValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return text


def _check_schema_kind(instance: Any, kind: str) -> None:
    if instance.schema_version != SCHEMA_VERSIONS[kind]:
        raise ArtifactValidationError(
            f"unsupported {kind} schema_version: {instance.schema_version!r}"
        )
    if instance.kind != kind:
        raise ArtifactValidationError(f"kind mismatch: expected {kind!r}, got {instance.kind!r}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_identifier(raw: Any) -> str:
    """Deterministic, irreversible placeholder for one identifier.

    Matches the convention already established by
    ``scripts/evaluate_aos8_050_readonly.py``'s ``_sanitize_identifier``:
    a truncated SHA-256 hex digest, never the raw value, so two artifacts
    that reference the same real identifier can still be correlated
    without ever persisting it.
    """
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Recursive redaction -- reuses hpe_networking_mcp.mcp_servers.shared.redact_sensitive for
# credential-shaped keys/values, then layers on tenant/workspace/account/
# scope identifier scrubbing and raw-response-shaped key scrubbing. Never
# reimplements secret detection; only adds artifact-specific coverage.
# ---------------------------------------------------------------------------

_TENANT_KEY_EXACT = {
    "tenant",
    "tenant_id",
    "workspace",
    "workspace_id",
    "glp_workspace_id",
    "account",
    "account_id",
    "customer",
    "customer_id",
    "org_id",
    "organization_id",
    "subscription_id",
    "scope_id",
    "scope_name",
    "device_scope_id",
    "cluster_scope_id",
    "cluster_name",
}
_TENANT_KEY_SUFFIXES = (
    "tenant_id",
    "workspace_id",
    "account_id",
    "customer_id",
    "org_id",
    "organization_id",
    "scope_id",
    "scope_name",
)
_RAW_PAYLOAD_KEY_EXACT = {
    "cookie",
    "cookies",
    "set_cookie",
    "raw_body",
    "raw_response",
    "response_body",
    "vendor_response",
    "http_body",
}
_RAW_PAYLOAD_MARKER = "**OMITTED-RAW-PAYLOAD**"


def _normalize_key(key: Any) -> str:
    key_text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")


def _is_tenant_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in _TENANT_KEY_EXACT or any(
        normalized.endswith(suffix) for suffix in _TENANT_KEY_SUFFIXES
    )


def _is_raw_payload_key(key: Any) -> bool:
    return _normalize_key(key) in _RAW_PAYLOAD_KEY_EXACT


def _redact_tenant_and_raw_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_raw_payload_key(key) and item is not None:
                out[key] = _RAW_PAYLOAD_MARKER
            elif _is_tenant_key(key) and item is not None:
                out[key] = hash_identifier(item)
            else:
                out[key] = _redact_tenant_and_raw_payload(item)
        return out
    if isinstance(value, list):
        return [_redact_tenant_and_raw_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_tenant_and_raw_payload(item) for item in value)
    return value


def _redact_known_sensitive_values(
    value: Any,
    known_sensitive_values: Sequence[str],
) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _redact_known_sensitive_values(item, known_sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_known_sensitive_values(item, known_sensitive_values)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_known_sensitive_values(item, known_sensitive_values)
            for item in value
        )
    if not isinstance(value, str):
        return value
    redacted = value
    for sensitive in known_sensitive_values:
        redacted = redacted.replace(sensitive, "**REDACTED**")
    return redacted


def _normalize_known_sensitive_values(
    values: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ArtifactValidationError("known_sensitive_values must be a list")
    if len(values) > MAX_KNOWN_SENSITIVE_VALUES:
        raise ArtifactValidationError(
            "known_sensitive_values exceeds the configured safety bound"
        )
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ArtifactValidationError(
                "known_sensitive_values entries must be strings"
            )
        if len(value) > MAX_KNOWN_SENSITIVE_VALUE_CHARS:
            raise ArtifactValidationError(
                "known_sensitive_values entry exceeds the configured safety bound"
            )
        if value:
            normalized.add(value)
    return tuple(sorted(normalized, key=lambda item: (-len(item), item)))


def redact_artifact_payload(
    value: Any,
    *,
    known_sensitive_values: Sequence[str] = (),
) -> Any:
    """Recursively scrub secrets, then tenant/workspace/account identifiers.

    Secrets (tokens, passwords, API keys, ...) are stripped first by the
    shared, independently-tested ``hpe_networking_mcp.mcp_servers.shared.redact_sensitive``.
    Tenant/workspace/account/scope identifiers and raw-response-shaped
    fields are then replaced with a deterministic, irreversible
    placeholder. Call this before writing any evidence artifact; use
    :func:`write_artifact` (default ``redact=True``) rather than calling
    this directly wherever possible.
    """
    normalized = _normalize_known_sensitive_values(known_sensitive_values)
    redacted = _redact_tenant_and_raw_payload(redact_sensitive(value))
    return _redact_known_sensitive_values(redacted, normalized)


# ---------------------------------------------------------------------------
# 1. Live lifecycle evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveLifecycleEvidence:
    """Bounded evidence from one live read-only or disposable-write probe."""

    platform: str
    mode: str
    generated_at: str
    steps: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    summary: Mapping[str, Any] = field(default_factory=dict)
    errors: Sequence[str] = field(default_factory=tuple)
    target_identifier_hash: str | None = None
    secrets_included: bool = False
    raw_response_included: bool = False
    schema_version: int = SCHEMA_VERSIONS[LIVE_LIFECYCLE_EVIDENCE]
    kind: str = LIVE_LIFECYCLE_EVIDENCE

    def __post_init__(self) -> None:
        _require_str(self.platform, "platform")
        if self.mode not in _LIVE_LIFECYCLE_MODES:
            raise ArtifactValidationError(f"mode must be one of {_LIVE_LIFECYCLE_MODES}")
        _require_iso_timestamp(self.generated_at, "generated_at")
        steps = _require_sequence(self.steps, "steps", max_items=MAX_EVIDENCE_STEPS)
        for index, step in enumerate(steps):
            if (
                not isinstance(step, Mapping)
                or "name" not in step
                or "status" not in step
            ):
                raise ArtifactValidationError(
                    f"steps[{index}] must be an object with 'name' and 'status'"
                )
        _require_mapping(self.summary, "summary")
        _require_sequence(self.errors, "errors", max_items=MAX_EVIDENCE_ERRORS)
        if self.target_identifier_hash is not None:
            candidate = _require_str(self.target_identifier_hash, "target_identifier_hash")
            if not candidate.startswith("sha256:"):
                raise ArtifactValidationError(
                    "target_identifier_hash must be a sha256:<hex> placeholder "
                    "(use hash_identifier()), never a raw identifier"
                )
        _require_bool(self.secrets_included, "secrets_included")
        if self.secrets_included:
            raise ArtifactValidationError(
                "secrets_included must be False; redact before creating evidence"
            )
        _require_bool(self.raw_response_included, "raw_response_included")
        if self.raw_response_included:
            raise ArtifactValidationError(
                "raw_response_included must be False; summarize vendor responses instead"
            )
        _check_schema_kind(self, LIVE_LIFECYCLE_EVIDENCE)


# ---------------------------------------------------------------------------
# 2. Platform compatibility result / matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformCompatibilityEntry:
    """One platform's compatibility verdict against a committed baseline."""

    platform: str
    compatible: bool
    reasons: Sequence[str] = field(default_factory=tuple)
    operations_added: int = 0
    operations_removed: int = 0
    operations_changed: int = 0
    source_sha256: str = ""
    baseline_sha256: str = ""

    def __post_init__(self) -> None:
        _require_str(self.platform, "platform")
        _require_bool(self.compatible, "compatible")
        _require_sequence(self.reasons, "reasons", max_items=MAX_COMPAT_REASONS)
        for name, value in (
            ("operations_added", self.operations_added),
            ("operations_removed", self.operations_removed),
            ("operations_changed", self.operations_changed),
        ):
            _require_int(value, name, minimum=0)
        total = self.operations_added + self.operations_removed + self.operations_changed
        if total > MAX_COMPATIBILITY_OPERATIONS:
            raise ArtifactValidationError(
                f"platform {self.platform!r} operation delta counts ({total}) exceed "
                f"the {MAX_COMPATIBILITY_OPERATIONS} bound"
            )
        for name, value in (
            ("source_sha256", self.source_sha256),
            ("baseline_sha256", self.baseline_sha256),
        ):
            _require_str(value, name, allow_empty=True)
            if value and not _SHA256_RE.match(value):
                raise ArtifactValidationError(f"{name} must be a 64-character hex sha256 digest")
        if not self.compatible and not self.reasons:
            raise ArtifactValidationError(
                f"platform {self.platform!r} is incompatible but has no reasons"
            )


@dataclass(frozen=True)
class PlatformCompatibilityMatrix:
    """A bounded collection of :class:`PlatformCompatibilityEntry` results.

    A single-entry matrix *is* a "result"; a multi-entry matrix is the full
    cross-platform matrix. Both use this one bounded shape.
    """

    generated_at: str
    entries: Sequence[PlatformCompatibilityEntry] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[PLATFORM_COMPATIBILITY_RESULT]
    kind: str = PLATFORM_COMPATIBILITY_RESULT

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        entries = _require_sequence(self.entries, "entries", max_items=MAX_MATRIX_PLATFORMS)
        if not entries:
            raise ArtifactValidationError("entries must contain at least one platform result")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, PlatformCompatibilityEntry):
                raise ArtifactValidationError(
                    "entries must be PlatformCompatibilityEntry instances"
                )
            if entry.platform in seen:
                raise ArtifactValidationError(f"duplicate platform entry: {entry.platform!r}")
            seen.add(entry.platform)
        _check_schema_kind(self, PLATFORM_COMPATIBILITY_RESULT)


# ---------------------------------------------------------------------------
# 3. Migration report metadata
# ---------------------------------------------------------------------------

_MIGRATION_REPORT_FORMATS = ("csv", "json", "html")
_MIGRATION_STATUS_KEYS = ("done", "partial", "failed", "skipped")


@dataclass(frozen=True)
class MigrationReportMetadata:
    """Metadata describing a migration report -- never the raw device rows.

    The raw per-device rows stay in ``src/hpe_networking_mcp/pipeline/reporter.py``'s CSV/JSON/HTML
    outputs; this contract is a small, bounded, redacted summary suitable
    for release evidence.
    """

    run_id: str
    generated_at: str
    report_formats: Sequence[str] = field(default_factory=tuple)
    device_count: int = 0
    status_counts: Mapping[str, int] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    device_ref_hashes: Sequence[str] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[MIGRATION_REPORT_METADATA]
    kind: str = MIGRATION_REPORT_METADATA

    def __post_init__(self) -> None:
        _require_str(self.run_id, "run_id")
        _require_iso_timestamp(self.generated_at, "generated_at")
        formats = _require_sequence(
            self.report_formats, "report_formats", max_items=len(_MIGRATION_REPORT_FORMATS)
        )
        unsupported_formats = sorted(set(formats) - set(_MIGRATION_REPORT_FORMATS))
        if unsupported_formats:
            raise ArtifactValidationError(
                f"unsupported report_formats: {', '.join(unsupported_formats)}"
            )
        _require_int(self.device_count, "device_count", minimum=0)
        status_counts = _require_mapping(self.status_counts, "status_counts")
        unknown_status = sorted(set(status_counts) - set(_MIGRATION_STATUS_KEYS))
        if unknown_status:
            raise ArtifactValidationError(
                f"unsupported status_counts keys: {', '.join(unknown_status)}"
            )
        for status_key, value in status_counts.items():
            _require_int(value, f"status_counts[{status_key}]", minimum=0)
        for name, value in (("started_at", self.started_at), ("ended_at", self.ended_at)):
            if value is not None:
                _require_iso_timestamp(value, name)
        refs = _require_sequence(
            self.device_ref_hashes, "device_ref_hashes", max_items=MAX_MIGRATION_DEVICE_REFS
        )
        for index, ref in enumerate(refs):
            if not isinstance(ref, str) or not ref.startswith("sha256:"):
                raise ArtifactValidationError(
                    f"device_ref_hashes[{index}] must be a sha256:<hex> placeholder "
                    "(use hash_identifier()), never a raw serial number"
                )
        _check_schema_kind(self, MIGRATION_REPORT_METADATA)


# ---------------------------------------------------------------------------
# 4. Capability snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformCapabilityCount:
    """One platform's read/diagnostic/write/destructive tool counts."""

    platform: str
    read: int = 0
    diagnostic: int = 0
    write: int = 0
    destructive: int = 0
    source: str = "combined"

    def __post_init__(self) -> None:
        _require_str(self.platform, "platform")
        for name in ("read", "diagnostic", "write", "destructive"):
            _require_int(getattr(self, name), name, minimum=0)
        total = self.read + self.diagnostic + self.write + self.destructive
        if total > MAX_CAPABILITY_TOOLS_PER_PLATFORM:
            raise ArtifactValidationError(
                f"platform {self.platform!r} tool count ({total}) exceeds "
                f"the {MAX_CAPABILITY_TOOLS_PER_PLATFORM} bound"
            )
        _require_str(self.source, "source")


@dataclass(frozen=True)
class CapabilitySnapshot:
    """A bounded collection of per-platform :class:`PlatformCapabilityCount`."""

    generated_at: str
    platforms: Sequence[PlatformCapabilityCount] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[CAPABILITY_SNAPSHOT]
    kind: str = CAPABILITY_SNAPSHOT

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        entries = _require_sequence(self.platforms, "platforms", max_items=MAX_SNAPSHOT_PLATFORMS)
        if not entries:
            raise ArtifactValidationError("platforms must contain at least one entry")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, PlatformCapabilityCount):
                raise ArtifactValidationError(
                    "platforms entries must be PlatformCapabilityCount instances"
                )
            if entry.platform in seen:
                raise ArtifactValidationError(f"duplicate platform entry: {entry.platform!r}")
            seen.add(entry.platform)
        _check_schema_kind(self, CAPABILITY_SNAPSHOT)


# ---------------------------------------------------------------------------
# 5. Source freshness / drift result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFreshnessEntry:
    """One source's observed count versus its committed minimum.

    ``status`` distinguishes the five states a scheduled source check can
    report (see ``scripts/check_security_lifecycle_drift.py``):

    - ``fresh`` -- fetched, parsed, and met its committed minimum count.
    - ``stale`` -- fetched and parsed, but the count regressed below the
      committed minimum.
    - ``unavailable`` -- the source could not be fetched at all (network,
      timeout, HTTP error).
    - ``changed`` -- the source was fetched but no longer parses the way
      its reviewed provenance pin expects (a structural/schema break, not
      a plain connectivity failure), and needs human review before the
      pin is updated.
    - ``coverage_gap`` -- an explicit, documented limitation (no reliable
      official machine-readable source exists yet) rather than a fetch or
      parse failure; never silently treated as ``fresh``.

    ``drift_detected`` is kept for backward compatibility with earlier
    callers that only distinguish pass/fail; new callers should prefer
    ``status``.
    """

    source: str
    count: int
    minimum: int
    status: str = "fresh"
    drift_detected: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        _require_str(self.source, "source")
        _require_int(self.count, "count", minimum=0)
        _require_int(self.minimum, "minimum", minimum=0)
        if self.status not in FRESHNESS_STATUSES:
            raise ArtifactValidationError(
                f"status must be one of {FRESHNESS_STATUSES}, got {self.status!r}"
            )
        _require_bool(self.drift_detected, "drift_detected")
        _require_str(self.detail, "detail", allow_empty=True)
        if len(self.detail) > MAX_FRESHNESS_DETAIL_CHARS:
            raise ArtifactValidationError(
                f"detail exceeds the {MAX_FRESHNESS_DETAIL_CHARS}-character bound"
            )


@dataclass(frozen=True)
class SourceFreshnessSnapshot:
    """A bounded collection of per-source :class:`SourceFreshnessEntry`."""

    generated_at: str
    entries: Sequence[SourceFreshnessEntry] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[SOURCE_FRESHNESS_RESULT]
    kind: str = SOURCE_FRESHNESS_RESULT

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        entries = _require_sequence(self.entries, "entries", max_items=MAX_FRESHNESS_DETAILS)
        if not entries:
            raise ArtifactValidationError("entries must contain at least one source")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, SourceFreshnessEntry):
                raise ArtifactValidationError("entries must be SourceFreshnessEntry instances")
            if entry.source in seen:
                raise ArtifactValidationError(f"duplicate source entry: {entry.source!r}")
            seen.add(entry.source)
        _check_schema_kind(self, SOURCE_FRESHNESS_RESULT)


# ---------------------------------------------------------------------------
# 6. Release artifact manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One artifact file's manifest record."""

    filename: str
    kind: str
    schema_version: int
    size_bytes: int
    sha256: str
    generated_at: str
    redacted: bool

    def __post_init__(self) -> None:
        _require_str(self.filename, "filename")
        if (
            self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or Path(self.filename).name != self.filename
        ):
            raise ArtifactValidationError("filename must be a plain basename")
        if self.kind not in ARTIFACT_KINDS:
            raise ArtifactValidationError(f"unknown manifest entry kind {self.kind!r}")
        _require_int(self.schema_version, "schema_version", minimum=1)
        _require_int(self.size_bytes, "size_bytes", minimum=0)
        if not isinstance(self.sha256, str) or not _SHA256_RE.match(self.sha256):
            raise ArtifactValidationError("sha256 must be a 64-character hex digest")
        _require_iso_timestamp(self.generated_at, "generated_at")
        _require_bool(self.redacted, "redacted")


@dataclass(frozen=True)
class ReleaseArtifactManifest:
    """A bounded manifest of artifact files produced for one release."""

    generated_at: str
    release_version: str
    entries: Sequence[ManifestEntry] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[RELEASE_ARTIFACT_MANIFEST]
    kind: str = RELEASE_ARTIFACT_MANIFEST

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        _require_str(self.release_version, "release_version")
        entries = _require_sequence(self.entries, "entries", max_items=MAX_MANIFEST_ENTRIES)
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, ManifestEntry):
                raise ArtifactValidationError("entries must be ManifestEntry instances")
            if entry.filename in seen:
                raise ArtifactValidationError(f"duplicate manifest filename: {entry.filename!r}")
            seen.add(entry.filename)
        _check_schema_kind(self, RELEASE_ARTIFACT_MANIFEST)


# ---------------------------------------------------------------------------
# 7. Router dependency plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouterPlanStep:
    """One resolved (or explicitly unresolved) step in a router dependency plan."""

    step_id: str
    tool: str | None = None
    resolved: bool = False
    ambiguous: bool = False
    capability: str = "unknown"
    platform: str | None = None
    depends_on: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_str(self.step_id, "step_id")
        if self.tool is not None:
            _require_str(self.tool, "tool")
        _require_bool(self.resolved, "resolved")
        _require_bool(self.ambiguous, "ambiguous")
        _require_str(self.capability, "capability")
        if self.platform is not None:
            _require_str(self.platform, "platform")
        _require_sequence(self.depends_on, "depends_on", max_items=MAX_ROUTER_PLAN_STEPS)
        if not self.resolved and self.tool is not None:
            raise ArtifactValidationError("an unresolved step must not carry a tool name")


@dataclass(frozen=True)
class RouterDependencyPlan:
    """A bounded, deterministic, read-only dependency/order plan.

    Never represents an executed workflow -- only a plan. ``order`` is
    populated only when ``acyclic`` is True and every step resolved cleanly;
    ``cycles``/``unresolved_step_ids`` explain why ordering was withheld
    otherwise.
    """

    generated_at: str
    steps: Sequence[RouterPlanStep] = field(default_factory=tuple)
    order: Sequence[str] = field(default_factory=tuple)
    acyclic: bool = True
    cycles: Sequence[Sequence[str]] = field(default_factory=tuple)
    unresolved_step_ids: Sequence[str] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[ROUTER_DEPENDENCY_PLAN]
    kind: str = ROUTER_DEPENDENCY_PLAN

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        steps = _require_sequence(self.steps, "steps", max_items=MAX_ROUTER_PLAN_STEPS)
        if not steps:
            raise ArtifactValidationError("steps must contain at least one entry")
        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, RouterPlanStep):
                raise ArtifactValidationError("steps entries must be RouterPlanStep instances")
            if step.step_id in seen:
                raise ArtifactValidationError(f"duplicate step_id: {step.step_id!r}")
            seen.add(step.step_id)
        order = _require_sequence(self.order, "order", max_items=MAX_ROUTER_PLAN_STEPS)
        if len(set(order)) != len(order):
            raise ArtifactValidationError("order must not contain duplicate step ids")
        unknown_order_ids = [step_id for step_id in order if step_id not in seen]
        if unknown_order_ids:
            raise ArtifactValidationError(
                f"order references unknown step_id(s): {unknown_order_ids!r}"
            )
        _require_bool(self.acyclic, "acyclic")
        cycles = _require_sequence(self.cycles, "cycles", max_items=MAX_ROUTER_PLAN_CYCLES)
        for index, cycle in enumerate(cycles):
            _require_sequence(cycle, f"cycles[{index}]", max_items=MAX_ROUTER_PLAN_CYCLE_LENGTH)
        _require_sequence(
            self.unresolved_step_ids, "unresolved_step_ids", max_items=MAX_ROUTER_PLAN_STEPS
        )
        if self.acyclic and self.cycles:
            raise ArtifactValidationError("acyclic plans must not report cycles")
        if not self.acyclic and not self.cycles:
            raise ArtifactValidationError("non-acyclic plans must report at least one cycle")
        _check_schema_kind(self, ROUTER_DEPENDENCY_PLAN)


# ---------------------------------------------------------------------------
# 8. Router reconciliation plan
# ---------------------------------------------------------------------------

_RECONCILIATION_ELIGIBLE_CAPABILITIES = ("read", "diagnostic")


@dataclass(frozen=True)
class ReconciliationEntry:
    """One tool scheduled for recurring, read-only reconciliation."""

    tool: str
    server: str
    platform: str
    capability: str
    enabled: bool

    def __post_init__(self) -> None:
        _require_str(self.tool, "tool")
        _require_str(self.server, "server")
        _require_str(self.platform, "platform")
        if self.capability not in _RECONCILIATION_ELIGIBLE_CAPABILITIES:
            raise ArtifactValidationError(
                f"capability {self.capability!r} is not eligible for reconciliation; "
                f"expected one of {_RECONCILIATION_ELIGIBLE_CAPABILITIES}"
            )
        _require_bool(self.enabled, "enabled")


@dataclass(frozen=True)
class RouterReconciliationPlan:
    """A bounded, read-only, plan-only recurring reconciliation schedule.

    Never a live schedule: no OS timer/cron/GitHub Actions schedule is ever
    created from this artifact. ``dry_run`` must always be True.
    """

    generated_at: str
    cadence: Mapping[str, Any]
    entries: Sequence[ReconciliationEntry] = field(default_factory=tuple)
    excluded_count: int = 0
    excluded: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    dry_run: bool = True
    schema_version: int = SCHEMA_VERSIONS[ROUTER_RECONCILIATION_PLAN]
    kind: str = ROUTER_RECONCILIATION_PLAN

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        cadence = _require_mapping(self.cadence, "cadence")
        if not cadence.get("valid"):
            raise ArtifactValidationError("cadence must be a validated (valid=True) descriptor")
        if len(json.dumps(dict(cadence), default=str)) > MAX_ROUTER_CADENCE_CHARS:
            raise ArtifactValidationError("cadence descriptor exceeds the configured safety bound")
        entries = _require_sequence(
            self.entries, "entries", max_items=MAX_ROUTER_RECONCILIATION_ENTRIES
        )
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, ReconciliationEntry):
                raise ArtifactValidationError("entries must be ReconciliationEntry instances")
            if entry.tool in seen:
                raise ArtifactValidationError(
                    f"duplicate reconciliation entry tool: {entry.tool!r}"
                )
            seen.add(entry.tool)
        _require_int(self.excluded_count, "excluded_count", minimum=0)
        excluded = _require_sequence(
            self.excluded, "excluded", max_items=MAX_ROUTER_RECONCILIATION_EXCLUDED
        )
        if self.excluded_count < len(excluded):
            raise ArtifactValidationError(
                "excluded_count must be >= len(excluded); the detail list may be "
                "capped below the true total, never the other way around"
            )
        for index, item in enumerate(excluded):
            _require_mapping(item, f"excluded[{index}]")
        if self.dry_run is not True:
            raise ArtifactValidationError("dry_run must be True; this contract is plan-only")
        _check_schema_kind(self, ROUTER_RECONCILIATION_PLAN)


# ---------------------------------------------------------------------------
# 9. Validation matrix result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationMatrixEntry:
    """One category's (platform, or rag/source-freshness, or
    router-automation) classification in the v0.7 validation matrix."""

    category: str
    classification: str
    detail: str = ""
    read_enabled: bool = False
    write_enabled: bool = False
    credentials_configured: bool = False

    def __post_init__(self) -> None:
        _require_str(self.category, "category")
        if self.classification not in VALIDATION_MATRIX_CLASSIFICATIONS:
            raise ArtifactValidationError(
                f"classification must be one of {VALIDATION_MATRIX_CLASSIFICATIONS}, "
                f"got {self.classification!r}"
            )
        _require_str(self.detail, "detail", allow_empty=True)
        if len(self.detail) > MAX_VALIDATION_MATRIX_DETAIL_CHARS:
            raise ArtifactValidationError(
                f"detail exceeds the {MAX_VALIDATION_MATRIX_DETAIL_CHARS}-character bound"
            )
        _require_bool(self.read_enabled, "read_enabled")
        _require_bool(self.write_enabled, "write_enabled")
        _require_bool(self.credentials_configured, "credentials_configured")
        if self.write_enabled and not self.read_enabled:
            raise ArtifactValidationError(
                "write_enabled cannot be True while read_enabled is False "
                "(disposable-write is never authorized without the read opt-in)"
            )


@dataclass(frozen=True)
class ValidationMatrix:
    """A bounded collection of :class:`ValidationMatrixEntry` -- one per
    platform plus the RAG/source-freshness and router-automation
    categories -- produced by ``scripts/run_v07_validation_matrix.py``."""

    generated_at: str
    entries: Sequence[ValidationMatrixEntry] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSIONS[VALIDATION_MATRIX_RESULT]
    kind: str = VALIDATION_MATRIX_RESULT

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        entries = _require_sequence(
            self.entries, "entries", max_items=MAX_VALIDATION_MATRIX_ENTRIES
        )
        if not entries:
            raise ArtifactValidationError("entries must contain at least one category")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, ValidationMatrixEntry):
                raise ArtifactValidationError(
                    "entries must be ValidationMatrixEntry instances"
                )
            if entry.category in seen:
                raise ArtifactValidationError(f"duplicate category entry: {entry.category!r}")
            seen.add(entry.category)
        _check_schema_kind(self, VALIDATION_MATRIX_RESULT)


# ---------------------------------------------------------------------------
# 10. Compliance report
# ---------------------------------------------------------------------------


def _require_counts_mapping(value: Any, field_name: str) -> Mapping[str, int]:
    counts = _require_mapping(value, field_name)
    for key in COMPLIANCE_RULE_STATUSES:
        if key not in counts:
            raise ArtifactValidationError(f"{field_name} is missing required key {key!r}")
        _require_int(counts[key], f"{field_name}.{key}", minimum=0)
    return counts


@dataclass(frozen=True)
class ComplianceRuleResult:
    """One rule's evaluated outcome against one observation.

    ``status`` is always exactly one of :data:`COMPLIANCE_RULE_STATUSES` --
    never success-shaped: a type mismatch or a missing required field is
    ``"error"``, never silently ``"pass"``.
    """

    rule_id: str
    field: str
    operator: str
    status: str
    observation_index: int
    observation_id: str | None = None
    severity: str = "error"
    actual: Any = None
    message: str = ""

    def __post_init__(self) -> None:
        _require_str(self.rule_id, "rule_id")
        _require_str(self.field, "field")
        _require_str(self.operator, "operator")
        if self.status not in COMPLIANCE_RULE_STATUSES:
            raise ArtifactValidationError(
                f"status must be one of {COMPLIANCE_RULE_STATUSES}, got {self.status!r}"
            )
        _require_int(self.observation_index, "observation_index", minimum=0)
        if self.observation_id is not None:
            _require_str(self.observation_id, "observation_id")
            if len(self.observation_id) > MAX_COMPLIANCE_VALUE_CHARS:
                raise ArtifactValidationError(
                    f"observation_id exceeds the {MAX_COMPLIANCE_VALUE_CHARS}-character bound"
                )
        _require_str(self.severity, "severity")
        _require_str(self.message, "message", allow_empty=True)
        if len(self.message) > MAX_COMPLIANCE_MESSAGE_CHARS:
            raise ArtifactValidationError(
                f"message exceeds the {MAX_COMPLIANCE_MESSAGE_CHARS}-character bound"
            )
        try:
            actual_size = len(json.dumps(self.actual, default=str))
        except TypeError as exc:
            raise ArtifactValidationError(f"actual is not JSON serializable: {exc}") from exc
        if actual_size > MAX_COMPLIANCE_VALUE_CHARS * 4:
            raise ArtifactValidationError("actual exceeds the configured safety bound")


@dataclass(frozen=True)
class ComplianceObservationSummary:
    """One observation's compliant flag and per-status rule counts."""

    observation_index: int
    compliant: bool
    counts: Mapping[str, int]
    observation_id: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.observation_index, "observation_index", minimum=0)
        _require_bool(self.compliant, "compliant")
        counts = _require_counts_mapping(self.counts, "counts")
        expected_compliant = counts["fail"] == 0 and counts["error"] == 0
        if self.compliant != expected_compliant:
            raise ArtifactValidationError(
                "compliant must be False whenever counts.fail or counts.error is nonzero "
                "(never success-shaped)"
            )
        if self.observation_id is not None:
            _require_str(self.observation_id, "observation_id")
            if len(self.observation_id) > MAX_COMPLIANCE_VALUE_CHARS:
                raise ArtifactValidationError(
                    f"observation_id exceeds the {MAX_COMPLIANCE_VALUE_CHARS}-character bound"
                )


@dataclass(frozen=True)
class ComplianceReport:
    """A bounded, declarative compliance-policy evaluation report.

    Produced by ``hpe_networking_mcp.mcp_servers.tool_router.evaluate_compliance_policy``
    (backed by ``src/hpe_networking_mcp/pipeline/compliance.py``) against caller-supplied,
    already-retrieved observations -- never a live fetch. ``results`` may be
    capped below ``results_total`` (mirroring the
    ``RouterReconciliationPlan.excluded``/``excluded_count`` pattern); the
    aggregate ``counts`` always reflect the true total regardless of the
    detail-list cap.
    """

    generated_at: str
    policy_id: str
    compliant: bool
    counts: Mapping[str, int]
    observations: Sequence[ComplianceObservationSummary] = field(default_factory=tuple)
    results: Sequence[ComplianceRuleResult] = field(default_factory=tuple)
    results_total: int = 0
    schema_version: int = SCHEMA_VERSIONS[COMPLIANCE_REPORT]
    kind: str = COMPLIANCE_REPORT

    def __post_init__(self) -> None:
        _require_iso_timestamp(self.generated_at, "generated_at")
        _require_str(self.policy_id, "policy_id")
        if len(self.policy_id) > MAX_COMPLIANCE_VALUE_CHARS:
            raise ArtifactValidationError(
                f"policy_id exceeds the {MAX_COMPLIANCE_VALUE_CHARS}-character bound"
            )
        _require_bool(self.compliant, "compliant")
        counts = _require_counts_mapping(self.counts, "counts")
        expected_compliant = counts["fail"] == 0 and counts["error"] == 0
        if self.compliant != expected_compliant:
            raise ArtifactValidationError(
                "compliant must be False whenever counts.fail or counts.error is nonzero "
                "(never success-shaped)"
            )
        observations = _require_sequence(
            self.observations, "observations", max_items=MAX_COMPLIANCE_OBSERVATIONS
        )
        if not observations:
            raise ArtifactValidationError("observations must contain at least one entry")
        for observation in observations:
            if not isinstance(observation, ComplianceObservationSummary):
                raise ArtifactValidationError(
                    "observations must be ComplianceObservationSummary instances"
                )
        results = _require_sequence(self.results, "results", max_items=MAX_COMPLIANCE_RESULTS)
        for result in results:
            if not isinstance(result, ComplianceRuleResult):
                raise ArtifactValidationError("results must be ComplianceRuleResult instances")
        _require_int(self.results_total, "results_total", minimum=0)
        if self.results_total < len(results):
            raise ArtifactValidationError(
                "results_total must be >= len(results); the detail list may be capped "
                "below the true total, never the other way around"
            )
        total_from_counts = sum(counts[key] for key in COMPLIANCE_RULE_STATUSES)
        if total_from_counts != self.results_total:
            raise ArtifactValidationError(
                "counts must sum to results_total (pass+fail+error+skipped)"
            )
        _check_schema_kind(self, COMPLIANCE_REPORT)


# ---------------------------------------------------------------------------
# Dict -> contract builders (used by build_artifact/write_artifact, and
# directly by tests/callers that only have plain JSON-shaped dicts).
# ---------------------------------------------------------------------------


def _build_entries(
    entry_cls: type,
    raw_entries: Any,
    field_name: str,
    max_items: int,
) -> tuple[Any, ...]:
    if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
        raise ArtifactValidationError(f"{field_name} must be a list")
    if len(raw_entries) > max_items:
        raise ArtifactValidationError(
            f"{field_name} has {len(raw_entries)} items, exceeding the bound of {max_items}"
        )
    built: list[Any] = []
    for index, item in enumerate(raw_entries):
        if isinstance(item, entry_cls):
            built.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ArtifactValidationError(f"{field_name}[{index}] must be an object")
        try:
            built.append(entry_cls(**item))
        except TypeError as exc:
            raise ArtifactValidationError(f"{field_name}[{index}] is malformed: {exc}") from exc
    return tuple(built)


def _build_live_lifecycle_evidence(payload: dict[str, Any]) -> LiveLifecycleEvidence:
    return LiveLifecycleEvidence(**payload)


def _build_platform_compatibility_matrix(payload: dict[str, Any]) -> PlatformCompatibilityMatrix:
    raw_entries = payload.pop("entries", ())
    entries = _build_entries(
        PlatformCompatibilityEntry, raw_entries, "entries", MAX_MATRIX_PLATFORMS
    )
    return PlatformCompatibilityMatrix(entries=entries, **payload)


def _build_migration_report_metadata(payload: dict[str, Any]) -> MigrationReportMetadata:
    return MigrationReportMetadata(**payload)


def _build_capability_snapshot(payload: dict[str, Any]) -> CapabilitySnapshot:
    raw_platforms = payload.pop("platforms", ())
    platforms = _build_entries(
        PlatformCapabilityCount, raw_platforms, "platforms", MAX_SNAPSHOT_PLATFORMS
    )
    return CapabilitySnapshot(platforms=platforms, **payload)


def _build_source_freshness_snapshot(payload: dict[str, Any]) -> SourceFreshnessSnapshot:
    raw_entries = payload.pop("entries", ())
    entries = _build_entries(
        SourceFreshnessEntry, raw_entries, "entries", MAX_FRESHNESS_DETAILS
    )
    return SourceFreshnessSnapshot(entries=entries, **payload)


def _build_release_artifact_manifest(payload: dict[str, Any]) -> ReleaseArtifactManifest:
    raw_entries = payload.pop("entries", ())
    entries = _build_entries(ManifestEntry, raw_entries, "entries", MAX_MANIFEST_ENTRIES)
    return ReleaseArtifactManifest(entries=entries, **payload)


def _build_router_dependency_plan(payload: dict[str, Any]) -> RouterDependencyPlan:
    raw_steps = payload.pop("steps", ())
    steps = _build_entries(RouterPlanStep, raw_steps, "steps", MAX_ROUTER_PLAN_STEPS)
    return RouterDependencyPlan(steps=steps, **payload)


def _build_router_reconciliation_plan(payload: dict[str, Any]) -> RouterReconciliationPlan:
    raw_entries = payload.pop("entries", ())
    entries = _build_entries(
        ReconciliationEntry, raw_entries, "entries", MAX_ROUTER_RECONCILIATION_ENTRIES
    )
    return RouterReconciliationPlan(entries=entries, **payload)


def _build_validation_matrix(payload: dict[str, Any]) -> ValidationMatrix:
    raw_entries = payload.pop("entries", ())
    entries = _build_entries(
        ValidationMatrixEntry, raw_entries, "entries", MAX_VALIDATION_MATRIX_ENTRIES
    )
    return ValidationMatrix(entries=entries, **payload)


def _build_compliance_report(payload: dict[str, Any]) -> ComplianceReport:
    raw_observations = payload.pop("observations", ())
    observations = _build_entries(
        ComplianceObservationSummary,
        raw_observations,
        "observations",
        MAX_COMPLIANCE_OBSERVATIONS,
    )
    raw_results = payload.pop("results", ())
    results = _build_entries(
        ComplianceRuleResult, raw_results, "results", MAX_COMPLIANCE_RESULTS
    )
    return ComplianceReport(observations=observations, results=results, **payload)


_BUILDERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    LIVE_LIFECYCLE_EVIDENCE: _build_live_lifecycle_evidence,
    PLATFORM_COMPATIBILITY_RESULT: _build_platform_compatibility_matrix,
    MIGRATION_REPORT_METADATA: _build_migration_report_metadata,
    CAPABILITY_SNAPSHOT: _build_capability_snapshot,
    SOURCE_FRESHNESS_RESULT: _build_source_freshness_snapshot,
    RELEASE_ARTIFACT_MANIFEST: _build_release_artifact_manifest,
    ROUTER_DEPENDENCY_PLAN: _build_router_dependency_plan,
    ROUTER_RECONCILIATION_PLAN: _build_router_reconciliation_plan,
    VALIDATION_MATRIX_RESULT: _build_validation_matrix,
    COMPLIANCE_REPORT: _build_compliance_report,
}


def build_artifact(kind: str, payload: Mapping[str, Any]) -> Any:
    """Validate a plain JSON-shaped mapping into its typed artifact contract.

    Args:
        kind: one of :data:`ARTIFACT_KINDS`.
        payload: a plain mapping (e.g. parsed JSON). Nested collection
            entries (``entries``/``platforms``) may be plain dicts or
            already-built entry dataclasses.

    Raises:
        ArtifactValidationError: ``kind`` is unknown, ``payload`` is not a
            mapping, or any required field/type/bound check fails.
    """
    if kind not in _BUILDERS:
        raise ArtifactValidationError(
            f"unknown artifact kind {kind!r}; expected one of {ARTIFACT_KINDS}"
        )
    if not isinstance(payload, Mapping):
        raise ArtifactValidationError("artifact payload must be an object/mapping")
    try:
        return _BUILDERS[kind](dict(payload))
    except ArtifactValidationError:
        raise
    except TypeError as exc:
        raise ArtifactValidationError(f"{kind} payload is malformed: {exc}") from exc


def to_json_dict(artifact: Any) -> dict[str, Any]:
    """Convert a built contract dataclass into a JSON-serializable dict."""
    if not is_dataclass(artifact):
        raise ArtifactValidationError("artifact must be a contract dataclass instance")
    payload = asdict(artifact)
    try:
        json.dumps(payload)
    except TypeError as exc:
        raise ArtifactValidationError(f"artifact payload is not JSON serializable: {exc}") from exc
    return payload


# ---------------------------------------------------------------------------
# Deterministic JSON, digest, and atomic write
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize ``payload`` deterministically (sorted keys, fixed spacing)."""
    try:
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    except TypeError as exc:
        raise ArtifactValidationError(f"artifact payload is not JSON serializable: {exc}") from exc
    return (text + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_artifact(
    path: str | Path,
    kind: str,
    payload: Mapping[str, Any],
    *,
    redact: bool = True,
    known_sensitive_values: Sequence[str] = (),
) -> ManifestEntry:
    """Validate, redact, and atomically write one artifact; return its manifest entry.

    Args:
        path: destination file path. Written via a same-directory temp file
            plus ``os.replace`` so a reader never observes a partial file.
        kind: one of :data:`ARTIFACT_KINDS`.
        payload: a plain JSON-shaped mapping, or the ``to_json_dict()``
            output of an already-built contract dataclass.
        redact: when True (default), recursively strip secrets and
            tenant/workspace/account/scope identifiers
            (:func:`redact_artifact_payload`) before writing. Only disable
            this for a payload that is already known-redacted -- never to
            work around a validation failure.
        known_sensitive_values: bounded raw values known by the caller
            (credentials, tenant names/IDs, workspace IDs, and similar)
            that must also be removed wherever they appear inside free-text
            fields such as reasons, errors, details, or summaries.

    Side effects:
        Writes (or overwrites, atomically) the file at ``path``.
    """
    artifact = build_artifact(kind, payload)
    body = to_json_dict(artifact)
    if redact:
        body = redact_artifact_payload(
            body,
            known_sensitive_values=known_sensitive_values,
        )
        body = to_json_dict(build_artifact(kind, body))
    data = canonical_json_bytes(body)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError(
            f"artifact for {kind!r} is {len(data)} bytes, exceeding the "
            f"{MAX_ARTIFACT_BYTES}-byte safety ceiling"
        )
    destination = Path(path)
    _atomic_write_bytes(destination, data)
    return ManifestEntry(
        filename=destination.name,
        kind=kind,
        schema_version=int(body.get("schema_version", SCHEMA_VERSIONS[kind])),
        size_bytes=len(data),
        sha256=sha256_hex(data),
        generated_at=_now_iso(),
        redacted=bool(redact),
    )
