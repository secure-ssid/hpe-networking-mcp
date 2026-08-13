from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DIAGRAM_SOURCE_DIR = REPO_ROOT / "docs" / "diagrams"
DIAGRAM_DIR = REPO_ROOT / "docs" / "assets" / "diagrams"

# Credential-free/local terminal transcripts covered by docs-terminal-assets.
TERMINAL_STEMS = (
    "terminal-setup-wizard-completion",
    "terminal-doctor-success",
    "terminal-http-router-startup",
)
TERMINAL_MAX_LINE_LENGTH = 72
FORBIDDEN_CREDENTIAL_SNIPPETS = (
    "client_secret",
    "api_key=",
    "Authorization: Bearer ey",
    "central_account:",
    "glp_account:",
    "password:",
    str(Path.home()),
)


def test_rendered_diagrams_are_fresh():
    subprocess.run(
        [sys.executable, "scripts/render_docs_diagrams.py", "--check"],
        cwd=REPO_ROOT,
        check=True,
    )


def test_rendered_diagrams_have_accessible_svg_roots():
    diagrams = sorted(DIAGRAM_DIR.glob("*.svg"))

    assert diagrams
    for path in diagrams:
        root = ET.parse(path).getroot()
        assert root.attrib["role"] == "img"
        assert root.attrib["aria-labelledby"] == "title desc"
        assert len(root.attrib["data-source-sha256"]) == 64
        children = list(root)
        assert children[0].tag.endswith("title")
        assert children[0].text
        assert children[1].tag.endswith("desc")
        assert children[1].text


def test_terminal_transcripts_pair_with_svgs():
    """Every credential-free terminal transcript source has exactly one rendered SVG."""
    for stem in TERMINAL_STEMS:
        source = DIAGRAM_SOURCE_DIR / f"{stem}.term"
        output = DIAGRAM_DIR / f"{stem}.svg"
        assert source.exists(), f"missing terminal transcript source: {source}"
        assert output.exists(), f"missing rendered terminal SVG: {output}"


def test_terminal_svgs_are_fresh_against_their_sources():
    """The committed SVG hash must match the current transcript source content."""
    for stem in TERMINAL_STEMS:
        source = DIAGRAM_SOURCE_DIR / f"{stem}.term"
        output = DIAGRAM_DIR / f"{stem}.svg"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        svg = output.read_text()
        assert f'data-source-sha256="{digest}"' in svg, f"{output} is stale relative to {source}"


def test_terminal_svgs_have_accessible_roots():
    for stem in TERMINAL_STEMS:
        root = ET.parse(DIAGRAM_DIR / f"{stem}.svg").getroot()
        assert root.attrib["role"] == "img"
        assert root.attrib["aria-labelledby"] == "title desc"
        children = list(root)
        assert children[0].tag.endswith("title")
        assert children[0].text
        assert children[1].tag.endswith("desc")
        assert children[1].text


def test_terminal_svgs_are_responsive():
    """Terminal SVGs must scale to the viewport instead of rendering at a fixed pixel size."""
    for stem in TERMINAL_STEMS:
        path = DIAGRAM_DIR / f"{stem}.svg"
        root = ET.parse(path).getroot()
        assert root.attrib.get("width") == "100%"
        assert "height" not in root.attrib
        assert root.attrib.get("viewBox")
        assert root.attrib.get("preserveAspectRatio")
        style = root.attrib.get("style", "")
        assert "max-width" in style
        assert "height: auto" in style


def test_terminal_transcripts_use_readable_line_lengths():
    """Lines stay short enough to remain legible once the SVG scales down on mobile."""
    for stem in TERMINAL_STEMS:
        source = DIAGRAM_SOURCE_DIR / f"{stem}.term"
        for line in source.read_text().splitlines():
            assert len(line) <= TERMINAL_MAX_LINE_LENGTH, (
                f"{source.name} line exceeds {TERMINAL_MAX_LINE_LENGTH} columns: {line!r}"
            )


def test_terminal_transcripts_use_fake_or_no_credentials():
    """Terminal examples must stay credential-free: no real secrets, tokens, or local paths."""
    for stem in TERMINAL_STEMS:
        text = (DIAGRAM_SOURCE_DIR / f"{stem}.term").read_text()
        for snippet in FORBIDDEN_CREDENTIAL_SNIPPETS:
            assert snippet not in text, f"{stem}.term appears to contain a credential-like value"


def test_visual_styles_keep_mobile_and_accessibility_rules():
    css = (REPO_ROOT / "docs" / "assets" / "css" / "style.scss").read_text()

    assert "@media (max-width: 42rem)" in css
    assert "rgb(7 16 32 / 8%)" not in css
    assert ".docs-callout--safe" in css
    assert ".docs-callout--warning" in css
    assert ".docs-figure" in css
    assert "min-width: 0" in css
