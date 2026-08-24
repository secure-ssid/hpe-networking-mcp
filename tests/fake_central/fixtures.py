"""Fixture bundle loading for the fake Central API.

Bundles are directories under ``tests/fake_central/fixtures/`` containing:

- ``env.yaml`` — OAuth client settings accepted by the fake token endpoint.
- ``collections.yaml`` — the deterministic dataset, keyed by collection name.

Bundles are hermetic: nothing is fetched, generated, or time-dependent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

DEFAULT_FIXTURE_STEM = "central/site-a"


@dataclass(frozen=True)
class FixtureBundle:
    """A loaded hermetic fixture bundle."""

    root: Path
    env: dict[str, Any] = field(default_factory=dict)
    collections: dict[str, list[Any]] = field(default_factory=dict)

    def items(self, collection: str) -> list[Any]:
        return self.collections.get(collection, [])


def load_bundle(root: str | Path) -> FixtureBundle:
    root = Path(root)
    env_path = root / "env.yaml"
    collections_path = root / "collections.yaml"
    env: dict[str, Any] = {}
    collections: dict[str, list[Any]] = {}
    if env_path.exists():
        env = yaml.safe_load(env_path.read_text(encoding="utf-8")) or {}
    if collections_path.exists():
        collections = yaml.safe_load(collections_path.read_text(encoding="utf-8")) or {}
    return FixtureBundle(root=root, env=env, collections=collections)


def default_bundle() -> FixtureBundle:
    """The golden site-a bundle used by every scenario by default."""
    return load_bundle(FIXTURES_ROOT / DEFAULT_FIXTURE_STEM)
