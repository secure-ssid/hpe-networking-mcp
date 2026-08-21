"""No module outside the SDK quarantine may reach into MCP's private tool manager.

``ToolManager`` and its ``_tools`` dict are private to ``mcp``. This package's
platform write gate -- the security boundary that refuses a write/destructive
tool under ``safe-read-only`` or a disabled platform gate -- is installed by
intercepting the manager's dispatcher, so an upstream rename would not fail
loudly; it would silently remove the gate. Every such reach is therefore
confined to :mod:`hpe_networking_mcp.mcp_servers._sdk_compat`, which is
exercised by :func:`test_sdk_compat_matches_the_installed_sdk` below so a
rename surfaces as one obvious failure instead of a missing boundary.

What counts as a reach (deliberate, not an accident of regex)
-------------------------------------------------------------
The scan is AST-based, not textual, so the rule can be stated precisely:

* **Attribute access** -- any ``x._tool_manager`` expression is a reach.
* **String literals** -- a string containing ``_tool_manager`` is a reach
  *unless* it is a module/class/function docstring. This is the deliberate
  part: ``pipeline/project_facts.py`` builds Python probe source as a string
  literal and executes it in a subprocess. That source is code. An upstream
  rename breaks it exactly as it breaks in-process code, only later and with a
  worse diagnostic (a subprocess traceback inside a fact-collection script), so
  it is held to the same standard.
* **Docstrings and ``#`` comments are prose and are exempt.** Prose that names
  the private attribute -- to explain why the quarantine exists, for instance --
  creates no coupling. Comments are invisible to the AST, which makes that
  exemption structural rather than accidental.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PRIVATE_ATTR = "_tool_manager"

#: The one module permitted to reach in. See its docstring for the three SDK
#: capabilities (synchronous registry introspection, publishing a pre-built
#: ``Tool``, raw in-process dispatch) that have no public equivalent in mcp 2.x.
QUARANTINE = {"_sdk_compat.py"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every string constant that serves as a docstring."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _offenders_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    rel = path.relative_to(REPO_ROOT)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == PRIVATE_ATTR:
            hits.append(f"{rel}:{node.lineno} (attribute access)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if PRIVATE_ATTR in node.value and id(node) not in docstrings:
                hits.append(f"{rel}:{node.lineno} (code in a string literal)")
    return hits


def test_no_private_tool_manager_access() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in QUARANTINE:
            continue
        offenders.extend(_offenders_in(path))
    assert not offenders, (
        "private MCP SDK tool-manager access outside "
        f"{sorted(QUARANTINE)}:\n  " + "\n  ".join(offenders)
    )


def test_quarantine_module_is_the_one_that_reaches_in() -> None:
    """The exemption must be load-bearing, not a stale entry."""
    compat = SRC / "hpe_networking_mcp" / "mcp_servers" / "_sdk_compat.py"
    assert compat.name in QUARANTINE
    assert _offenders_in(compat), (
        "_sdk_compat.py no longer touches the private tool manager -- "
        "drop it from QUARANTINE"
    )


def test_generated_probe_source_uses_the_public_helper() -> None:
    """The subprocess probe is executed code, so it must not embed the private name.

    Guards the decision documented in this module's docstring against someone
    later re-introducing ``r.mcp._tool_manager._tools`` into the probe string on
    the grounds that "it is only a string".
    """
    from hpe_networking_mcp.pipeline import project_facts

    source = Path(project_facts.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    probe_lines = [text for text in literals if "r.mcp" in text]
    assert probe_lines, "router-mode probe source literals not found -- test is stale"
    for text in probe_lines:
        assert PRIVATE_ATTR not in text, f"probe source embeds a private SDK name: {text!r}"


def test_sdk_compat_matches_the_installed_sdk() -> None:
    """Pin every SDK internal the quarantine depends on.

    This is the loud failure an ``mcp`` point release should trip instead of
    silently detaching the write gate.
    """
    import anyio
    from mcp.server.mcpserver import MCPServer

    from hpe_networking_mcp.mcp_servers import _sdk_compat as compat

    server = MCPServer("sdk-compat-probe")

    @server.tool()
    def probe_tool(value: int = 1) -> dict:
        return {"value": value}

    assert compat.tool_names(server) == ["probe_tool"]
    assert set(compat.tool_registry(server)) == {"probe_tool"}

    tool = compat.get_tool(server, "probe_tool")
    assert tool is not None
    # The router classifies capability from annotations and reads the JSON
    # schema off `parameters`; both are internal-tool attributes that the
    # `MCPTool` wire form from `list_tools()` renames or drops.
    assert hasattr(tool, "annotations")
    assert isinstance(tool.parameters, dict)
    assert compat.get_tool(server, "absent") is None

    # A pre-built Tool republishes verbatim -- object identity, not a rebuild.
    other = MCPServer("sdk-compat-probe-2")
    compat.register_tool_object(other, "probe_tool", tool)
    assert compat.get_tool(other, "probe_tool") is tool

    # Raw dispatch returns the tool's Python value, not a serialized CallToolResult.
    assert anyio.run(compat.call_tool_raw, server, "probe_tool", {"value": 7}) == {
        "value": 7
    }

    # Interception seam: claim/replace/restore, and the public `call_tool`
    # funnels through it (this is what makes the write gate cover direct
    # by-name calls, not just the router's dispatch).
    pristine = compat.claim_dispatcher(server, "_probe_marker")
    assert compat.claim_dispatcher(server, "_probe_marker") is pristine

    seen: list[str] = []

    async def intercepted(name, arguments, context=None, convert_result=False):
        seen.append(name)
        return await pristine(name, arguments, context, convert_result=convert_result)

    compat.set_dispatcher(server, intercepted)
    anyio.run(server.call_tool, "probe_tool", {})
    assert seen == ["probe_tool"], "public call_tool no longer routes through the seam"
    compat.set_dispatcher(server, pristine)

    assert compat.install_sorted_tool_listing(server) is True
    assert compat.install_sorted_tool_listing(server) is False
