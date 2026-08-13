import ast
import re
from pathlib import Path

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count
from scripts import ingest_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_COUNT_RE = re.compile(r"\((\d+) tools\)")
GENERATED_TOOL_COUNT_RE = re.compile(
    r"\((?P<curated>\d+) curated \+ (?P<generated>\d+) generated OpenAPI tools\)"
)
PUBLIC_TOOL_COUNT_RE = re.compile(
    r"(?P<core>\d+) core tools\s*/\s+"
    r"(?P<read_only>\d+) read-only optional starters\s*/\s+"
    r"(?P<read_write>\d+) read-write optional starters"
)


def _registered_tool_count(tree: ast.Module) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
            ):
                count += 1
    return count


def test_module_docstring_tool_counts_match_registered_tools():
    counted_modules = []
    generated_modules = []

    for path in sorted((REPO_ROOT / "src" / "hpe_networking_mcp" / "mcp_servers").glob("*.py")):
        tree = ast.parse(path.read_text())
        docstring = ast.get_docstring(tree) or ""
        first_line = docstring.splitlines()[0] if docstring else ""

        # Modules that register generated OpenAPI tools declare the split count
        # "(C curated + G generated OpenAPI tools)". Only the C curated tools use
        # @mcp.tool decorators; the G generated tools come from the committed
        # manifest, so the AST decorator count must equal C and G must equal the
        # manifest operation count for the module's platform (module stem).
        gen_match = GENERATED_TOOL_COUNT_RE.search(first_line)
        if gen_match is not None:
            generated_modules.append(path.name)
            curated = int(gen_match.group("curated"))
            generated = int(gen_match.group("generated"))
            assert _registered_tool_count(tree) == curated, path.relative_to(REPO_ROOT)
            platform = path.stem
            assert manifest_operation_count(platform) == generated, path.relative_to(REPO_ROOT)
            continue

        match = TOOL_COUNT_RE.search(first_line)
        if match is None:
            continue

        counted_modules.append(path.name)
        expected = int(match.group(1))
        assert _registered_tool_count(tree) == expected, path.relative_to(REPO_ROOT)

    assert counted_modules
    assert "mist.py" in generated_modules


def test_public_tool_count_claims_match_registered_catalog(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    core_count = len(ingest_tools._collect())
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    read_only_count = len(ingest_tools._collect("all"))
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    read_write_count = len(ingest_tools._collect("all"))
    claim_paths = []

    for path in sorted([REPO_ROOT / "README.md", *(REPO_ROOT / "docs").rglob("*.md")]):
        for line in path.read_text(errors="replace").splitlines():
            match = PUBLIC_TOOL_COUNT_RE.search(line)
            if not match:
                continue
            claim_paths.append(str(path.relative_to(REPO_ROOT)))
            assert int(match.group("core")) == core_count
            assert int(match.group("read_only")) == read_only_count
            assert int(match.group("read_write")) == read_write_count

    assert claim_paths == ["README.md", "docs/architecture/RAG-ARCHITECTURE.md"]
