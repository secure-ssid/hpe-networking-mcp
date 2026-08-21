import ast
import importlib
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_LINK_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"', re.IGNORECASE)
HTML_IMAGE_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
ROUTER_INVOKE_RE = re.compile(r"\binvoke_(?:read_)?tool\((.+)\)")
CORE_VISUAL_DOCS = {
    "README.md",
    "docs/index.md",
    "docs/getting-started.md",
    "docs/mcp-client-recipes.md",
    "docs/tool-router.md",
    "docs/example-prompts.md",
    "docs/troubleshooting.md",
    "docs/optional-products.md",
    "docs/architecture/system-overview.md",
    "docs/architecture/how-it-works.md",
}
BACKEND_MODULES = [
    "hpe_networking_mcp.mcp_servers.config",
    "hpe_networking_mcp.mcp_servers.monitoring",
    "hpe_networking_mcp.mcp_servers.nac",
    "hpe_networking_mcp.mcp_servers.ops",
    "hpe_networking_mcp.mcp_servers.central_streaming",
    "hpe_networking_mcp.mcp_servers.glp",
    "hpe_networking_mcp.mcp_servers.rag",
    "hpe_networking_mcp.mcp_servers.design",
    "hpe_networking_mcp.mcp_servers.clearpass",
    "hpe_networking_mcp.mcp_servers.mist",
    "hpe_networking_mcp.mcp_servers.apstra",
    "hpe_networking_mcp.mcp_servers.aos8",
    "hpe_networking_mcp.mcp_servers.edgeconnect",
    "hpe_networking_mcp.mcp_servers.uxi",
    "hpe_networking_mcp.mcp_servers.axis",
]


def _tracked_markdown_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        text=True,
    )
    return [REPO_ROOT / line for line in output.splitlines()]


def test_tracked_markdown_local_links_and_images_resolve():
    missing = []

    for path in _tracked_markdown_files():
        markdown = path.read_text(errors="replace")
        matches = (
            list(MARKDOWN_LINK_RE.finditer(markdown))
            + list(MARKDOWN_IMAGE_RE.finditer(markdown))
            + list(HTML_IMAGE_RE.finditer(markdown))
        )
        for match in matches:
            target = match.group(1).split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]

            candidate = (path.parent / unquote(target)).resolve()
            if not candidate.exists():
                missing.append(f"{path.relative_to(REPO_ROOT)} -> {target}")

    assert missing == []


def _heading_ids(markdown: str) -> set[str]:
    ids: set[str] = set()
    duplicates: dict[str, int] = {}
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        heading = heading.replace("`", "").lower()
        slug = re.sub(r"[^\w\s-]", "", heading)
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        duplicate = duplicates.get(slug, 0)
        duplicates[slug] = duplicate + 1
        ids.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return ids


def test_core_visual_documentation_local_anchors_resolve():
    missing = []
    for relative_path in CORE_VISUAL_DOCS:
        path = REPO_ROOT / relative_path
        markdown = path.read_text(errors="replace")
        matches = list(MARKDOWN_LINK_RE.finditer(markdown)) + list(HTML_LINK_RE.finditer(markdown))
        for match in matches:
            target = match.group(1).strip()
            if "#" not in target or target.startswith(("http://", "https://", "mailto:")):
                continue
            target_path, anchor = target.split("#", 1)
            candidate = path if not target_path else (path.parent / unquote(target_path)).resolve()
            if candidate.suffix != ".md" or not candidate.exists() or not anchor:
                continue
            if unquote(anchor) not in _heading_ids(candidate.read_text(errors="replace")):
                missing.append(f"{relative_path} -> {target}")

    assert missing == []


def test_pages_sitemap_and_robots_cover_landing_navigation():
    site_root = "https://secure-ssid.github.io/hpe-networking-mcp"
    robots = (REPO_ROOT / "docs" / "robots.txt").read_text()
    sitemap = REPO_ROOT / "docs" / "sitemap.xml"
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls = {
        node.text
        for node in ET.parse(sitemap).getroot().findall("sm:url/sm:loc", namespace)
    }
    expected_urls = {f"{site_root}/"}
    index = REPO_ROOT / "docs" / "index.md"

    for match in MARKDOWN_LINK_RE.finditer(index.read_text(errors="replace")):
        target = match.group(1).split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = (index.parent / unquote(target)).resolve()
        if target_path.suffix != ".md":
            continue
        relative = target_path.relative_to(REPO_ROOT / "docs")
        url_path = relative.with_suffix(".html").as_posix()
        if relative.name == "index.md":
            expected_urls.add(f"{site_root}/")
        else:
            expected_urls.add(f"{site_root}/{url_path}")

    assert f"Sitemap: {site_root}/sitemap.xml" in robots
    assert sitemap_urls == expected_urls


def _backend_tool_args() -> dict[str, set[str]]:
    tools: dict[str, set[str]] = {}
    for module_name in BACKEND_MODULES:
        module = importlib.import_module(module_name)
        for name, tool in module.mcp._tool_manager._tools.items():
            schema = tool.parameters if isinstance(tool.parameters, dict) else {}
            tools[name] = set((schema.get("properties") or {}).keys())
    return tools


def test_documented_router_invocation_arguments_match_backend_tools():
    tool_args = _backend_tool_args()
    problems = []

    for path in _tracked_markdown_files():
        for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            match = ROUTER_INVOKE_RE.search(line.strip())
            if not match:
                continue
            try:
                call = ast.parse(f"f({match.group(1)})", mode="eval").body
            except SyntaxError as exc:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: cannot parse invocation: {exc}"
                )
                continue
            args = call.args
            if (
                not args
                or not isinstance(args[0], ast.Constant)
                or not isinstance(args[0].value, str)
            ):
                continue
            tool_name = args[0].value
            if tool_name not in tool_args:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: "
                    f"unknown tool {tool_name!r}"
                )
                continue
            if len(args) < 2 or not isinstance(args[1], ast.Dict):
                continue
            documented = {
                key.value
                for key in args[1].keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            unknown = documented - tool_args[tool_name]
            if unknown:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: {tool_name} "
                    f"unknown args {sorted(unknown)}; valid={sorted(tool_args[tool_name])}"
                )

    assert problems == []


def test_product_workflow_tool_table_names_match_backend_tools():
    tool_args = _backend_tool_args()
    path = REPO_ROOT / "docs" / "product-workflows.md"
    problems = []

    for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not line.startswith("|") or "`" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        for tool_name in re.findall(r"`([a-z][a-z0-9_]+)`", cells[1]):
            if tool_name not in tool_args:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: unknown tool {tool_name!r}"
                )

    assert problems == []


def test_validation_docs_describe_current_guard_coverage():
    expected_phrases = [
        "committed low-token MCP config examples",
        "local-only config files",
        "router product/toolset docs",
        "bounded generic read-only GET tools",
        "MCP list default bounds",
        "RAG/search top_k bounds",
        "public tool-count claims",
        "tool-count docstrings",
        "rendered RAG/index doc-fact claims",
        "tracked Markdown local links and images",
        "Pages sitemap and robots metadata",
        "documented router example arguments",
        "product workflow tool-name tables",
        "wizard optional-product env tables",
    ]
    combined_docs = "\n".join(
        [
            (REPO_ROOT / "README.md").read_text(),
            (REPO_ROOT / "docs" / "README.md").read_text(),
        ]
    )

    for phrase in expected_phrases:
        assert phrase in combined_docs


def test_current_rag_architecture_avoids_removed_qdrant_implementation_names():
    architecture = (REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md").read_text()

    assert "qdrant_client" not in architecture
    assert "qdrant-client" not in architecture


def test_current_rag_architecture_does_not_claim_embedding_batches_are_sequential():
    architecture = (REPO_ROOT / "docs" / "architecture" / "RAG-ARCHITECTURE.md").read_text()

    assert "embed_batch` is sequential" not in architecture
    assert "40k-call serial loop" not in architecture
