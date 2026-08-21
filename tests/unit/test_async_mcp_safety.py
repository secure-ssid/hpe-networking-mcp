import ast
from pathlib import Path

MCP_SERVER_DIR = Path(__file__).resolve().parents[2] / "src" / "hpe_networking_mcp" / "mcp_servers"


def _attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _full_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _full_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _async_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def test_async_mcp_tools_do_not_call_sync_http_or_blocking_sleep():
    violations: list[str] = []

    for path in sorted(MCP_SERVER_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for func in _async_functions(tree):
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                attr = _attribute_name(node.func)
                name = _full_name(node.func)
                if attr == "_request":
                    violations.append(
                        f"{path.name}:{func.lineno}:{func.name} calls sync _request()"
                    )
                if name == "time.sleep":
                    violations.append(f"{path.name}:{func.lineno}:{func.name} calls time.sleep()")

    assert violations == []
