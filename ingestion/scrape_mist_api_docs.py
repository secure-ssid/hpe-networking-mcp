#!/usr/bin/env python3
"""Materialize Juniper's complete Mist API reference into RAG markdown.

The public Mist API page is an APIMatic JavaScript shell. The shell's
``portal.js`` points to a generated JSON document containing the full
navigation tree, guides, endpoint descriptions, models, enums, webhook
events, and examples. This scraper downloads that document once and renders
each virtual ``$h/`` page into a separate markdown file.

Usage:
    python ingestion/discover_mist_api_docs.py
    python ingestion/scrape_mist_api_docs.py --skip-existing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.scrape_report import write_scrape_report  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "sources" / "mist_api_docs"
URLS_PATH = Path(__file__).parent / "mist_api_docs_urls.json"
DEFAULT_ASSET_URL = (
    "https://www.juniper.net/documentation/us/en/software/mist/api/"
    "static/docs/mist-api-HTTP_CURL_V1.json"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def load_asset_url() -> str:
    if not URLS_PATH.exists():
        return DEFAULT_ASSET_URL
    values = json.loads(URLS_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise ValueError(f"{URLS_PATH} must contain one Mist API docs asset URL")
    return values[0]


def fetch_document(url: str) -> dict:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=120) as response:
        document = json.loads(response.read())
    if not isinstance(document, dict) or not isinstance(document.get("Sections"), list):
        raise ValueError(f"{url} is not a Mist APIMatic documentation document")
    return document


def is_page(node: object) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("SuggestedLink"), str)
        and node["SuggestedLink"].startswith("$h/")
    )


def iter_pages(sections: list[dict]) -> list[tuple[str, dict]]:
    pages: dict[str, dict] = {}

    def visit(node: object) -> None:
        if isinstance(node, dict):
            link = node.get("SuggestedLink")
            if isinstance(link, str) and link.startswith("$h/"):
                pages.setdefault(link, node)
            for child in node.get("Nodes", []):
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(sections)
    return sorted(pages.items())


def heading(text: str, level: int) -> str:
    return f"{'#' * min(max(level, 1), 6)} {text}".strip()


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_table(node: dict, lines: list[str]) -> None:
    header = node.get("Header", {}).get("Data", [])
    rows = [row.get("Data", []) for row in node.get("Rows", [])]
    if not isinstance(header, list):
        return
    columns = max([len(header), *(len(row) for row in rows)] or [0])
    if columns == 0:
        return
    header_values = [clean_cell(header[i] if i < len(header) else "") for i in range(columns)]
    lines.append("| " + " | ".join(header_values) + " |")
    lines.append("| " + " | ".join("---" for _ in range(columns)) + " |")
    for row in rows:
        lines.append(
            "| "
            + " | ".join(clean_cell(row[i] if i < len(row) else "") for i in range(columns))
            + " |"
        )
    lines.append("")


def render_param_table(parameters: list[dict], lines: list[str], title: str = "Parameters") -> None:
    if not parameters:
        return
    lines.extend(
        [
            f"### {title}",
            "",
            "| Name | Type | Location | Required | Description |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        description = clean_cell(parameter.get("Description"))
        if parameter.get("Nullable"):
            description = f"{description}<br>Nullable" if description else "Nullable"
        lines.append(
            "| "
            + " | ".join(
                [
                    clean_cell(parameter.get("Name")),
                    clean_cell(parameter.get("DataTypeMarkdown") or parameter.get("DataType")),
                    clean_cell(parameter.get("ParamType")),
                    "yes" if parameter.get("Required") else "no",
                    description,
                ]
            )
            + " |"
        )
    lines.append("")


def render_code_block(text: object, language: object, lines: list[str]) -> None:
    if not isinstance(text, str) or not text.strip():
        return
    lang = str(language or "").strip().lower()
    lines.extend([f"```{lang}", text.rstrip(), "```", ""])


def render_response_details(responses: list[dict], lines: list[str]) -> None:
    if not responses:
        return
    lines.extend(["### Responses", ""])
    for response in responses:
        if not isinstance(response, dict):
            continue
        status = response.get("StatusCode", "")
        description = response.get("Description") or ""
        lines.extend([f"#### {status}: {description}".rstrip(), ""])
        render_param_table(response.get("Headers", []), lines, title="Headers")
        for content in response.get("Content", []):
            if not isinstance(content, dict):
                continue
            content_type = content.get("ContentType") or "response"
            data_type = content.get("DataType") or ""
            lines.extend([f"- `{content_type}`: **{data_type}**", ""])
            example = content.get("Example")
            if isinstance(example, dict):
                render_code_block(example.get("Text"), example.get("Language", "json"), lines)
            examples = content.get("Examples") or []
            seen_examples: set[str] = set()
            for example in examples:
                if not isinstance(example, dict):
                    continue
                text = example.get("Text")
                if not isinstance(text, str) or text in seen_examples:
                    continue
                seen_examples.add(text)
                if example.get("Name"):
                    lines.extend([f"**Example: {example['Name']}**", ""])
                render_code_block(text, example.get("Language", "json"), lines)


def render_reference(node: dict, lines: list[str], level: int) -> None:
    node_type = node.get("Type")
    title = node.get("Title") or node.get("Name") or node_type
    if title:
        lines.extend([heading(str(title), level), ""])

    description = node.get("Description")
    if description:
        lines.extend([str(description).strip(), ""])

    if node_type == "endpointreference":
        signature = node.get("MethodSignature", {})
        render_code_block(signature.get("Text"), signature.get("Language", "http"), lines)
        if node.get("AuthDescription"):
            lines.extend(["### Authentication", str(node["AuthDescription"]).strip(), ""])
        render_param_table(node.get("Parameters", []), lines)
        render_response_details(node.get("Response", []), lines)
        if node.get("Errors"):
            lines.extend(["### Errors", "", "| Status | Description |", "| --- | --- |"])
            for error in node["Errors"]:
                lines.append(
                    f"| {clean_cell(error.get('StatusCode'))} | "
                    f"{clean_cell(error.get('Description'))} |"
                )
            lines.append("")
        usage = node.get("UsageExample", {})
        if isinstance(usage, dict):
            render_code_block(usage.get("HttpCallTemplate"), "http", lines)
            templates = usage.get("Templates", {})
            if isinstance(templates, dict):
                render_code_block(templates.get("HTTP_CURL_V1"), "bash", lines)
        static_usage = node.get("StaticUsageExample")
        if isinstance(static_usage, dict):
            render_code_block(
                static_usage.get("Text"),
                static_usage.get("Language", "bash"),
                lines,
            )
        return

    if node_type == "structurereference":
        render_param_table(node.get("Fields", []), lines, title="Fields")
        example = node.get("Example")
        if isinstance(example, dict):
            render_code_block(example.get("Text"), "json", lines)
        return

    if node_type == "enumreference":
        elements = node.get("Elements", [])
        if elements:
            lines.extend(["### Values", "", "| Value | Description |", "| --- | --- |"])
            for element in elements:
                lines.append(
                    f"| {clean_cell(element.get('Key'))} | {clean_cell(element.get('Value'))} |"
                )
            lines.append("")
        example = node.get("Example")
        if isinstance(example, dict):
            render_code_block(example.get("Text"), "json", lines)
        return

    if node_type == "eventpayloadreference":
        render_param_table(node.get("Fields", []), lines, title="Payload fields")
        return

    if node_type == "eventreference":
        if node.get("Headers"):
            render_param_table(node["Headers"], lines, title="Headers")
        payload = node.get("Payload", {})
        if isinstance(payload, dict):
            render_reference(payload, lines, level + 1)
        for example in node.get("PayloadExamples", []):
            render_code_block(example.get("Text"), example.get("Language", "json"), lines)
        render_response_details(node.get("Response", []), lines)
        return

    if node_type == "typecombinatorcontainerreference":
        cases = node.get("Cases", [])
        if cases:
            lines.extend(
                [
                    "### Cases",
                    "",
                    "| Discriminator | Type | Description |",
                    "| --- | --- | --- |",
                ]
            )
            for case in cases:
                if not isinstance(case, dict):
                    continue
                lines.append(
                    "| "
                    + " | ".join(
                        clean_cell(case.get(key))
                        for key in ("DiscriminatorValue", "DataType", "Description")
                    )
                    + " |"
                )
                initialization = case.get("InitializationExamples")
                if isinstance(initialization, dict):
                    render_code_block(initialization.get("Example"), "json", lines)
            lines.append("")
        return


def render_node(node: object, lines: list[str], level: int, *, skip_page_children: bool) -> None:
    if not isinstance(node, dict):
        return
    node_type = node.get("Type")
    if node_type == "paragraph":
        text = node.get("Text")
        if isinstance(text, str) and text.strip():
            lines.extend([text.strip(), ""])
        return
    if node_type == "codeblock":
        render_code_block(node.get("Text"), node.get("Language"), lines)
        return
    if node_type == "examplecodeblock":
        if node.get("Name"):
            lines.extend([f"**Example: {node['Name']}**", ""])
        render_code_block(node.get("Text"), node.get("Language"), lines)
        return
    if node_type == "compilablecodeblock":
        templates = node.get("Templates", {})
        if isinstance(templates, dict):
            render_code_block(templates.get("HTTP_CURL_V1"), "bash", lines)
        return
    if node_type == "table":
        render_table(node, lines)
        return
    if node_type == "heading":
        lines.extend([heading(str(node.get("Text", "")), level), ""])
        return
    if node_type in {
        "endpointreference",
        "eventreference",
        "structurereference",
        "enumreference",
        "typecombinatorcontainerreference",
    }:
        render_reference(node, lines, level)
        return
    if node_type == "responseinfo":
        render_response_details([node], lines)
        return
    if node_type == "deprecationdetail":
        message = node.get("Message") or node.get("DeprecatedInVersion")
        if message:
            lines.extend([f"> Deprecated: {message}", ""])
        return
    if node_type == "steppedguide":
        for index, step in enumerate(node.get("Steps", []), 1):
            lines.extend([heading(f"Step {index}", level), ""])
            for child in step.get("Nodes", []):
                render_node(child, lines, level + 1, skip_page_children=False)
        return
    if node_type == "codesectionreference":
        for child in node.get("Nodes", []):
            render_node(child, lines, level, skip_page_children=False)
        return

    title = node.get("Title")
    children = node.get("Nodes", [])
    duplicate_wrapper = (
        bool(title)
        and isinstance(children, list)
        and len(children) == 1
        and isinstance(children[0], dict)
        and children[0].get("Title") == title
    )
    if title and not duplicate_wrapper:
        lines.extend([heading(str(title), level), ""])
    child_level = level + (1 if title and not duplicate_wrapper else 0)
    for child in children:
        if skip_page_children and is_page(child):
            child_title = child.get("Title") or child.get("SuggestedLink")
            if child_title:
                lines.extend([f"See **{child_title}** in its own indexed page.", ""])
            continue
        render_node(child, lines, child_level, skip_page_children=skip_page_children)


def slug_for(link: str) -> str:
    route = unquote(link[3:]).strip("/") or "root"
    slug = re.sub(r"[^a-z0-9]+", "_", route.lower()).strip("_")[:100] or "page"
    digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:10]
    return f"{slug}_{digest}.md"


def render_page(link: str, node: dict, source_url: str) -> tuple[Path, str]:
    title = node.get("Title") or unquote(link.rsplit("/", 1)[-1])
    lines = [
        f"<!-- source: {source_url} -->",
        f"<!-- virtual route: {link} -->",
        "",
        f"# {title}",
        "",
    ]
    for child in node.get("Nodes", []):
        render_node(child, lines, 2, skip_page_children=True)
    text = "\n".join(lines).strip() + "\n"
    return OUTPUT_DIR / slug_for(link), text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="render only the first N virtual pages",
    )
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")

    asset_url = load_asset_url()
    document = fetch_document(asset_url)
    pages = iter_pages(document["Sections"])
    if args.limit:
        pages = pages[: args.limit]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Rendering {len(pages)} Mist API virtual pages -> {OUTPUT_DIR}")

    saved = 0
    skipped = 0
    errors: list[str] = []
    parser_errors: list[str] = []
    for index, (link, node) in enumerate(pages, 1):
        try:
            target, text = render_page(link, node, asset_url)
        except (OSError, TypeError, ValueError) as exc:
            parser_errors.append(f"PARSER_ERROR {link}: {exc}")
            continue
        if args.skip_existing and target.exists():
            skipped += 1
            continue
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            errors.append(f"ERROR {link}: {exc}")
            continue
        saved += 1
        if saved % 50 == 0 or saved == 1 or saved == len(pages):
            print(f"  [{index}/{len(pages)}] {target.name} ({len(text)} chars)")

    report = write_scrape_report(
        "mist_api_docs",
        inventory_path=URLS_PATH if URLS_PATH.exists() else None,
        document_count=len(pages),
        ok=saved,
        skipped=skipped,
        errors=errors,
        parser_errors=parser_errors,
        extra={"asset_url": asset_url, "output_dir": str(OUTPUT_DIR)},
    )
    failed = len(errors) + len(parser_errors)
    print(
        f"Done. {saved} saved, {skipped} skipped, {failed} errors. "
        f"Report: {report}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
