#!/usr/bin/env python3
"""Render Mermaid diagrams and terminal transcripts, verify committed SVG freshness.

Two documentation source kinds live under docs/diagrams/ and render to
docs/assets/diagrams/ with the same accessible, responsive SVG contract:

- ``*.mmd``  Mermaid diagram sources rendered with the mermaid CLI.
- ``*.term`` Plain-text terminal transcripts rendered directly to SVG, used
  for credential-free/local command output examples (no live API calls).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "docs" / "diagrams"
OUTPUT_DIR = ROOT / "docs" / "assets" / "diagrams"
MERMAID_CLI = "@mermaid-js/mermaid-cli@11.4.1"
SVG_OPEN_RE = re.compile(r"<svg\b([^>]*)>")
HASH_RE = re.compile(r'data-source-sha256="([0-9a-f]{64})"')

ACCESSIBILITY = {
    "client-transport-choice": (
        "Choose an MCP client transport",
        "Decision tree for choosing stdio, local streamable HTTP, or protected non-loopback HTTP.",
    ),
    "data-index-flow": (
        "Documentation and index data flow",
        "Official documentation, OpenAPI, and advisory sources flow through ingestion into docs.lance, specs.sqlite, and tools.lance for RAG and router discovery.",
    ),
    "how-mcp-rag-works": (
        "How MCP and RAG work",
        "An MCP client uses find_tool, invoke_read_tool, and invoke_tool. Reads reach local RAG indexes or live vendor APIs; writes reach vendor APIs only.",
    ),
    "discovery-dispatch": (
        "Tool discovery and dispatch sequence",
        "Sequence from a user request through router tool discovery, backend dispatch, a bounded vendor API request, and the normalized response.",
    ),
    "optional-products-map": (
        "Choose an optional product backend",
        "Map from common network automation goals to the ClearPass, Mist, Apstra, ArubaOS 8, EdgeConnect, UXI, and Axis backends.",
    ),
    "rag-query-routing": (
        "RAG and live-tool query routing",
        "Exact API questions use lookup_api, advisory IDs use lookup_advisory, how-to questions use ask_docs, and live device state uses find_tool then invoke_read_tool.",
    ),
    "quickstart-journey": (
        "hpe-networking-mcp quickstart journey",
        "Six steps from cloning hpe-networking-mcp through setup, doctor checks, MCP connection, tool discovery, and a safe read-only call.",
    ),
    "router-safety-flow": (
        "Router discovery and safety flow",
        "Decision flow from find_tool through read, diagnostic, write, and destructive dispatch with dry-run, confirmation, and write gates.",
    ),
    "setup-wizard-flow": (
        "Setup wizard phases",
        "Six compact wizard phases: install, credentials, optional products, MCP client configs, tool catalog, and the local doctor.",
    ),
    "runtime-overview": (
        "hpe-networking-mcp runtime overview",
        "High-level flow from a user and MCP client through the low-token router, tool catalog, Central/GLP/RAG, Central Streaming, optional products, local indexes, and vendor APIs.",
    ),
    "troubleshooting-tree": (
        "hpe-networking-mcp troubleshooting decision tree",
        "Diagnostic path from the local doctor through setup, authentication, transport, catalog, and RAG index checks.",
    ),
    "transport-deployment": (
        "MCP transport and deployment choices",
        "Deployment paths for stdio, local streamable HTTP, and protected non-loopback streamable HTTP connections to the hpe-networking-mcp router.",
    ),
}

# Credential-free/local terminal transcripts. Each source is plain text: the
# first line is the command, blank lines are spacers, and [OK]/[WARN]/[FAIL]
# prefixes get high-contrast status colors. No real credentials or hostnames.
TERMINAL_ACCESSIBILITY = {
    "terminal-setup-wizard-completion": (
        "Setup wizard completion transcript",
        "Terminal transcript of running the guided setup wizard credential-free: "
        "it writes local MCP client configs, runs the doctor, and finishes with "
        "an all-OK setup summary and next steps.",
    ),
    "terminal-doctor-success": (
        "Local doctor success transcript",
        "Terminal transcript of running the local doctor credential-free: zero "
        "failures, one expected credentials warning, and a passing summary "
        "line with no live API calls.",
    ),
    "terminal-http-router-startup": (
        "Local streamable HTTP router startup transcript",
        "Terminal transcript of starting the local streamable HTTP router: the "
        "loopback endpoint, health routes, minimal router mode, and how to "
        "stop the foreground or background process.",
    ),
}

TERMINAL_MAX_LINE_LENGTH = 72
TERMINAL_FONT_SIZE = 20
TERMINAL_CHAR_WIDTH = 12.2  # approx monospace glyph advance at TERMINAL_FONT_SIZE
TERMINAL_LINE_HEIGHT = 30
TERMINAL_PADDING_X = 32
TERMINAL_PADDING_TOP = 96
TERMINAL_PADDING_BOTTOM = 32
# Sized so TERMINAL_MAX_LINE_LENGTH columns never overflow the frame, keeping
# lines readable (not clipped) once the responsive SVG scales down on mobile.
TERMINAL_WIDTH = round(2 * TERMINAL_PADDING_X + TERMINAL_MAX_LINE_LENGTH * TERMINAL_CHAR_WIDTH)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decorate_svg(svg: str, source: Path) -> str:
    title, description = ACCESSIBILITY[source.stem]
    svg = re.sub(r"<title\b[^>]*>.*?</title>", "", svg, flags=re.DOTALL)
    svg = re.sub(r"<desc\b[^>]*>.*?</desc>", "", svg, flags=re.DOTALL)
    match = SVG_OPEN_RE.search(svg)
    if match is None:
        raise RuntimeError(f"Mermaid output for {source.name} has no <svg> root")
    attrs = match.group(1)
    attrs = re.sub(r'\s(?:role|aria-labelledby|data-source-sha256)="[^"]*"', "", attrs)
    opening = (
        f'<svg{attrs} role="img" aria-labelledby="title desc" '
        f'data-source-sha256="{_digest(source)}">'
    )
    accessible = (
        f"<title id=\"title\">{html.escape(title)}</title>"
        f"<desc id=\"desc\">{html.escape(description)}</desc>"
    )
    return svg[: match.start()] + opening + accessible + svg[match.end() :]


def _terminal_line_style(line: str) -> str:
    stripped = line.strip()
    if line.startswith("$ "):
        return "prompt"
    if stripped.startswith("[OK]"):
        return "ok"
    if stripped.startswith("[WARN]"):
        return "warn"
    if stripped.startswith("[FAIL]"):
        return "fail"
    if not stripped:
        return "blank"
    return "dim"


def _render_terminal_svg(source: Path) -> str:
    title, description = TERMINAL_ACCESSIBILITY[source.stem]
    lines = source.read_text().splitlines()
    for line in lines:
        if len(line) > TERMINAL_MAX_LINE_LENGTH:
            raise SystemExit(
                f"{source.name}: line exceeds {TERMINAL_MAX_LINE_LENGTH} columns "
                f"(mobile readability limit): {line!r}"
            )
    height = TERMINAL_PADDING_TOP + max(len(lines), 1) * TERMINAL_LINE_HEIGHT + TERMINAL_PADDING_BOTTOM
    rows: list[str] = []
    y = TERMINAL_PADDING_TOP + TERMINAL_LINE_HEIGHT - 8
    for line in lines:
        style = _terminal_line_style(line)
        if style != "blank":
            text = html.escape(line)
            rows.append(f'<text x="{TERMINAL_PADDING_X}" y="{y}" class="term-{style}">{text}</text>')
        y += TERMINAL_LINE_HEIGHT
    body = "".join(rows)
    digest = _digest(source)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" '
        f'viewBox="0 0 {TERMINAL_WIDTH} {height}" preserveAspectRatio="xMidYMid meet" '
        f'style="max-width: {TERMINAL_WIDTH}px; height: auto;" role="img" '
        f'aria-labelledby="title desc" data-source-sha256="{digest}">'
        f'<title id="title">{html.escape(title)}</title>'
        f'<desc id="desc">{html.escape(description)}</desc>'
        "<defs><style>"
        f'text {{ font: 400 {TERMINAL_FONT_SIZE}px "SFMono-Regular", Consolas, '
        '"Liberation Mono", monospace; }'
        ".term-shell { fill: #05070d; }"
        ".term-frame { fill: #10182a; stroke: #3a4a63; stroke-width: 2; }"
        ".term-dot-red { fill: #ff5f56; }"
        ".term-dot-amber { fill: #ffbd2e; }"
        ".term-dot-green { fill: #27c93f; }"
        ".term-prompt { fill: #8be9fd; font-weight: 700; }"
        ".term-ok { fill: #6be675; }"
        ".term-warn { fill: #ffd866; }"
        ".term-fail { fill: #ff6e6e; }"
        ".term-dim { fill: #e6ecf7; }"
        "</style></defs>"
        f'<rect width="{TERMINAL_WIDTH}" height="{height}" class="term-shell"/>'
        f'<rect x="12" y="12" width="{TERMINAL_WIDTH - 24}" height="{height - 24}" '
        'rx="14" class="term-frame"/>'
        '<circle cx="40" cy="40" r="7" class="term-dot-red"/>'
        '<circle cx="64" cy="40" r="7" class="term-dot-amber"/>'
        '<circle cx="88" cy="40" r="7" class="term-dot-green"/>'
        f"{body}"
        "</svg>"
    )


def render() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for source in sorted(SOURCE_DIR.glob("*.mmd")):
        if source.stem not in ACCESSIBILITY:
            raise SystemExit(f"Missing accessibility metadata for {source.name}")
        output = OUTPUT_DIR / f"{source.stem}.svg"
        with tempfile.TemporaryDirectory(prefix="hpe-networking-mcp-mermaid-") as temp_dir:
            temporary = Path(temp_dir) / output.name
            subprocess.run(
                [
                    "npx",
                    "--yes",
                    MERMAID_CLI,
                    "--input",
                    str(source),
                    "--output",
                    str(temporary),
                    "--backgroundColor",
                    "transparent",
                    "--theme",
                    "neutral",
                ],
                cwd=ROOT,
                check=True,
            )
            output.write_text(_decorate_svg(temporary.read_text(), source))
    for source in sorted(SOURCE_DIR.glob("*.term")):
        if source.stem not in TERMINAL_ACCESSIBILITY:
            raise SystemExit(f"Missing accessibility metadata for {source.name}")
        output = OUTPUT_DIR / f"{source.stem}.svg"
        output.write_text(_render_terminal_svg(source))


def check() -> None:
    errors: list[str] = []
    mermaid_sources = sorted(SOURCE_DIR.glob("*.mmd"))
    terminal_sources = sorted(SOURCE_DIR.glob("*.term"))
    stem_overlap = {s.stem for s in mermaid_sources} & {s.stem for s in terminal_sources}
    if stem_overlap:
        errors.append(f"diagram source stem collision between .mmd and .term: {sorted(stem_overlap)}")
    sources = mermaid_sources + terminal_sources
    expected_outputs = {f"{source.stem}.svg" for source in sources}
    actual_outputs = {path.name for path in OUTPUT_DIR.glob("*.svg")}
    if expected_outputs != actual_outputs:
        errors.append(
            f"diagram source/output mismatch: expected={sorted(expected_outputs)} "
            f"actual={sorted(actual_outputs)}"
        )
    terminal_stems = {s.stem for s in terminal_sources}
    for source in sources:
        output = OUTPUT_DIR / f"{source.stem}.svg"
        if not output.exists():
            continue
        svg = output.read_text()
        match = HASH_RE.search(svg)
        if match is None or match.group(1) != _digest(source):
            errors.append(f"{output.relative_to(ROOT)} is stale; rerun {Path(__file__).name}")
        for marker in ('role="img"', 'aria-labelledby="title desc"', "<title", "<desc"):
            if marker not in svg:
                errors.append(f"{output.relative_to(ROOT)} missing {marker}")
        if source.stem in terminal_stems:
            for line in source.read_text().splitlines():
                if len(line) > TERMINAL_MAX_LINE_LENGTH:
                    errors.append(
                        f"{source.relative_to(ROOT)} line exceeds {TERMINAL_MAX_LINE_LENGTH} "
                        "columns (mobile readability limit)"
                    )
            for marker in ('width="100%"', "viewBox="):
                if marker not in svg:
                    errors.append(f"{output.relative_to(ROOT)} is not responsive; missing {marker}")
    if errors:
        raise SystemExit("\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify committed SVG freshness")
    args = parser.parse_args()
    if args.check:
        check()
    else:
        render()
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
