"""Shared helpers for the Aruba developer portal's ReadMe SuperHub API registry.

Aruba's developer portal (developer.arubanetworks.com) migrated to ReadMe's
SuperHub platform in July 2026. That migration killed two ingestion paths
this repo relied on:

- ``internal-ui.central.arubanetworks.com/cnxconfig/docs/<slug>.json`` (used
  by the old ``scrape_openapi.py``) no longer resolves.
- The per-page embedded ``"oasDefinition": {...}`` blob (used by the old
  ``scrape_cnac_spec.py``) is gone from the rendered page HTML.

What replaced it: each reference page now embeds a compact pointer instead
of the full spec::

    "oasPublicUrl":"@aruba-new-central-config/v26.04#efby2pmq0s5oms"
                     ^project slug        ^version   ^registry id

The registry id resolves the *complete* OpenAPI document for that page's
category (ReadMe groups operations into "x-tag-group" categories server
side -- one registry id typically covers many reference pages) via::

    GET https://dash.readme.com/api/v1/api-registry/{registry_id}

This module centralizes: parsing that pointer out of a reference page's
HTML, fetching the registry document, and building/comparing a manifest
(source URL, project, version, sha256, fetched-at) so both ingestion
scripts and a CI drift check share one implementation instead of three
copies of a regex.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PAGE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
REGISTRY_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
REGISTRY_BASE_URL = "https://dash.readme.com/api/v1/api-registry"

# "oasPublicUrl":"@<project>/<version>#<registry_id>"
_OAS_PUBLIC_URL_RE = re.compile(r'"oasPublicUrl"\s*:\s*"@([^/"]+)/([^#"]+)#([A-Za-z0-9]+)"')


class RegistryFetchError(RuntimeError):
    """A reference page or registry document could not be fetched/parsed.

    Deliberately narrow and always raised with an actionable message --
    the July 2026 migration proved these ingestion paths go stale without
    warning, so failures here should be loud, not swallowed.

    Subclassed (not replaced) so existing callers that catch
    ``RegistryFetchError`` keep working while drift gates can tell a
    transient transport failure from a permanent removal from a
    parse/layout break -- three states that must never share one result
    class (see ``hpe_networking_mcp.pipeline.drift_taxonomy``).
    """


class RegistryUnavailableError(RegistryFetchError):
    """Transient/blocked transport failure: timeout, DNS, 429, 5xx, 403."""


class RegistryMissingError(RegistryFetchError):
    """The page or registry document is permanently gone (404/410)."""


class RegistryParseError(RegistryFetchError):
    """Content was retrieved but could not be parsed/extracted."""


#: HTTP statuses that mean "gone", not "try again later".
_MISSING_STATUSES = frozenset({404, 410})

_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "remote end closed",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
)


def classify_transport_error(exc: Exception, url: str) -> RegistryFetchError:
    """Wrap a urllib/OS error as the narrowest matching registry error."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _MISSING_STATUSES:
        return RegistryMissingError(f"{url} is gone (HTTP {code})")
    return RegistryUnavailableError(f"failed to fetch {url}: {exc}")


@dataclass(frozen=True)
class OasPointer:
    """A parsed ``oasPublicUrl`` pointer."""

    project: str
    version: str
    registry_id: str

    @property
    def registry_url(self) -> str:
        return f"{REGISTRY_BASE_URL}/{self.registry_id}"


def fetch_page_html(url: str, *, timeout: float = 30.0) -> str:
    """Fetch a developer-portal reference page's rendered HTML."""
    req = urllib.request.Request(url, headers=PAGE_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
            return resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise classify_transport_error(exc, f"reference page {url}") from exc


def extract_oas_pointer(html: str) -> OasPointer:
    """Parse the ``oasPublicUrl`` pointer embedded in a SuperHub page.

    Raises:
        RegistryFetchError: no pointer found -- e.g. the page layout has
            changed again, or a stale pre-migration page was fetched by
            mistake (this is intentionally NOT silently ignored: a missing
            pointer here means ingestion output would otherwise go stale
            without anyone noticing).
    """
    match = _OAS_PUBLIC_URL_RE.search(html)
    if not match:
        raise RegistryParseError(
            "no oasPublicUrl pointer found in page HTML -- the ReadMe "
            "SuperHub page layout may have changed again, or this page "
            "predates/postdates the July 2026 migration format"
        )
    project, version, registry_id = match.groups()
    return OasPointer(project=project, version=version, registry_id=registry_id)


def fetch_registry_spec(pointer: OasPointer, *, timeout: float = 60.0) -> dict[str, Any]:
    """Fetch the full OpenAPI document for ``pointer`` from the API registry."""
    req = urllib.request.Request(pointer.registry_url, headers=REGISTRY_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise classify_transport_error(exc, f"api-registry {pointer.registry_id}") from exc
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryParseError(
            f"api-registry {pointer.registry_id} did not return valid JSON: {exc}"
        ) from exc
    if not isinstance(spec, dict) or "paths" not in spec:
        raise RegistryParseError(
            f"api-registry {pointer.registry_id} response is missing a 'paths' object"
        )
    return spec


def fetch_spec_for_page(
    url: str, *, page_timeout: float = 30.0, registry_timeout: float = 60.0
) -> tuple[OasPointer, dict[str, Any]]:
    """Fetch ``url``'s reference page, then resolve and fetch its registry spec."""
    html = fetch_page_html(url, timeout=page_timeout)
    pointer = extract_oas_pointer(html)
    spec = fetch_registry_spec(pointer, timeout=registry_timeout)
    return pointer, spec


def spec_fingerprint(spec: dict[str, Any]) -> str:
    """Stable sha256 over ``spec``'s content (key-sorted) for drift detection."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def registry_slug(pointer: OasPointer, spec: dict[str, Any]) -> str:
    """Human-readable, filesystem-safe name for a registry's spec file.

    Prefers the spec's own title (its "x-tag-group" category name);
    falls back to the registry id if the title is missing or empty. The
    registry id suffix keeps filenames unique even if two categories
    happen to share a title across projects.
    """
    title = str((spec.get("info") or {}).get("title") or "").strip()
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") if title else ""
    suffix = pointer.registry_id[:10]
    return f"{base}-{suffix}" if base else suffix


# ---------------------------------------------------------------------------
# Manifest: source URL / project / version / hash / fetched-at bookkeeping
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the registry manifest, tolerating a missing or corrupt file."""
    if not path.exists():
        return {"generated_at": None, "registries": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"generated_at": None, "registries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("registries"), dict):
        return {"generated_at": None, "registries": {}}
    return data


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def build_registry_entry(
    pointer: OasPointer,
    spec: dict[str, Any],
    *,
    source_url: str,
    output_path: str,
) -> dict[str, Any]:
    """Build one manifest entry for a fetched registry spec."""
    info = spec.get("info") if isinstance(spec, dict) else None
    info = info if isinstance(info, dict) else {}
    return {
        "registry_id": pointer.registry_id,
        "project": pointer.project,
        "portal_version": pointer.version,
        "source_url": source_url,
        "output_path": output_path,
        "sha256": spec_fingerprint(spec),
        "title": info.get("title"),
        "spec_version": info.get("version"),
        "path_count": len(spec.get("paths", {})) if isinstance(spec.get("paths"), dict) else 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def upsert_registry_entry(manifest: dict[str, Any], entry: dict[str, Any]) -> None:
    """Insert/replace ``entry`` in ``manifest`` keyed by registry id."""
    manifest.setdefault("registries", {})[entry["registry_id"]] = entry
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Drift detection (see scripts/check_openapi_drift.py for the CI entry point)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftResult:
    """One registry entry's drift verdict.

    ``status`` keeps the original three-value vocabulary
    (``unchanged``/``changed``/``fetch_failed``) for existing callers;
    ``result_class`` is the shared taxonomy class
    (``hpe_networking_mcp.pipeline.drift_taxonomy``) that CI gates and JSON
    artifacts key off, so a 503 can never be summarized as content drift.
    """

    registry_id: str
    source_url: str
    status: str  # "unchanged" | "changed" | "fetch_failed" | "skipped"
    detail: str
    result_class: str = taxonomy.FRESH
    observed_registry_id: str | None = None
    observed_sha256: str | None = None


_LEGACY_STATUS_BY_CLASS = {
    taxonomy.FRESH: "unchanged",
    taxonomy.CONTENT_DRIFT: "changed",
    taxonomy.POINTER_CHANGE: "changed",
    taxonomy.SOURCE_REMOVED: "fetch_failed",
    taxonomy.SOURCE_ADDED: "changed",
    taxonomy.UNAVAILABLE: "fetch_failed",
    taxonomy.PARSER_ERROR: "fetch_failed",
    taxonomy.NOT_CHECKED: "skipped",
}


def _result(
    registry_id: str,
    source_url: str,
    result_class: str,
    detail: str,
    **evidence: Any,
) -> DriftResult:
    return DriftResult(
        registry_id=registry_id,
        source_url=source_url,
        status=_LEGACY_STATUS_BY_CLASS[result_class],
        detail=detail,
        result_class=result_class,
        observed_registry_id=evidence.get("observed_registry_id"),
        observed_sha256=evidence.get("observed_sha256"),
    )


def check_entry_drift(entry: dict[str, Any], *, offline: bool = False) -> DriftResult:
    """Re-fetch one manifest entry's source page and classify the difference.

    Never raises -- every failure is returned as a classified
    :class:`DriftResult` so a CI job can iterate a whole manifest and report
    every finding instead of stopping at the first one.

    Classification:

    * ``fresh`` -- the page still points at the recorded registry id and the
      registry document's sha256 matches the manifest.
    * ``pointer_change`` -- the page now points at a *different* registry
      id: a portal/layout move, not a proven content change.
    * ``content_drift`` -- same pointer, different spec sha256.
    * ``source_removed`` -- the reference page or registry document is gone
      (404/410).
    * ``unavailable`` -- transient/blocked transport failure after retries.
    * ``parser_error`` -- fetched, but the pointer/JSON could not be parsed.
    * ``not_checked`` -- ``offline=True``; nothing was fetched.

    Args:
        entry: one manifest registry entry.
        offline: skip all network access and report ``not_checked``. Used by
            tests and by runs where external source refresh is disabled.
    """
    registry_id = entry.get("registry_id", "?")
    source_url = entry.get("source_url", "")
    if offline:
        return _result(
            registry_id,
            source_url,
            taxonomy.NOT_CHECKED,
            "offline mode: registry not re-fetched, manifest hash not re-verified",
        )
    if not source_url:
        return _result(
            registry_id,
            source_url,
            taxonomy.PARSER_ERROR,
            "manifest entry has no source_url to re-resolve",
        )

    pointer = spec = None
    parse_error_detail = ""
    for attempt in range(3):
        try:
            pointer, spec = fetch_spec_for_page(source_url)
            break
        except RegistryParseError as exc:
            parse_error_detail = str(exc)
            manifest_pointer = OasPointer(
                project=str(entry.get("project") or ""),
                version=str(entry.get("portal_version") or ""),
                registry_id=str(registry_id),
            )
            try:
                spec = fetch_registry_spec(manifest_pointer)
                pointer = manifest_pointer
                break
            except RegistryParseError as registry_exc:
                return _result(
                    registry_id,
                    source_url,
                    taxonomy.PARSER_ERROR,
                    f"{parse_error_detail}; manifest registry also failed: {registry_exc}",
                )
            except RegistryMissingError as registry_exc:
                return _result(
                    registry_id,
                    source_url,
                    taxonomy.SOURCE_REMOVED,
                    f"{parse_error_detail}; manifest registry is gone: {registry_exc}",
                )
            except RegistryFetchError as registry_exc:
                return _result(
                    registry_id,
                    source_url,
                    taxonomy.UNAVAILABLE,
                    f"{parse_error_detail}; manifest registry could not be fetched: {registry_exc}",
                )
        except RegistryMissingError as exc:
            return _result(registry_id, source_url, taxonomy.SOURCE_REMOVED, str(exc))
        except RegistryFetchError as exc:
            # RegistryUnavailableError plus any bare RegistryFetchError a
            # caller/monkeypatch still raises: retry only on a transient
            # marker, then classify as unavailable -- never as drift.
            detail = str(exc)
            transient = any(marker in detail.lower() for marker in _TRANSIENT_MARKERS)
            if not transient or attempt == 2:
                return _result(registry_id, source_url, taxonomy.UNAVAILABLE, detail)
            time.sleep(0.25 * (attempt + 1))

    if pointer is None or spec is None:  # pragma: no cover - defensive
        return _result(
            registry_id, source_url, taxonomy.UNAVAILABLE, "no response after retries"
        )

    if pointer.registry_id != registry_id:
        return _result(
            registry_id,
            source_url,
            taxonomy.POINTER_CHANGE,
            f"page now points at a different registry id: {pointer.registry_id}",
            observed_registry_id=pointer.registry_id,
        )

    new_hash = spec_fingerprint(spec)
    old_hash = entry.get("sha256")
    if new_hash != old_hash:
        return _result(
            registry_id,
            source_url,
            taxonomy.CONTENT_DRIFT,
            f"sha256 {old_hash} -> {new_hash}",
            observed_registry_id=pointer.registry_id,
            observed_sha256=new_hash,
        )
    return _result(
        registry_id,
        source_url,
        taxonomy.FRESH,
        (
            "sha256 matches manifest"
            if not parse_error_detail
            else f"sha256 matches manifest via direct api-registry fetch ({parse_error_detail})"
        ),
        observed_registry_id=pointer.registry_id,
        observed_sha256=new_hash,
    )
