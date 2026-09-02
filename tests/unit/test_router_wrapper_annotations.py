"""Promoted router wrappers must not declare a narrower return type than the
tool they dispatch to.

FastMCP builds an output validator from each tool's return annotation. A
wrapper annotated ``dict[str, Any]`` in front of a tool that can return a bare
list (unbounded list responses) or ``None`` (a lookup miss) makes the wrapper
fail with a Pydantic ``DictModel`` validation error at runtime while the
dispatcher path for the same tool succeeds.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVERS = Path(__file__).resolve().parents[2] / "src" / "hpe_networking_mcp" / "mcp_servers"
ROUTER = SERVERS / "tool_router.py"


def _returns(node: ast.AST) -> str | None:
    returns = getattr(node, "returns", None)
    return ast.unparse(returns) if returns else None


def _promoted_wrappers() -> dict[str, tuple[str | None, str]]:
    """Map wrapper name -> (return annotation, dispatched tool name)."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    found: dict[str, tuple[str | None, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        decorators = [ast.unparse(d) for d in node.decorator_list]
        if not any("_dispatching_wrapper_tool" in d for d in decorators):
            continue
        target = node.name
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "invoke_tool"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            ):
                target = call.args[1].value
                break
        found[node.name] = (_returns(node), target)
    return found


def _underlying_tools() -> dict[str, str | None]:
    """Map @mcp.tool name -> return annotation, across every backend module."""
    found: dict[str, str | None] = {}
    for path in SERVERS.rglob("*.py"):
        if path == ROUTER:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if any("mcp.tool" in ast.unparse(d) for d in node.decorator_list):
                found.setdefault(node.name, _returns(node))
    return found


def test_promoted_wrappers_are_registered() -> None:
    """Guard the guard: the AST scan must actually find the wrappers."""
    wrappers = _promoted_wrappers()
    assert len(wrappers) >= 15, f"expected the promoted wrapper set, found {sorted(wrappers)}"
    for name in ("list_sites", "list_devices", "find_device", "find_client", "get_site"):
        assert name in wrappers, f"{name} is no longer a promoted wrapper"


def test_wrapper_return_annotations_are_not_narrower_than_the_dispatched_tool() -> None:
    wrappers = _promoted_wrappers()
    underlying = _underlying_tools()

    violations: list[str] = []
    for wrapper, (wrapper_returns, target) in sorted(wrappers.items()):
        target_returns = underlying.get(target)
        if wrapper_returns is None or target_returns is None:
            continue
        # ``Any`` accepts everything, so it can never be too narrow.
        if wrapper_returns.strip() == "Any":
            continue
        if "list" in target_returns and "list" not in wrapper_returns:
            violations.append(
                f"{wrapper} -> {target}: wrapper returns {wrapper_returns!r} but the tool "
                f"can return a list ({target_returns!r})"
            )
        if "None" in target_returns and "None" not in wrapper_returns:
            violations.append(
                f"{wrapper} -> {target}: wrapper returns {wrapper_returns!r} but the tool "
                f"can return None ({target_returns!r})"
            )

    assert not violations, "promoted wrapper return annotations are too narrow:\n" + "\n".join(
        violations
    )
