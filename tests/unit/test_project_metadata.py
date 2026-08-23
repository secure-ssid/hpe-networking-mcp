import ast
from pathlib import Path

from hpe_networking_mcp.pipeline import project_facts

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
REPO_ROOT = PYPROJECT.parents[0]
ACTIVE_CODE_DIRS = (
    "src/hpe_networking_mcp/mcp_servers",
    "src/hpe_networking_mcp/pipeline",
    "scripts",
)
SCRAPER = REPO_ROOT / "ingestion" / "scrape.py"
BOUNDED_GENERIC_GET_TOOLS = {
    "src/hpe_networking_mcp/mcp_servers/glp.py": "glp_get",
    "src/hpe_networking_mcp/mcp_servers/clearpass.py": "clearpass_get",
    "src/hpe_networking_mcp/mcp_servers/mist.py": "mist_get",
    "src/hpe_networking_mcp/mcp_servers/apstra.py": "apstra_get",
    "src/hpe_networking_mcp/mcp_servers/aos8.py": "aos8_get",
    "src/hpe_networking_mcp/mcp_servers/edgeconnect.py": "edgeconnect_get",
    "src/hpe_networking_mcp/mcp_servers/uxi.py": "uxi_get",
}
MAX_MCP_LIST_DEFAULT = 200


def _project_dependencies(pyproject_text: str) -> list[str]:
    in_project_dependencies = False
    dependencies: list[str] = []

    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped == "dependencies = [":
            in_project_dependencies = True
            continue
        if in_project_dependencies and stripped == "]":
            break
        if in_project_dependencies and stripped.startswith('"'):
            dependencies.append(stripped.split('"', 2)[1])

    return dependencies


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _calls_name(node: ast.AST, name: str) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id == name:
                return True
    return False


def _calls_name_directly_or_via_helper(
    tree: ast.Module,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> bool:
    if _calls_name(function, name):
        return True
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for child in ast.walk(function):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            helper = helpers.get(child.func.id)
            if helper is not None and _calls_name(helper, name):
                return True
    return False


def _is_mcp_tool(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "mcp"
        ):
            return True
    return False


def _project_table(pyproject_text: str, section: str) -> dict[str, str]:
    """Return simple ``key = "value"`` pairs from one pyproject table."""
    values: dict[str, str] = {}
    current = ""
    for raw in pyproject_text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            current = line
            continue
        if current != section or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            values[key.strip()] = value[1:-1]
    return values


def test_project_name_matches_repo_name():
    text = PYPROJECT.read_text()

    assert 'name = "hpe-networking-mcp"' in text
    assert 'name = "api-central"' not in text


def test_changelog_url_points_at_the_root_changelog_file_on_github():
    """``[project.urls].Changelog`` must not point at a versioned Pages note.

    The root ``CHANGELOG.md`` is the rolling index of every release, but it
    lives outside ``docs/`` -- the GitHub Pages source -- so it has no
    ``https://secure-ssid.github.io/hpe-networking-mcp/...`` URL at all.
    Pointing PyPI's "Changelog" link at a single release-notes page
    (``release-notes-0.7.0.html``) both misses the rename entry and silently
    goes stale on the next release.
    """
    urls = _project_table(PYPROJECT.read_text(), "[project.urls]")

    assert urls["Changelog"] == (
        "https://github.com/secure-ssid/hpe-networking-mcp/blob/main/CHANGELOG.md"
    )
    assert "release-notes-" not in urls["Changelog"]
    assert (PYPROJECT.parent / "CHANGELOG.md").is_file()
    assert not (PYPROJECT.parent / "docs" / "CHANGELOG.md").exists()


def test_project_description_quotes_the_complete_backend_total():
    """The published package summary must use the complete backend total.

    ``docs/project-facts.json`` distinguishes the platform API backend total
    (vendor-facing tools only) from the complete registered backend total
    (plus the credential-free ``design-core``/``interop-core`` backends).
    The package description says "backend catalog" without qualification, so
    it must carry the complete total; publishing the platform-only subtotal
    there is the drift this guards.
    """
    tracked = project_facts.load()
    description = _project_table(PYPROJECT.read_text(), "[project]")["description"]
    complete = tracked["tools"]["registered_total"]
    platform_only = tracked["tools"]["platform_backend_total"]

    assert f"{complete:,}-tool backend catalog" in description
    assert f"{platform_only:,}" not in description


def test_project_declares_mit_license_metadata():
    text = PYPROJECT.read_text()

    assert 'license = "MIT"' in text
    assert 'license-files = ["LICENSE"]' in text
    assert 'license = {' not in text
    assert "License :: OSI Approved :: MIT License" not in text


def test_active_code_does_not_use_legacy_project_aliases():
    legacy_aliases = ("api-central", "API-Central", "central-mcp-server")
    violations: list[str] = []

    for dirname in ACTIVE_CODE_DIRS:
        for path in sorted((REPO_ROOT / dirname).rglob("*.py")):
            text = path.read_text()
            for alias in legacy_aliases:
                if alias in text:
                    violations.append(f"{path.relative_to(REPO_ROOT)} contains {alias}")

    assert violations == []


def test_active_runtime_code_does_not_reference_removed_qdrant_backend():
    violations: list[str] = []

    for dirname in ACTIVE_CODE_DIRS:
        for path in sorted((REPO_ROOT / dirname).rglob("*.py")):
            if "qdrant" in path.read_text().lower():
                violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == []


def test_active_runtime_code_does_not_reference_removed_central_sdk():
    removed_sdk = "py" + "central"
    violations: list[str] = []

    for dirname in ACTIVE_CODE_DIRS:
        for path in sorted((REPO_ROOT / dirname).rglob("*.py")):
            if removed_sdk in path.read_text().lower():
                violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == []


def test_doc_scraper_excludes_removed_central_sdk_pages():
    removed_sdk = "py" + "central"
    assert removed_sdk not in SCRAPER.read_text().lower()


def test_direct_runtime_dependencies_do_not_include_removed_sdks():
    removed_sdk = "py" + "central"
    dependencies = _project_dependencies(PYPROJECT.read_text())
    names = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0]
        for dependency in dependencies
    }

    assert removed_sdk not in names
    assert "requests" not in names
    assert "httpx" in names


def test_generic_read_only_get_tools_bound_list_responses():
    violations: list[str] = []

    for relative_path, function_name in BOUNDED_GENERIC_GET_TOOLS.items():
        path = REPO_ROOT / relative_path
        tree = ast.parse(path.read_text())
        function = _function_node(tree, function_name)
        arg_names = [arg.arg for arg in function.args.args]

        if "limit" not in arg_names or "offset" not in arg_names:
            violations.append(f"{relative_path}:{function_name} missing limit/offset")
        docstring = ast.get_docstring(function) or ""
        if "limit" not in docstring or "offset" not in docstring:
            violations.append(
                f"{relative_path}:{function_name} docstring does not describe limit/offset"
            )
        if not _calls_name_directly_or_via_helper(tree, function, "bound_collection_response"):
            violations.append(
                f"{relative_path}:{function_name} does not call bound_collection_response"
            )

    assert violations == []


def test_mcp_tool_limit_defaults_do_not_exceed_project_bound():
    violations: list[str] = []

    for path in sorted((REPO_ROOT / "src" / "hpe_networking_mcp" / "mcp_servers").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _is_mcp_tool(node):
                continue
            defaults = [None] * (len(node.args.args) - len(node.args.defaults))
            defaults.extend(node.args.defaults)
            for arg, default in zip(node.args.args, defaults, strict=True):
                if arg.arg != "limit" or not isinstance(default, ast.Constant):
                    continue
                if isinstance(default.value, int) and default.value > MAX_MCP_LIST_DEFAULT:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.name} limit default "
                        f"{default.value} exceeds {MAX_MCP_LIST_DEFAULT}"
                    )

    assert violations == []
