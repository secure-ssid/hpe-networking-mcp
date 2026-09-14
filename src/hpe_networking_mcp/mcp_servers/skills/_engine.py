"""Skill engine — load markdown runbooks and expose browse/load helpers.

A *skill* is a markdown file with YAML frontmatter in this package directory.
Frontmatter carries metadata (name / title / description / platforms / tags /
tools); the body is the runbook the model follows.

Inspired by the MIT-licensed nowireless4u/hpe-networking-mcp skills engine,
adapted for hpe-networking-mcp tool naming and the find_tool → invoke_read_tool flow.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SKILLS_DIR = Path(__file__).parent
_RESERVED_FILENAMES = frozenset({"TEMPLATE.md", "README.md"})

_MATCH_STOPWORDS = frozenset(
    {
        "the",
        "for",
        "all",
        "and",
        "with",
        "from",
        "this",
        "that",
        "any",
        "are",
        "what",
        "which",
        "how",
        "please",
        "need",
        "give",
        "get",
        "show",
        "list",
        "find",
        "to",
        "of",
        "is",
        "in",
        "on",
        "or",
        "no",
        "my",
        "we",
        "it",
        "be",
        "as",
        "at",
        "by",
        "do",
        "an",
    }
)

_LIST_NEXT_STEP_MATCHED = (
    "This is skill METADATA only — not the runbook. If any skill matches the "
    "request, call `load_skill(name=...)` (see each entry's `load_with`) for "
    "the full step-by-step body, then follow it. Do not improvise from metadata."
)
_LIST_NEXT_STEP_EMPTY = "No skills matched these filters. Proceed with find_tool / platform tools."
_FIND_NEXT_STEP_EMPTY = (
    "No skill matched that request. Call `list_skills()` to browse what is "
    "available, or go straight to `find_tool` for a single-step answer."
)


@dataclass(frozen=True)
class Skill:
    """A loaded skill — frontmatter metadata plus markdown body."""

    name: str
    title: str
    description: str
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    body: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "platforms": list(self.platforms),
            "tags": list(self.tags),
            "tools": list(self.tools),
        }

    def to_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "platforms": list(self.platforms),
            "tags": list(self.tags),
        }


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value if item)
    return ()


def _parse_skill(path: Path) -> Skill | None:
    """Parse one ``.md`` file into a ``Skill``, or ``None`` if malformed."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    try:
        _, frontmatter_block, body = text.split("---", 2)
    except ValueError:
        return None
    try:
        meta = yaml.safe_load(frontmatter_block) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None

    name = meta.get("name")
    title = meta.get("title")
    description = meta.get("description")
    if not isinstance(name, str) or not name:
        return None
    if name != path.stem:
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None

    return Skill(
        name=name,
        title=title.strip(),
        description=description.strip(),
        platforms=_as_str_tuple(meta.get("platforms")),
        tags=_as_str_tuple(meta.get("tags")),
        tools=_as_str_tuple(meta.get("tools")),
        body=body.lstrip("\n"),
    )


def _coerce_filter(value: str | list[str] | None) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def _match_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in text.replace("-", " ").replace("_", " ").replace("/", " ").split():
        tok = "".join(ch for ch in raw.lower() if ch.isalnum())
        if len(tok) >= 2 and tok not in _MATCH_STOPWORDS:
            out.add(tok)
            # Operators type "clients keep dropping" while the runbook is
            # tagged "client". Emitting a crude singular alongside the token
            # (on both the query and the searchable text, so normalization is
            # symmetric) lets plural phrasing match without a stemmer.
            if len(tok) > 3 and tok.endswith("s"):
                out.add(tok[:-1])
    return out


class SkillRegistry:
    """In-memory skill index built once at import/startup."""

    def __init__(self, skills: list[Skill]):
        self._by_name: dict[str, Skill] = {skill.name: skill for skill in skills}

    @classmethod
    def from_directory(cls, directory: Path = _SKILLS_DIR) -> SkillRegistry:
        skills: list[Skill] = []
        for path in sorted(directory.glob("*.md")):
            if path.name in _RESERVED_FILENAMES:
                continue
            parsed = _parse_skill(path)
            if parsed is not None:
                skills.append(parsed)
        return cls(skills)

    def all(self) -> list[Skill]:
        return list(self._by_name.values())

    def filter(
        self,
        platform: str | list[str] | None = None,
        tag: str | list[str] | None = None,
    ) -> list[Skill]:
        wanted_platforms = _coerce_filter(platform)
        wanted_tags = _coerce_filter(tag)
        results = self.all()
        if wanted_platforms is not None:
            results = [
                skill
                for skill in results
                if any(platform in wanted_platforms for platform in skill.platforms)
            ]
        if wanted_tags is not None:
            results = [
                skill for skill in results if any(tag in wanted_tags for tag in skill.tags)
            ]
        return results

    def match(self, query: str, *, limit: int = 3) -> list[Skill]:
        q_tokens = _match_tokens(query or "")
        if not q_tokens:
            return []
        scored: list[tuple[int, Skill]] = []
        for skill in self.all():
            searchable = " ".join(
                [skill.name, skill.title, " ".join(skill.tags), " ".join(skill.platforms)]
            )
            overlap = len(q_tokens & _match_tokens(searchable))
            if overlap:
                scored.append((overlap, skill))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [skill for _score, skill in scored[:limit]]

    def lookup(self, name: str) -> Skill | list[Skill] | None:
        normalized = name.strip().lower()
        if not normalized:
            return None
        for skill in self._by_name.values():
            if skill.name.lower() == normalized:
                return skill
        candidates = [
            skill for skill in self._by_name.values() if normalized in skill.name.lower()
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return candidates
        return None


_REGISTRY: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """Return the process-wide skill registry (loaded once)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SkillRegistry.from_directory()
    return _REGISTRY


def reset_registry_for_tests(registry: SkillRegistry | None = None) -> None:
    """Test helper to replace or clear the cached registry."""
    global _REGISTRY
    _REGISTRY = registry


# -- Backend gating ----------------------------------------------------------
#
# Prompts are gated by enabled backend already (tool_router calls
# register_router_prompts(..., enabled_backends=_BACKENDS)), but skills load
# whole-directory, so `uxi-diagnostics` was listed on a deployment carrying no
# UXI tool at all -- a runbook whose every step is uncallable.
#
# The router calls set_enabled_platforms() once it knows its backends. Until
# then the gate stays open (None), which leaves the standalone rag-core server
# and every existing test unchanged.
_ENABLED_PLATFORMS: frozenset[str] | None = None


def set_enabled_platforms(platforms: Collection[str] | None) -> None:
    """Restrict browse/search to skills this deployment can actually run.

    Pass ``None`` to disable gating entirely (the default).
    """
    global _ENABLED_PLATFORMS
    _ENABLED_PLATFORMS = None if platforms is None else frozenset(platforms)


def _platform_visible(skill: Skill) -> bool:
    if _ENABLED_PLATFORMS is None:
        return True
    # A skill declaring no platform is platform-agnostic, so it stays visible.
    if not skill.platforms:
        return True
    # The FIRST platform is the required one; the rest are correlation targets
    # the runbook uses "when those backends are enabled". Every skill named
    # `<platform>-*` lists that platform first, and unprefixed skills lead with
    # the platform they cannot run without. Gating on "any platform enabled"
    # would keep `uxi-diagnostics` visible on a UXI-less deployment purely
    # because it can *also* correlate against Central.
    return skill.platforms[0] in _ENABLED_PLATFORMS


def _visible(skills: list[Skill]) -> list[Skill]:
    return [skill for skill in skills if _platform_visible(skill)]


def list_skills_payload(
    platform: str | list[str] | None = None,
    tag: str | list[str] | None = None,
    detail: bool = False,
    registry: SkillRegistry | None = None,
) -> dict[str, Any]:
    reg = registry or get_registry()
    results = _visible(reg.filter(platform=platform, tag=tag))
    skills: list[dict[str, Any]] = []
    for skill in results:
        entry = skill.to_metadata() if detail else skill.to_summary()
        entry["load_with"] = f"load_skill(name={skill.name!r})"
        skills.append(entry)
    return {
        "count": len(results),
        "skills": skills,
        "next_step": _LIST_NEXT_STEP_MATCHED if results else _LIST_NEXT_STEP_EMPTY,
    }


def find_skill_payload(
    query: str,
    limit: int = 3,
    registry: SkillRegistry | None = None,
) -> dict[str, Any]:
    """Rank skills against free-text intent.

    ``list_skills`` filters only by platform/tag, so selecting a runbook from
    a natural-language request meant reading every entry. The registry has
    carried a token-overlap matcher all along; this exposes it.
    """
    reg = registry or get_registry()
    capped = max(1, limit)
    # Over-fetch before gating so hidden skills cannot consume result slots.
    matches = _visible(reg.match(query, limit=capped * 4))[:capped]
    skills: list[dict[str, Any]] = []
    for skill in matches:
        entry = skill.to_summary()
        entry["load_with"] = f"load_skill(name={skill.name!r})"
        skills.append(entry)
    return {
        "query": query,
        "count": len(skills),
        "skills": skills,
        "next_step": _LIST_NEXT_STEP_MATCHED if skills else _FIND_NEXT_STEP_EMPTY,
    }


def load_skill_payload(
    name: str,
    registry: SkillRegistry | None = None,
) -> dict[str, Any]:
    reg = registry or get_registry()
    match = reg.lookup(name)
    if match is None:
        return {
            "error": (
                f"No skill matches {name!r}. Call `list_skills()` to see available skills."
            )
        }
    if isinstance(match, list):
        return {
            "error": (
                f"Multiple skills match {name!r}: "
                f"{', '.join(sorted(skill.name for skill in match))}. "
                "Use a more specific name."
            )
        }
    return {
        "name": match.name,
        "title": match.title,
        "description": match.description,
        "platforms": list(match.platforms),
        "tags": list(match.tags),
        "tools": list(match.tools),
        "body": match.body,
    }
