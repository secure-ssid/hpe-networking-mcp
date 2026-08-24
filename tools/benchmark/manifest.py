"""Golden-scenario manifest: schema, strict validation, and loading.

The **loader contract** the benchmark harness consumes verbatim. Scout
(repo-deconstructor) produces a manifest in exactly this shape —
``RESEARCH/CENTRAL_BENCH_SCENARIOS.md`` targeting ``tools/benchmark``
consumers — and this module turns it into scenario objects with zero
guesswork on either side.

A scenario is: a task intent, the Central API calls a correct solution must
produce (ground-truth wire invariant), the expected tool per compared repo,
the safety assertions (``must_not_call``) the run must uphold, the write-gate
mechanism under test per repo, and coverage markers (``absent``) that are a
measured head-to-head signal rather than a harness error (OX-Lead
ratification rule 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ManifestError, ManifestNotFoundError

# Intent families from the golden manifest (hyphenated, as published).
INTENT_FAMILIES = frozenset(
    {
        "client-diagnosis",
        "site-device-health",
        "wlan-change",
        "nac",
        "firmware",
        "config-compliance",
        "alerts",
        "incident-response",
        "adversarial",
    }
)

VALID_ALLOWED_TOOLS = frozenset({"read", "write", "destructive"})

# The repo key head-to-head runs score against. The team's own repo is the
# PR-gate target; the manifest is neutral (OX-Lead ratification) so the key
# here is also usable as a default.
DEFAULT_REPO_KEY = "secure_ssid"


@dataclass(frozen=True)
class Scenario:
    """One golden scenario, exactly as Scout emits it."""

    id: str
    suite: str
    intent: str
    platform: str
    allowed_tools: frozenset[str]
    fixture: str
    fixture_entities: tuple[str, ...]
    api_calls: tuple[str, ...]
    expected_tools: dict[str, tuple[str, ...]]
    must_not_call: tuple[str, ...]
    write_gate: dict[str, str]
    coverage: dict[str, str]
    evidence: dict[str, Any]

    def tools_for(self, repo: str) -> tuple[str, ...]:
        return self.expected_tools.get(repo, ())

    def is_absent(self, repo: str) -> bool:
        return self.coverage.get(repo, "") == "absent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "suite": self.suite,
            "intent": self.intent,
            "platform": self.platform,
            "allowed_tools": sorted(self.allowed_tools),
            "fixture": self.fixture,
            "fixture_entities": list(self.fixture_entities),
            "api_calls": list(self.api_calls),
            "expected_tools": {k: list(v) for k, v in self.expected_tools.items()},
            "must_not_call": list(self.must_not_call),
            "write_gate": self.write_gate,
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class Manifest:
    """A parsed and validated golden-scenario manifest."""

    manifest_version: int
    generated_by: str
    pinned: dict[str, str]
    fixture_default: str
    scenarios: list[Scenario]

    @property
    def scenario_ids(self) -> list[str]:
        return [s.id for s in self.scenarios]

    def by_id(self, scenario_id: str) -> Scenario | None:
        for s in self.scenarios:
            if s.id == scenario_id:
                return s
        return None


def _require(raw: dict[str, Any], key: str, errors: list[str]) -> Any:
    if key not in raw:
        errors.append(f"missing required field {key!r}")
        return None
    return raw[key]


def _require_kind(value: Any, expected: type, key: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, expected):
        errors.append(f"{key!r} must be {expected.__name__}, got {type(value).__name__}")


def _str_list(value: Any, key: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{key!r} must be a list of strings")
        return ()
    return tuple(value)


def _repo_map(value: Any, key: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{key!r} must be an object keyed by repo")
        return {}
    return value


def _parse_scenario(raw: Any, errors: list[str]) -> Scenario | None:
    if not isinstance(raw, dict):
        errors.append("scenario must be an object")
        return None
    sid = raw.get("id") or "<no-id>"
    for key, expected in (("id", str), ("suite", str), ("intent", str), ("platform", str)):
        _require_kind(raw.get(key), expected, f"{sid}.{key}", errors)
    if not raw.get("id"):
        errors.append("scenario missing 'id'")
        return None
    suite = raw.get("suite")
    if suite not in INTENT_FAMILIES:
        errors.append(f"{sid}: suite {suite!r} not in {sorted(INTENT_FAMILIES)}")
        return None
    if not raw.get("intent"):
        errors.append(f"{sid}: scenario missing 'intent'")
        return None
    platform = raw.get("platform", "central")

    allowed = raw.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        errors.append(f"{sid}: 'allowed_tools' must be a non-empty list")
        return None
    unknown = set(allowed) - VALID_ALLOWED_TOOLS
    if unknown:
        errors.append(f"{sid}: allowed_tools contains invalid values {sorted(unknown)}")
        return None

    fixture = raw.get("fixture") or ""
    expect = raw.get("expect") if isinstance(raw.get("expect"), dict) else {}
    api_calls = _str_list(expect.get("api_calls"), f"{sid}.expect.api_calls", errors)
    must_not_call = _str_list(expect.get("must_not_call"), f"{sid}.expect.must_not_call", errors)
    tools_raw = _repo_map(expect.get("tools"), f"{sid}.expect.tools", errors)
    expected_tools: dict[str, tuple[str, ...]] = {}
    for repo, tools in tools_raw.items():
        if not isinstance(repo, str):
            errors.append(f"{sid}.expect.tools: repo key must be a string")
            continue
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            errors.append(f"{sid}.expect.tools.{repo}: must be a list of tool names")
            continue
        expected_tools[repo] = tuple(tools)

    write_gate = _repo_map(raw.get("write_gate"), f"{sid}.write_gate", errors)
    bad_gate = {k for k, v in write_gate.items() if not isinstance(v, str)}
    for repo in bad_gate:
        errors.append(f"{sid}.write_gate.{repo}: must be a string")
        del write_gate[repo]

    coverage = _repo_map(raw.get("coverage"), f"{sid}.coverage", errors)
    bad_cov = {k for k, v in coverage.items() if v != "absent"}
    for repo in bad_cov:
        errors.append(f"{sid}.coverage.{repo}: only 'absent' is a supported marker")
        del coverage[repo]

    entities = _str_list(raw.get("fixture_entities", []), f"{sid}.fixture_entities", errors)
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}

    if errors:
        return None
    return Scenario(
        id=raw["id"],
        suite=suite,
        intent=raw["intent"],
        platform=platform,
        allowed_tools=frozenset(allowed),
        fixture=fixture,
        fixture_entities=entities,
        api_calls=api_calls,
        expected_tools=expected_tools,
        must_not_call=must_not_call,
        write_gate=write_gate,
        coverage=coverage,
        evidence=evidence,
    )


def load_manifest(path: str | Path) -> Manifest:
    """Load and strictly validate a manifest file (YAML/JSON or Markdown).

    Markdown is accepted so the single source of truth
    (``RESEARCH/CENTRAL_BENCH_SCENARIOS.md``) can be consumed verbatim: the
    first fenced ``yaml`` code block carries the manifest.

    Raises :class:`ManifestNotFoundError` when the file is absent and
    :class:`ManifestError` listing every schema violation when invalid.
    """
    p = Path(path)
    if not p.exists():
        raise ManifestNotFoundError(str(p))
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".md", ".markdown"):
        fence = text.split("```yaml", 1)
        text = fence[1].split("```", 1)[0] if len(fence) == 2 else ""
        if not text.strip():
            raise ManifestError(f"{p}: markdown contains no fenced ```yaml block")
    try:
        raw = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{p}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{p}: manifest must be an object")

    errors: list[str] = []
    version = raw.get("manifest_version")
    if version != 1:
        errors.append(f"manifest_version must be 1, got {version!r}")
    scenarios_raw = raw.get("scenarios")
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        errors.append("'scenarios' must be a non-empty list")
    scenarios = [_parse_scenario(s, errors) for s in scenarios_raw or []]
    scenarios = [s for s in scenarios if s is not None]

    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            errors.append(f"duplicate scenario id {s.id!r}")
        seen.add(s.id)

    pinned = raw.get("pinned")
    if not isinstance(pinned, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in pinned.items()
    ):
        errors.append("'pinned' must be an object mapping repo keys to SHAs")
    fixture_default = raw.get("fixture_default")
    if not isinstance(fixture_default, str) or not fixture_default:
        errors.append("'fixture_default' must be a non-empty string")

    if errors:
        raise ManifestError(
            f"{p}: {len(errors)} schema violation(s)\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return Manifest(
        manifest_version=version,
        generated_by=str(raw.get("generated_by", "")),
        pinned=pinned,
        fixture_default=fixture_default,
        scenarios=scenarios,
    )
