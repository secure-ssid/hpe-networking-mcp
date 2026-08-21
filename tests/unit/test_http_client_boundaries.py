import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = ("src/hpe_networking_mcp/mcp_servers", "src/hpe_networking_mcp/pipeline")
SESSION_OWNER = (
    REPO_ROOT / "src" / "hpe_networking_mcp" / "pipeline" / "clients" / "central_client.py"
)


def _full_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _full_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _production_python_files():
    for dirname in PRODUCTION_DIRS:
        yield from sorted((REPO_ROOT / dirname).rglob("*.py"))


def test_production_code_does_not_import_requests():
    violations: list[str] = []

    for path in _production_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "requests" or alias.name.startswith("requests."):
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom) and (
                node.module == "requests" or (node.module or "").startswith("requests.")
            ):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports from {node.module}"
                )

    assert violations == []


def test_central_client_session_is_not_used_outside_central_client():
    violations: list[str] = []

    for path in _production_python_files():
        if path == SESSION_OWNER:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "session":
                name = _full_name(node)
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} accesses {name or '.session'}"
                )

    assert violations == []
