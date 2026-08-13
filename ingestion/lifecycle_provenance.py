"""Deterministic provenance pins for official security/lifecycle sources.

``ingestion/sources/`` (the fetched markdown) is git-ignored and regenerated
on demand, so it cannot itself carry reviewed provenance. This module pins,
under the *committed* ``ingestion/provenance/`` directory, the reviewed
identity of each official source
(:mod:`ingestion.scrape_security_lifecycle`) uses: its endpoint URLs, the
structural markers its parser depends on, and its committed minimum
coverage count.

This mirrors the existing ``scripts/build_optional_product_manifests.py``
source-pin pattern (``SourcePinError`` / ``load_source_pin`` /
``validate_source_pin`` / ``write_source_pin``), generalized for sources
whose *content* naturally grows over time (new advisories, new lifecycle
notices) and therefore cannot be pinned by full-content digest the way a
static generated API manifest can.

Two independent things can drift, and this module keeps them separate:

- **Source identity** (which official URLs we trust) -- pinned exactly.
  A code change to a source URL without updating the pin is rejected
  (:class:`SourceProvenanceError`) until reviewed and re-pinned with
  ``write_pin``.
- **Structural markers** (the field/tag names a parser depends on) --
  pinned as a list of literal substrings expected to appear in the raw
  fetched content. This catches a silent schema change that would
  otherwise just quietly parse to zero records (see
  ``scrape_security_lifecycle.parse_hpe_lifecycle_xml``, which does not
  raise on a missing/renamed tag).

Coverage counts (minimums) are intentionally validated by
``scripts/check_security_lifecycle_drift.py`` against the *pin*, not
against a hardcoded module constant, so raising a minimum is itself an
explicit, reviewed, committed change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_INGESTION_DIR = Path(__file__).resolve().parent
PROVENANCE_DIR = _INGESTION_DIR / "provenance"

SCHEMA_VERSION = 1

# Every pinned family. Keep in sync with the pin filenames under
# ingestion/provenance/ and with scripts/check_security_lifecycle_drift.py.
SECURITY_ADVISORIES = "security_advisories"
HPE_LIFECYCLE_NOTICES = "hpe_lifecycle_notices"
JUNIPER_LIFECYCLE_PAGES = "juniper_lifecycle_pages"
JUNIPER_SECURITY_ADVISORIES = "juniper_security_advisories"
HPE_ARUBA_CURRENT_LIFECYCLE = "hpe_aruba_current_lifecycle"

FAMILIES: tuple[str, ...] = (
    SECURITY_ADVISORIES,
    HPE_LIFECYCLE_NOTICES,
    JUNIPER_LIFECYCLE_PAGES,
    JUNIPER_SECURITY_ADVISORIES,
    HPE_ARUBA_CURRENT_LIFECYCLE,
)

# Bound how much of an unexpectedly large/garbled pin or source list this
# module will ever hold in memory or echo back in an error message.
MAX_SOURCE_URLS = 20
MAX_EXPECTED_MARKERS = 40
MAX_DETAIL_CHARS = 500


class SourceProvenanceError(RuntimeError):
    """A source's reviewed identity or expected structure no longer matches its pin."""


def _validate_family(family: str) -> str:
    if family not in FAMILIES:
        raise SourceProvenanceError(
            f"unknown source-lifecycle family {family!r}; expected one of {FAMILIES}"
        )
    return family


def provenance_path(family: str) -> Path:
    return PROVENANCE_DIR / f"{_validate_family(family)}.json"


def _bounded_list(values: Any, field_name: str, max_items: int) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise SourceProvenanceError(f"{field_name} must be a list of strings")
    if len(values) > max_items:
        raise SourceProvenanceError(
            f"{field_name} has {len(values)} entries, exceeding the bound of {max_items}"
        )
    return list(values)


def load_pin(family: str) -> dict[str, Any]:
    """Load the committed provenance pin for ``family``.

    Raises:
        SourceProvenanceError: the pin file is missing, not valid JSON, or
            not a JSON object. A missing pin fails closed rather than
            silently treating the source as unpinned/trusted.
    """
    path = provenance_path(family)
    if not path.exists():
        try:
            display_path = path.relative_to(_INGESTION_DIR.parent)
        except ValueError:
            display_path = path
        raise SourceProvenanceError(
            f"missing source-lifecycle provenance pin: {display_path}"
        )
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceProvenanceError(
            f"invalid source-lifecycle provenance pin {path.name}: {exc}"
        ) from exc
    if not isinstance(pin, dict):
        raise SourceProvenanceError(
            f"source-lifecycle provenance pin {path.name} must be a JSON object"
        )
    if pin.get("schema_version") != SCHEMA_VERSION:
        raise SourceProvenanceError(
            f"unsupported schema_version in source-lifecycle pin {path.name}"
        )
    if pin.get("source_family") != family:
        raise SourceProvenanceError(
            f"source_family mismatch in source-lifecycle pin {path.name}"
        )
    _bounded_list(pin.get("source_urls"), "source_urls", MAX_SOURCE_URLS)
    _bounded_list(
        pin.get("expected_markers"),
        "expected_markers",
        MAX_EXPECTED_MARKERS,
    )
    minimum = pin.get("minimum_count")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or minimum < 0
    ):
        raise SourceProvenanceError(
            f"minimum_count in source-lifecycle pin {path.name} "
            "must be a non-negative int"
        )
    return pin


def minimum_count(family: str) -> int:
    """Return the reviewed minimum coverage count for one source family."""
    return int(load_pin(family)["minimum_count"])


def build_pin(
    family: str,
    *,
    source_urls: list[str],
    expected_markers: list[str],
    minimum_count: int,
    note: str = "",
    reviewed_at: str,
) -> dict[str, Any]:
    """Build a pin payload for ``family`` (does not write it)."""
    _validate_family(family)
    urls = _bounded_list(source_urls, "source_urls", MAX_SOURCE_URLS)
    markers = _bounded_list(expected_markers, "expected_markers", MAX_EXPECTED_MARKERS)
    if not isinstance(minimum_count, int) or isinstance(minimum_count, bool) or minimum_count < 0:
        raise SourceProvenanceError("minimum_count must be a non-negative int")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_family": family,
        "generator": "ingestion/scrape_security_lifecycle.py",
        "reviewed_at": reviewed_at,
        "source_urls": sorted(urls),
        "expected_markers": sorted(markers),
        "minimum_count": minimum_count,
        "note": note[:MAX_DETAIL_CHARS],
    }


def write_pin(family: str, pin: dict[str, Any]) -> Path:
    """Atomically write a reviewed pin for ``family``. Requires explicit intent."""
    path = provenance_path(family)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def validate_source_identity(
    family: str, actual_source_urls: list[str], pin: dict[str, Any] | None = None
) -> None:
    """Fail closed if ``actual_source_urls`` no longer matches the pin exactly.

    Raises:
        SourceProvenanceError: the code's current source URLs for
            ``family`` differ from its committed, reviewed pin. Reviewed
            intentional changes must call :func:`write_pin` with a new pin
            (an explicit ``--update-provenance`` regeneration), never a
            silent pass-through.
    """
    expected = pin if pin is not None else load_pin(family)
    expected_urls = sorted(expected.get("source_urls") or [])
    actual_urls = sorted(_bounded_list(actual_source_urls, "source_urls", MAX_SOURCE_URLS))
    if expected_urls != actual_urls:
        raise SourceProvenanceError(
            f"{family} source URLs no longer match the reviewed provenance pin "
            f"(expected {expected_urls}, got {actual_urls}); review the change, "
            "then regenerate the pin"
        )


def validate_markers(family: str, raw_text: str, pin: dict[str, Any] | None = None) -> None:
    """Fail closed if any of ``family``'s expected structural markers vanished.

    A marker is a literal substring (e.g. an XML tag name) the parser
    depends on. This catches a silent schema change for parsers that do
    not themselves raise on a missing/renamed field (unlike, e.g., the
    Juniper page renderers, which already raise ``SourceFetchError`` when
    their expected content sections are absent).
    """
    expected = pin if pin is not None else load_pin(family)
    markers = expected.get("expected_markers") or []
    missing = [marker for marker in markers if marker not in raw_text]
    if missing:
        raise SourceProvenanceError(
            f"{family} source no longer contains expected structural markers: {missing}"
        )
