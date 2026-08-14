"""Discover and load SKILL.md workflow files (repo + personal)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hpe_networking_mcp.cli_client.config import default_user_data_dir, repo_root_from_package

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

REPO_SKILL_DIRS = (
    ".github/skills",
    ".claude/skills",
    ".agents/skills",
)
USER_SKILL_DIRS = (
    ".copilot/skills",
    ".agents/skills",
    ".config/hpe-mcp/skills",
)


@dataclass
class Skill:
    name: str
    path: Path
    description: str = ""
    body: str = ""
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def title(self) -> str:
        t = self.meta.get("name") or self.meta.get("title")
        return str(t) if t else self.name


def _parse_skill_md(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, object] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        raw_meta, body = m.group(1), m.group(2)
        try:
            loaded = yaml.safe_load(raw_meta) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            meta = {}
    name = str(meta.get("name") or path.parent.name or path.stem)
    desc = str(meta.get("description") or "")
    return Skill(name=name, path=path, description=desc, body=body.strip(), meta=meta)


def skill_search_roots(repo_root: Path | None = None) -> list[Path]:
    root = repo_root or repo_root_from_package()
    roots: list[Path] = []
    for rel in REPO_SKILL_DIRS:
        roots.append(root / rel)
    home = Path.home()
    for rel in USER_SKILL_DIRS:
        roots.append(home / rel)
    roots.append(default_user_data_dir() / "skills")
    return roots


def discover_skills(repo_root: Path | None = None) -> list[Skill]:
    """Return skills keyed by directory name; later roots do not override earlier names."""
    seen: dict[str, Skill] = {}
    for root in skill_search_roots(repo_root):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                skill = _parse_skill_md(skill_md)
            except OSError:
                continue
            if skill.name not in seen:
                seen[skill.name] = skill
    return sorted(seen.values(), key=lambda s: s.name.lower())


def get_skill(name: str, repo_root: Path | None = None) -> Skill:
    for skill in discover_skills(repo_root):
        if skill.name == name or skill.path.parent.name == name:
            return skill
    raise KeyError(f"unknown skill {name!r}")
