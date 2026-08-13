#!/usr/bin/env python3
"""Report whether pinned nowireless4u/hpe-networking-mcp inputs have advanced.

hpe-networking-mcp treats the MIT-licensed community project
``nowireless4u/hpe-networking-mcp`` as a set of reviewed *benchmark/input*
pins -- never as API authority -- for three unrelated purposes:

- the vendored GreenLake OpenAPI specs used by
  ``scripts/generate_glp_tools.py`` (``vendor/greenlake``);
- the Axis Atmos Cloud platform source reviewed by
  ``scripts/generate_axis_manifest.py``
  (``src/hpe_networking_mcp/platforms/axis``); and
- the capability-benchmark counts recorded in
  ``docs/capability-benchmark-snapshot.json``, reproduced from
  ``README.md`` and ``scripts/build_spec_index.py`` at the pinned commit.

Each of those pins is a single whole-tree commit SHA covering the entire
reviewed upstream repository, not a per-path commit. That whole-tree SHA is
almost never itself the last commit that touched any one watched path, so it
must never be compared directly against a path-scoped "latest commit" query
-- doing so reports false drift on every run. Instead, for each watched
path this script derives two *path-scoped* commits from GitHub's commits API
(``per_page=1``):

- **baseline** -- the latest commit that touched the path as of the
  reviewed pin (``sha=<pin>&path=<path>``), i.e. what the path looked like
  when the pin was reviewed; and
- **latest** -- the latest commit that touched the path on the upstream
  default branch (``path=<path>``, no ``sha``), i.e. what the path looks
  like now.

The path is CURRENT when ``baseline == latest`` (nothing has touched the
path since the reviewed pin) and DRIFTED when they differ (something has
touched the path after the reviewed pin). This script re-derives the pins
from their existing source-of-truth modules/files (it holds no copy of its
own), never fetches raw source contents, and never writes to any file.

Exit codes:
    0  every watched path is current (baseline == latest).
    1  drift detected, a GitHub fetch failed or returned malformed data,
       the pins are internally inconsistent, or there are no watched
       inputs at all.

Refresh guidance when drift is reported: review the changed path(s)
upstream, then regenerate the affected artifact(s) with
``scripts/generate_glp_tools.py``, ``scripts/generate_axis_manifest.py``,
or ``scripts/report_capability_gaps.py`` (updating
``docs/capability-benchmark-snapshot.json``) as applicable, and only then
advance the reviewed pin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402
from scripts import generate_axis_manifest as _axis_gen  # noqa: E402
from scripts import generate_glp_tools as _glp_gen  # noqa: E402

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "hpe-networking-mcp-nowireless-source-drift"
DEFAULT_TIMEOUT = 20.0
DEFAULT_BENCHMARK_PATH = _REPO_ROOT / "docs" / "capability-benchmark-snapshot.json"

STATUS_CURRENT = "current"
STATUS_DRIFT = "drift"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

CHECK_NAME = "nowireless_community_input_drift"
DEFAULT_ARTIFACT_PATH = (
    _REPO_ROOT / "outputs" / "drift" / "nowireless-community-input-drift.json"
)

# This whole check watches *community* benchmark/input pins. It is never a
# statement about HPE's official GreenLake API surface -- see
# OFFICIAL_GLP_REGISTRY_BOUNDARY below, which is emitted as its own
# machine-readable coverage_gap finding so "community input is fresh" can
# never be read as "the HPE GLP API was checked".
INPUT_AUTHORITY = "community_input_pin"

OFFICIAL_GLP_REGISTRY_BOUNDARY: dict[str, Any] = {
    "boundary_id": "official_hpe_glp_openapi_registry",
    "authority": "official_hpe_greenlake",
    "state": "no_official_machine_readable_registry_tracked",
    "reason": (
        "HPE does not publish a public, reproducible machine-readable "
        "OpenAPI registry/index for the GreenLake Platform APIs that this "
        "repo could pin and diff the way the Aruba developer portal's ReadMe "
        "api-registry is pinned in ingestion/openapi_registry_manifest.json."
    ),
    "consequence": (
        "GLP tool generation is driven by the reviewed community-vendored "
        "specs under nowireless4u/hpe-networking-mcp vendor/greenlake. Their "
        "freshness says nothing about HPE's own API surface: a GLP API change "
        "that upstream has not vendored yet is invisible to this check."
    ),
    "evidence": [
        "scripts/generate_glp_tools.py pins UPSTREAM_REPO/UPSTREAM_REF/VENDOR_DIR "
        "(a community repository), not an HPE-published registry document.",
        "ingestion/openapi_registry_manifest.json covers developer.arubanetworks.com "
        "ReadMe registries only; it holds no GreenLake registry entry.",
        "ingestion/source_manifest.json declares no official GLP OpenAPI source.",
    ],
    "remediation": (
        "If HPE publishes a reproducible GLP OpenAPI registry/index, add it as "
        "a first-class source (ingestion/source_manifest.json + a registry "
        "manifest) and demote this boundary to a real drift check."
    ),
}

_RESULT_CLASS_BY_STATUS = {
    STATUS_CURRENT: taxonomy.FRESH,
    STATUS_DRIFT: taxonomy.CONTENT_DRIFT,
    STATUS_ERROR: taxonomy.UNAVAILABLE,
    STATUS_SKIPPED: taxonomy.NOT_CHECKED,
}

# Bound how many labels/entries are ever rendered in one line, defense in
# depth against an upstream metadata file ballooning source_evidence.
_MAX_LABELS_SHOWN = 6
_SHA_SHOWN = 12

_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_OWNER_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class DriftConfigError(Exception):
    """Pinned metadata is missing, malformed, or internally inconsistent."""


class DriftFetchError(Exception):
    """GitHub could not be queried, or returned unusable data.

    Subclassed so a permanent removal (404/410) and a malformed response can
    be classified apart from a transient transport failure without breaking
    callers that catch the base class.
    """


class DriftMissingError(DriftFetchError):
    """The watched repository/path no longer exists upstream (404/410)."""


class DriftParseError(DriftFetchError):
    """GitHub answered, but with a body this check cannot read."""


def normalize_repo(value: str) -> str:
    """Normalize a repository identifier to ``owner/repo``.

    Accepts either a bare ``owner/repo`` string or a ``github.com`` URL
    (with or without scheme, trailing slash, or ``.git`` suffix).
    """
    text = (value or "").strip()
    match = _REPO_URL_RE.match(text)
    if match:
        owner, repo = match.groups()
        return f"{owner}/{repo}"
    if _OWNER_REPO_RE.match(text):
        return text
    raise DriftConfigError(f"cannot normalize repository identifier: {value!r}")


def load_glp_pin() -> dict[str, str]:
    """Return the GLP vendored-spec pin from scripts/generate_glp_tools.py."""
    return {
        "label": "glp_vendor_specs",
        "repo": normalize_repo(_glp_gen.UPSTREAM_REPO),
        "ref": _glp_gen.UPSTREAM_REF,
        "path": _glp_gen.VENDOR_DIR,
    }


def load_axis_pin() -> dict[str, str]:
    """Return the Axis platform-source pin from scripts/generate_axis_manifest.py."""
    return {
        "label": "axis_platform_source",
        "repo": normalize_repo(_axis_gen.REPOSITORY),
        "ref": _axis_gen.COMMIT,
        "path": _axis_gen.AXIS_ROOT,
    }


def load_capability_benchmark_pins(
    snapshot_path: Path = DEFAULT_BENCHMARK_PATH,
) -> list[dict[str, str]]:
    """Return the capability-benchmark path pins from the committed snapshot JSON."""
    try:
        raw = snapshot_path.read_text()
    except OSError as exc:
        raise DriftConfigError(
            f"cannot read capability benchmark snapshot at {snapshot_path}: {exc}"
        ) from exc
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DriftConfigError(
            f"capability benchmark snapshot is not valid JSON: {snapshot_path}"
        ) from exc

    repo_field = data.get("repository")
    commit = data.get("commit")
    if not repo_field or not commit:
        raise DriftConfigError(
            f"capability benchmark snapshot missing repository/commit: {snapshot_path}"
        )
    normalized_repo = normalize_repo(repo_field)

    repo_url_field = data.get("repository_url")
    if repo_url_field:
        normalized_repo_url = normalize_repo(repo_url_field)
        if normalized_repo_url != normalized_repo:
            raise DriftConfigError(
                "capability benchmark snapshot repository/repository_url mismatch: "
                f"{repo_field!r} vs {repo_url_field!r}"
            )

    paths: set[str] = set()
    for evidence in data.get("source_evidence", []) or []:
        path = evidence.get("path") if isinstance(evidence, dict) else None
        if path:
            paths.add(path)
    reproduction = data.get("indexed_endpoint_reproduction") or {}
    script_path = reproduction.get("source_script") if isinstance(reproduction, dict) else None
    if script_path:
        paths.add(script_path)

    if not paths:
        raise DriftConfigError(
            f"capability benchmark snapshot has no watched source paths: {snapshot_path}"
        )

    return [
        {
            "label": f"capability_benchmark:{path}",
            "repo": normalized_repo,
            "ref": commit,
            "path": path,
        }
        for path in sorted(paths)
    ]


def dedupe_inputs(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Collapse entries sharing the same (repo, ref, path) into one record."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for entry in entries:
        key = (entry["repo"], entry["ref"], entry["path"])
        if key not in merged:
            merged[key] = {
                "repo": entry["repo"],
                "ref": entry["ref"],
                "path": entry["path"],
                "labels": [entry["label"]],
            }
            order.append(key)
        else:
            merged[key]["labels"].append(entry["label"])
    return [merged[key] for key in order]


def collect_watched_inputs(
    *, benchmark_path: Path = DEFAULT_BENCHMARK_PATH
) -> list[dict[str, Any]]:
    """Gather, validate, and deduplicate all watched path-specific pins.

    Raises DriftConfigError if any source module's pin cannot be
    normalized, or if the pinned repositories disagree with each other
    (all three sources are expected to reference the same reviewed
    upstream tree).
    """
    entries: list[dict[str, str]] = [load_glp_pin(), load_axis_pin()]
    entries.extend(load_capability_benchmark_pins(benchmark_path))

    repos = {entry["repo"] for entry in entries}
    if len(repos) > 1:
        detail = ", ".join(f"{entry['label']}={entry['repo']}" for entry in entries)
        raise DriftConfigError(
            f"inconsistent pinned repository across source-of-truth modules: {detail}"
        )

    return dedupe_inputs(entries)


def fetch_path_commit(
    repo: str,
    path: str,
    *,
    sha: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Return the SHA of the most recent commit that touched ``path`` in ``repo``.

    Args:
        repo: normalized ``owner/repo`` identifier.
        path: repo-relative path to scope the commits query to.
        sha: optional starting point (branch, tag, or commit SHA) to scope
            the search to history reachable from that ref. Pass the
            reviewed whole-tree pin to get the *baseline* path commit "as
            of" that review point. Omit (default) to search the upstream
            default branch, giving the *latest* path commit. Never compare
            the whole-tree pin itself against a path-scoped commit --
            always fetch both a baseline and a latest path commit and
            compare those.
        token: optional GitHub token sent as ``Authorization: Bearer`` --
            never logged or included in any returned/raised value.
        timeout: finite socket timeout in seconds.
    """
    params: dict[str, str | int] = {}
    if sha:
        params["sha"] = sha
    params["path"] = path
    params["per_page"] = 1
    query = urllib.parse.urlencode(params)
    url = f"{GITHUB_API_BASE}/repos/{repo}/commits?{query}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise DriftMissingError(
                f"HTTP {exc.code} fetching {repo}@{path}: path is gone upstream"
            ) from exc
        raise DriftFetchError(f"HTTP {exc.code} fetching {repo}@{path}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise DriftFetchError(f"network error fetching {repo}@{path}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DriftFetchError(f"timed out fetching {repo}@{path}") from exc

    try:
        commits = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DriftParseError(f"malformed (non-JSON) response for {repo}@{path}") from exc

    if not isinstance(commits, list) or not commits:
        raise DriftParseError(f"no commits returned for {repo}@{path}")

    latest = commits[0]
    found_sha = latest.get("sha") if isinstance(latest, dict) else None
    if not found_sha or not isinstance(found_sha, str):
        raise DriftParseError(f"malformed commit entry for {repo}@{path} (missing sha)")
    return found_sha


def _classify_fetch_error(exc: DriftFetchError, phase: str) -> tuple[str, str]:
    """Map a fetch failure to (result_class, detail) -- never to drift."""
    if isinstance(exc, DriftMissingError):
        return taxonomy.SOURCE_REMOVED, f"{phase} fetch: {exc}"
    if isinstance(exc, DriftParseError):
        return taxonomy.PARSER_ERROR, f"{phase} fetch returned unusable data: {exc}"
    return taxonomy.UNAVAILABLE, f"{phase} fetch failed: {exc} (transport, not drift)"


def evaluate_input(
    entry: dict[str, Any],
    *,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    offline: bool = False,
) -> dict[str, Any]:
    """Compare a watched path's baseline (as of the reviewed pin) and latest commits.

    Fetches two path-scoped commits -- never compares the whole-tree pin
    directly against a path commit. ``baseline`` is the latest commit that
    touched the path as of the reviewed pin (``sha=<pin>``); ``latest`` is
    the latest commit that touched the path on the default branch.

    Legacy ``status`` stays ``current``/``drift``/``error`` for existing
    callers; ``result_class`` is the shared taxonomy class:

    * ``fresh`` -- baseline == latest.
    * ``content_drift`` -- baseline != latest (the path really changed).
    * ``source_removed`` -- the watched path 404s upstream.
    * ``parser_error`` -- GitHub answered with unusable data.
    * ``unavailable`` -- transport failure/rate limit (fail-closed, not drift).
    * ``not_checked`` -- ``offline=True``; nothing fetched, nothing claimed.
    """
    result: dict[str, Any] = {
        "repo": entry["repo"],
        "path": entry["path"],
        "pin": entry["ref"],
        "labels": entry["labels"],
        "authority": INPUT_AUTHORITY,
        "baseline": None,
        "latest": None,
    }

    if offline:
        result["status"] = STATUS_SKIPPED
        result["result_class"] = taxonomy.NOT_CHECKED
        result["detail"] = (
            "offline run: community input pin not re-verified upstream "
            "(no freshness claimed)"
        )
        return result

    try:
        baseline = fetch_path_commit(
            entry["repo"], entry["path"], sha=entry["ref"], token=token, timeout=timeout
        )
    except DriftFetchError as exc:
        result_class, detail = _classify_fetch_error(exc, "baseline")
        result["status"] = STATUS_ERROR
        result["result_class"] = result_class
        result["detail"] = detail
        return result
    result["baseline"] = baseline

    try:
        latest = fetch_path_commit(entry["repo"], entry["path"], token=token, timeout=timeout)
    except DriftFetchError as exc:
        result_class, detail = _classify_fetch_error(exc, "latest")
        result["status"] = STATUS_ERROR
        result["result_class"] = result_class
        result["detail"] = detail
        return result
    result["latest"] = latest

    if baseline == latest:
        result["status"] = STATUS_CURRENT
        result["result_class"] = taxonomy.FRESH
        result["detail"] = "no path changes since reviewed pin"
    else:
        result["status"] = STATUS_DRIFT
        result["result_class"] = taxonomy.CONTENT_DRIFT
        result["detail"] = "path changed upstream since reviewed pin"
    return result


def evaluate_official_glp_boundary() -> dict[str, Any]:
    """Return the standalone official-HPE-GLP coverage-gap record.

    This is deliberately a *separate* machine-readable finding, not a
    modifier on the community pins: community-input freshness must never be
    rendered as "the official HPE GreenLake API was checked". It is always
    ``coverage_gap`` -- an expected, documented boundary that does not fail
    the gate on its own.
    """
    boundary = OFFICIAL_GLP_REGISTRY_BOUNDARY
    return {
        "repo": None,
        "path": boundary["boundary_id"],
        "pin": None,
        "labels": ["official_glp_registry_boundary"],
        "authority": boundary["authority"],
        "baseline": None,
        "latest": None,
        "status": taxonomy.COVERAGE_GAP,
        "result_class": taxonomy.COVERAGE_GAP,
        "detail": f"{boundary['state']}: {boundary['reason']}",
        "boundary": boundary,
    }


def build_findings(results: list[dict[str, Any]]) -> list[taxonomy.Finding]:
    """Convert evaluation records into shared-taxonomy findings."""
    findings: list[taxonomy.Finding] = []
    for result in results:
        result_class = result.get("result_class") or _RESULT_CLASS_BY_STATUS.get(
            result.get("status", ""), taxonomy.UNAVAILABLE
        )
        target = (
            f"{result['repo']}@{result['path']}" if result.get("repo") else result["path"]
        )
        evidence = {
            "labels": result.get("labels") or [],
            "authority": result.get("authority"),
            "pin": result.get("pin"),
            "baseline": result.get("baseline"),
            "latest": result.get("latest"),
        }
        if result.get("boundary"):
            evidence["boundary_id"] = result["boundary"]["boundary_id"]
            evidence["boundary_state"] = result["boundary"]["state"]
            evidence["boundary_evidence"] = result["boundary"]["evidence"]
            evidence["boundary_remediation"] = result["boundary"]["remediation"]
        findings.append(
            taxonomy.Finding(
                target=target,
                result_class=result_class,
                detail=result.get("detail", ""),
                legacy_status=result.get("status"),
                evidence=evidence,
            )
        )
    return findings


def format_result_line(result: dict[str, Any]) -> str:
    """Render one bounded, compact status line for a watched path."""
    labels = result["labels"][:_MAX_LABELS_SHOWN]
    labels_str = ",".join(labels)
    if len(result["labels"]) > _MAX_LABELS_SHOWN:
        labels_str += ",..."
    status = str(result.get("result_class") or result["status"]).upper()
    pin = (result.get("pin") or "-")[:_SHA_SHOWN]
    baseline = result["baseline"][:_SHA_SHOWN] if result.get("baseline") else "?"
    latest = result["latest"][:_SHA_SHOWN] if result.get("latest") else "?"
    location = f"{result['repo']}@{result['path']}" if result.get("repo") else result["path"]
    return (
        f"  {status:14s} {location} ({labels_str}) "
        f"pin={pin} baseline={baseline} latest={latest}: {result['detail']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-path",
        type=Path,
        default=DEFAULT_BENCHMARK_PATH,
        help="path to docs/capability-benchmark-snapshot.json (for testing)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="per-request GitHub API timeout in seconds",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Do not contact GitHub: report every community pin as not_checked. "
            "Never reports a pin as fresh."
        ),
    )
    taxonomy.add_common_arguments(parser, default_artifact=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args(argv)

    # Never logged: only ever placed into a request header, never printed.
    token = os.environ.get("GITHUB_TOKEN") or None

    try:
        entries = collect_watched_inputs(benchmark_path=args.benchmark_path)
    except DriftConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return taxonomy.EXIT_USAGE

    if not entries:
        print(
            "No watched nowireless4u/hpe-networking-mcp inputs configured.",
            file=sys.stderr,
        )
        return taxonomy.EXIT_USAGE

    print(f"Checking {len(entries)} pinned nowireless4u/hpe-networking-mcp path(s) for drift...")
    results = [
        evaluate_input(entry, token=token, timeout=args.timeout, offline=args.offline)
        for entry in entries
    ]
    boundary_result = evaluate_official_glp_boundary()
    for result in results:
        print(format_result_line(result))
    print(format_result_line(boundary_result))

    current = [r for r in results if r["status"] == STATUS_CURRENT]
    drifted = [r for r in results if r["status"] == STATUS_DRIFT]
    errored = [r for r in results if r["status"] == STATUS_ERROR]

    skipped = [r for r in results if r["status"] == STATUS_SKIPPED]
    print(
        f"\n{len(current)} current, {len(drifted)} drifted, {len(errored)} fetch errors"
        + (f", {len(skipped)} not checked (offline)." if skipped else ".")
    )

    findings = build_findings([*results, boundary_result])
    report = taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=not args.offline,
        exit_code_mode=args.exit_code_mode,
        notes=(
            "Community-input freshness only. These are reviewed benchmark/input "
            "pins from an MIT-licensed community repository, never HPE API "
            "authority; the official HPE GreenLake registry boundary is reported "
            "separately as coverage_gap."
        ),
        extra={"official_glp_registry_boundary": OFFICIAL_GLP_REGISTRY_BOUNDARY},
    )
    if not args.no_artifact and args.json_artifact:
        print(f"wrote {taxonomy.write_report(args.json_artifact, report)}")

    incomplete = [
        r for r in results if r.get("result_class") in (taxonomy.UNAVAILABLE, taxonomy.PARSER_ERROR)
    ]
    removed = [r for r in results if r.get("result_class") == taxonomy.SOURCE_REMOVED]
    if incomplete:
        print(
            "\nSome paths could not be checked: GitHub fetch failed or returned "
            "malformed data (rate limiting, network issues, or a bad/expired "
            "GITHUB_TOKEN are common causes). This is a configuration/fetch "
            "failure, not confirmed upstream drift -- fix access and rerun."
        )
    if removed:
        print(
            "\nSome watched paths no longer exist upstream (404). The community "
            "input moved or was deleted: re-review the upstream layout before "
            "regenerating anything."
        )
    if drifted:
        print(
            "\nDrift detected in reviewed nowireless4u/hpe-networking-mcp inputs. These "
            "are community benchmark/input pins, not API authority: review the changed "
            "path(s) upstream, then regenerate the affected artifact(s) with "
            "scripts/generate_glp_tools.py, scripts/generate_axis_manifest.py, or "
            "scripts/report_capability_gaps.py (docs/capability-benchmark-snapshot.json) "
            "as applicable before advancing the reviewed pin."
        )
    print(
        "\nOfficial HPE GreenLake registry boundary: "
        f"{OFFICIAL_GLP_REGISTRY_BOUNDARY['state']} (coverage_gap). Community input "
        "freshness is NOT HPE GLP API freshness."
    )

    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
