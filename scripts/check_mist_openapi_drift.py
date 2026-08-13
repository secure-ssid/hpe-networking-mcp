#!/usr/bin/env python3
"""Classified drift gate for the official Mist OpenAPI pin.

The old version asked GitHub one question ("has the newest commit touching
``mist.openapi.json`` moved?") and mapped *every* other outcome -- rate
limit, DNS failure, malformed JSON, empty response -- onto the same exit
code 1 as a real upstream change. It also had no notion of a *reviewed*
pin: a pin nobody has re-verified looked identical to a verified-fresh one.

This version reads the reviewed-pin record at
``ingestion/provenance/mist_openapi_pin.json``, cross-checks it against the
source-of-truth constants in ``ingestion/fetch_mist_openapi.py``, and emits
one classified finding per concern
(``hpe_networking_mcp.pipeline.drift_taxonomy``):

``reviewed_pin``
    ``fresh`` when the pin is marked reviewed and upstream's latest commit
    for the pinned path still equals ``reviewed_ref``; ``stale_pin`` when
    upstream has advanced past the pin, when the pin is marked
    ``review_needed``, or when refresh is disabled so the pin could not be
    re-verified; ``unavailable`` on a transport failure; ``parser_error``
    on a malformed GitHub response; ``source_removed`` when the pinned
    repository/path is gone.
``pinned_ref_content``
    Only with ``--verify-pinned-digest``: re-hashes the blob at the pinned
    ref. A mismatch is ``content_drift`` (the pinned ref's content itself
    changed, e.g. a force-push) -- the pin is no longer reproducible.
``local_spec_digest``
    The on-disk ``ingestion/sources/openapi_specs/mist-openapi.json``
    (git-ignored) hashed against ``reviewed_sha256``: ``fresh`` on match,
    ``content_drift`` on mismatch, ``not_checked`` when absent.

The pin is never advanced by this script. ``refresh_policy: frozen`` in the
pin record is an explicit instruction: report honestly, let a human review
and commit the new ref/digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from ingestion.fetch_mist_openapi import (  # noqa: E402
    DEFAULT_OUTPUT,
    DEFAULT_PATH,
    DEFAULT_REF,
    DEFAULT_SHA256,
    REPOSITORY,
)

CHECK_NAME = "mist_openapi_drift"
DEFAULT_PIN_PATH = _REPO_ROOT / "ingestion" / "provenance" / "mist_openapi_pin.json"
DEFAULT_ARTIFACT_PATH = _REPO_ROOT / "outputs" / "drift" / "mist-openapi-drift.json"
GITHUB_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
USER_AGENT = "hpe-networking-mcp-openapi-drift"
DEFAULT_TIMEOUT = 30.0

REVIEW_STATUSES = ("reviewed", "review_needed")


class PinError(Exception):
    """The reviewed-pin record is missing, malformed, or inconsistent."""


class FetchError(Exception):
    """GitHub could not be queried (transport failure)."""


class RemoteParseError(Exception):
    """GitHub answered, but with something this check cannot read."""


class RemoteMissingError(Exception):
    """The pinned repository/path no longer exists (404/410)."""


def load_pin(path: Path = DEFAULT_PIN_PATH) -> dict[str, Any]:
    """Load and validate the reviewed-pin record against the module constants."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PinError(f"cannot read reviewed pin record {path}: {exc}") from exc
    try:
        pin = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PinError(f"reviewed pin record is not valid JSON: {path}") from exc
    if not isinstance(pin, dict):
        raise PinError(f"reviewed pin record is not an object: {path}")

    required = ("repository", "path", "reviewed_ref", "reviewed_sha256", "review_status")
    missing = [key for key in required if not pin.get(key)]
    if missing:
        raise PinError(f"reviewed pin record missing {', '.join(missing)}: {path}")
    if pin["review_status"] not in REVIEW_STATUSES:
        raise PinError(
            f"unknown review_status {pin['review_status']!r} "
            f"(expected one of {', '.join(REVIEW_STATUSES)})"
        )

    # The generator module stays the source of truth for what is actually
    # fetched; a disagreement means the check cannot conclude anything.
    mismatches = []
    for key, expected in (
        ("repository", REPOSITORY),
        ("path", DEFAULT_PATH),
        ("reviewed_ref", DEFAULT_REF),
        ("reviewed_sha256", DEFAULT_SHA256),
    ):
        if pin[key] != expected:
            mismatches.append(f"{key}: pin={pin[key]!r} module={expected!r}")
    if mismatches:
        raise PinError(
            "reviewed pin record disagrees with ingestion/fetch_mist_openapi.py: "
            + "; ".join(mismatches)
        )
    return pin


def _get(url: str, *, timeout: float, accept: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise RemoteMissingError(f"HTTP {exc.code} for {url}") from exc
        raise FetchError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"network error for {url}: {exc}") from exc


def fetch_latest_ref(pin: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return the newest commit sha that touched the pinned path."""
    query = urllib.parse.urlencode({"path": pin["path"], "per_page": 1})
    payload = _get(
        f"{GITHUB_API_BASE}/repos/{pin['repository']}/commits?{query}",
        timeout=timeout,
        accept="application/vnd.github+json",
    )
    try:
        commits = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RemoteParseError("GitHub returned a non-JSON commits response") from exc
    if not isinstance(commits, list) or not commits:
        raise RemoteParseError(
            f"no commits returned for {pin['repository']}/{pin['path']}"
        )
    sha = commits[0].get("sha") if isinstance(commits[0], dict) else None
    if not isinstance(sha, str) or not sha:
        raise RemoteParseError("GitHub commit entry has no sha")
    return sha


def fetch_pinned_digest(pin: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return the sha256 of the blob at the pinned ref (reproducibility check)."""
    url = f"{RAW_BASE}/{pin['repository']}/{pin['reviewed_ref']}/{pin['path']}"
    return hashlib.sha256(_get(url, timeout=timeout, accept="*/*")).hexdigest()


def evaluate_reviewed_pin(
    pin: dict[str, Any],
    *,
    offline: bool,
    timeout: float = DEFAULT_TIMEOUT,
) -> taxonomy.Finding:
    frozen = pin.get("refresh_policy") == "frozen"
    short = pin["reviewed_ref"][:12]
    if pin["review_status"] == "review_needed":
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.STALE_PIN,
            detail=(
                f"pin {short} is marked review_needed"
                + (" and refresh_policy=frozen" if frozen else "")
                + "; it has not been re-verified upstream, so it is reported "
                "stale/review-needed rather than fresh"
            ),
            evidence={
                "repository": pin["repository"],
                "path": pin["path"],
                "reviewed_ref": pin["reviewed_ref"],
                "review_status": pin["review_status"],
                "refresh_policy": pin.get("refresh_policy"),
                "reviewed_at": pin.get("reviewed_at"),
            },
        )
    if offline:
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.STALE_PIN,
            detail=(
                f"offline run: pin {short} could not be re-verified against "
                f"{pin['repository']}; an unverified pin is never reported fresh"
            ),
            evidence={"reviewed_ref": pin["reviewed_ref"], "offline": True},
        )
    try:
        latest = fetch_latest_ref(pin, timeout=timeout)
    except RemoteMissingError as exc:
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.SOURCE_REMOVED,
            detail=f"pinned repository/path is gone: {exc}",
        )
    except RemoteParseError as exc:
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.PARSER_ERROR,
            detail=str(exc),
        )
    except FetchError as exc:
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.UNAVAILABLE,
            detail=f"{exc} (transport failure, not drift)",
        )
    if latest == pin["reviewed_ref"]:
        return taxonomy.Finding(
            target="reviewed_pin",
            result_class=taxonomy.FRESH,
            detail=f"reviewed pin {short} is still the newest commit for {pin['path']}",
            evidence={"latest_ref": latest},
        )
    return taxonomy.Finding(
        target="reviewed_pin",
        result_class=taxonomy.STALE_PIN,
        detail=(
            f"upstream advanced: reviewed pin {short}, latest {latest[:12]}. "
            "Review the new spec, then update ingestion/fetch_mist_openapi.py "
            "and this pin record together."
        ),
        evidence={"reviewed_ref": pin["reviewed_ref"], "latest_ref": latest},
    )


def evaluate_pinned_ref_content(
    pin: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT
) -> taxonomy.Finding:
    try:
        digest = fetch_pinned_digest(pin, timeout=timeout)
    except RemoteMissingError as exc:
        return taxonomy.Finding(
            target="pinned_ref_content",
            result_class=taxonomy.SOURCE_REMOVED,
            detail=str(exc),
        )
    except FetchError as exc:
        return taxonomy.Finding(
            target="pinned_ref_content",
            result_class=taxonomy.UNAVAILABLE,
            detail=str(exc),
        )
    if digest == pin["reviewed_sha256"]:
        return taxonomy.Finding(
            target="pinned_ref_content",
            result_class=taxonomy.FRESH,
            detail="blob at the pinned ref still hashes to reviewed_sha256",
        )
    return taxonomy.Finding(
        target="pinned_ref_content",
        result_class=taxonomy.CONTENT_DRIFT,
        detail=(
            f"blob at pinned ref hashes {digest[:12]}, reviewed_sha256 is "
            f"{pin['reviewed_sha256'][:12]} -- the pin is no longer reproducible"
        ),
        evidence={"observed_sha256": digest},
    )


def evaluate_local_spec(pin: dict[str, Any], *, spec_path: Path) -> taxonomy.Finding:
    if not spec_path.is_file():
        return taxonomy.Finding(
            target="local_spec_digest",
            result_class=taxonomy.NOT_CHECKED,
            detail=(
                f"{spec_path.name} is absent (git-ignored build artifact); run "
                "ingestion/fetch_mist_openapi.py to materialize it"
            ),
        )
    digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    if digest == pin["reviewed_sha256"]:
        return taxonomy.Finding(
            target="local_spec_digest",
            result_class=taxonomy.FRESH,
            detail="on-disk spec matches reviewed_sha256",
        )
    return taxonomy.Finding(
        target="local_spec_digest",
        result_class=taxonomy.CONTENT_DRIFT,
        detail=(
            f"on-disk spec hashes {digest[:12]} but reviewed_sha256 is "
            f"{pin['reviewed_sha256'][:12]}"
        ),
        evidence={"observed_sha256": digest, "path": str(spec_path)},
    )


def evaluate(
    *,
    pin_path: Path = DEFAULT_PIN_PATH,
    spec_path: Path = DEFAULT_OUTPUT,
    offline: bool = False,
    verify_pinned_digest: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[taxonomy.Finding]:
    try:
        pin = load_pin(pin_path)
    except PinError as exc:
        return [
            taxonomy.Finding(
                target="reviewed_pin",
                result_class=taxonomy.PARSER_ERROR,
                detail=str(exc),
            )
        ]
    findings = [evaluate_reviewed_pin(pin, offline=offline, timeout=timeout)]
    if verify_pinned_digest and not offline:
        findings.append(evaluate_pinned_ref_content(pin, timeout=timeout))
    findings.append(evaluate_local_spec(pin, spec_path=spec_path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN_PATH)
    parser.add_argument("--spec-path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Never contact GitHub; an unverified pin is reported stale, never fresh.",
    )
    parser.add_argument(
        "--verify-pinned-digest",
        action="store_true",
        help="Also re-hash the blob at the pinned ref (catches a force-pushed ref).",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    taxonomy.add_common_arguments(parser, default_artifact=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args(argv)

    findings = evaluate(
        pin_path=args.pin,
        spec_path=args.spec_path,
        offline=args.offline,
        verify_pinned_digest=args.verify_pinned_digest,
        timeout=args.timeout,
    )
    report = taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=not args.offline,
        exit_code_mode=args.exit_code_mode,
        notes=(
            "Reviewed pin is never advanced by this check; update "
            "ingestion/fetch_mist_openapi.py and "
            "ingestion/provenance/mist_openapi_pin.json together after review."
        ),
    )
    taxonomy.print_report(report)
    if not args.no_artifact and args.json_artifact:
        print(f"wrote {taxonomy.write_report(args.json_artifact, report)}")
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
