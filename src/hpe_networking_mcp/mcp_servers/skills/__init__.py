"""Skills — markdown-defined multi-step procedures for hpe-networking-mcp.

Browse with ``list_skills`` and load full runbooks with ``load_skill``.
Skills are packaged under this module and registered on the ``rag-core``
server as local, read-only tools.
"""

from hpe_networking_mcp.mcp_servers.skills._engine import (
    Skill,
    SkillRegistry,
    get_registry,
    list_skills_payload,
    load_skill_payload,
    reset_registry_for_tests,
)

__all__ = [
    "Skill",
    "SkillRegistry",
    "get_registry",
    "list_skills_payload",
    "load_skill_payload",
    "reset_registry_for_tests",
]
