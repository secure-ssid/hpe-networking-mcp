"""Read-only defaults and write-confirmation policy for the CLI client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Annotations commonly set by MCP servers (FastMCP / this repo).
_READ_HINTS = ("readOnlyHint", "read_only_hint", "readonly")
_DESTRUCTIVE_HINTS = ("destructiveHint", "destructive_hint", "destructive")
_IDEMPOTENT_HINTS = ("idempotentHint", "idempotent_hint")


def _anno_bool(annotations: Any, names: tuple[str, ...]) -> bool | None:
    if annotations is None:
        return None
    # Tool annotations may be a pydantic model or a plain dict.
    for name in names:
        if isinstance(annotations, dict):
            if name in annotations:
                return bool(annotations[name])
            continue
        if hasattr(annotations, name):
            val = getattr(annotations, name)
            if val is not None:
                return bool(val)
    return None


def tool_is_read_only(tool: Any) -> bool:
    """Best-effort classification from MCP tool annotations + name heuristics."""
    if isinstance(tool, dict):
        if "read_only" in tool:
            return bool(tool["read_only"])
        capability = str(tool.get("capability", "")).lower()
        if capability in {"read", "diagnostic", "readonly", "read_only"}:
            return True
        if capability in {"write", "destructive", "idempotent_write"}:
            return False
        annotations = tool.get("annotations")
        tool_name = tool.get("name", "")
    else:
        read_only = getattr(tool, "read_only", None)
        if read_only is not None:
            return bool(read_only)
        capability = str(getattr(tool, "capability", "") or "").lower()
        if capability in {"read", "diagnostic", "readonly", "read_only"}:
            return True
        if capability in {"write", "destructive", "idempotent_write"}:
            return False
        annotations = getattr(tool, "annotations", None)
        tool_name = getattr(tool, "name", "")
    read = _anno_bool(annotations, _READ_HINTS)
    if read is True:
        return True
    destructive = _anno_bool(annotations, _DESTRUCTIVE_HINTS)
    if destructive is True:
        return False
    if read is False:
        return False

    name = str(tool_name or "").lower()
    # Prefer namespaced bare segment.
    bare = name.rsplit(".", 1)[-1]
    write_prefixes = (
        "create_",
        "update_",
        "delete_",
        "remove_",
        "set_",
        "add_",
        "apply_",
        "reboot_",
        "bounce_",
        "disconnect_",
        "migrate_",
        "write_",
        "post_",
        "put_",
        "patch_",
    )
    if bare.startswith(write_prefixes) or bare in {"invoke_tool"}:
        return False
    read_prefixes = (
        "get_",
        "list_",
        "find_",
        "lookup_",
        "search_",
        "ask_",
        "describe_",
        "show_",
        "read_",
        "fetch_",
        "check_",
        "validate_",
        "count_",
    )
    if bare.startswith(read_prefixes) or bare in {"invoke_read_tool", "ping", "health"}:
        return True
    # Unknown → treat as write-capable (fail closed for dispatch defaults).
    return False


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str
    requires_confirm: bool = False
    is_read_only: bool = True


@dataclass
class SafetyPolicy:
    """Default: only read-only tools may run without an explicit confirm flag."""

    read_only_default: bool = True
    allow_writes: bool = False
    confirmed: bool = False  # set by --yes / interactive confirm

    def check(self, tool: Any, *, force_write: bool = False) -> SafetyDecision:
        ro = tool_is_read_only(tool)
        name = tool.get("name", "?") if isinstance(tool, dict) else getattr(tool, "name", "?")
        if ro:
            return SafetyDecision(allowed=True, reason="read-only tool", is_read_only=True)
        if not self.read_only_default and self.allow_writes:
            if self.confirmed or force_write:
                return SafetyDecision(
                    allowed=True,
                    reason="write allowed (confirmed)",
                    requires_confirm=False,
                    is_read_only=False,
                )
            return SafetyDecision(
                allowed=False,
                reason=f"write tool {name!r} requires confirmation (--yes or interactive)",
                requires_confirm=True,
                is_read_only=False,
            )
        if force_write and self.confirmed:
            return SafetyDecision(
                allowed=True,
                reason="write forced with confirmation",
                is_read_only=False,
            )
        return SafetyDecision(
            allowed=False,
            reason=(
                f"write tool {name!r} blocked by read-only default; "
                "pass --allow-writes --yes to override"
            ),
            requires_confirm=True,
            is_read_only=False,
        )
