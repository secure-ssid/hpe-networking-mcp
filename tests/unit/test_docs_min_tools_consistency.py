"""Guard against the documented ``--min-tools`` floor value drifting.

``scripts/validate_release.py --catalog-products all --strict-tool-index
--min-tools <N>`` is the release gate every current doc
(README, CONTRIBUTING, docs/README, getting-started) copy/pastes as the
"validate the complete backend catalog" example. ``<N>`` is a platform API
backend *compatibility floor* -- the minimum acceptable tool count, checked
against the complete registered catalog (platform API tools plus the
credential-free ``design-core``/``interop-core`` backends) -- not a claim
that ``<N>`` is itself the complete catalog size. Conflating the two is the
exact drift this module exists to catch, so the floor is derived from
``docs/project-facts.json``'s ``tools.platform_backend_total`` and the
complete total from its ``tools.registered_total`` rather than re-typed
here.

Every doc that ships the command must also carry the floor label, so a
reader copy/pasting it cannot come away believing the floor is the complete
backend total.

Dated release-notes snapshots (``docs/release-notes-*.md``) are historical
records of the floor at release time and are intentionally excluded: they
must stay frozen, not track future catalog growth.
"""

from __future__ import annotations

import re
from pathlib import Path

from hpe_networking_mcp.pipeline import project_facts

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED = project_facts.load()

#: The platform API backend compatibility floor -- docs/tool-catalog.md's
#: "Platform API backend total" row and docs/capability-gap-matrix.md's Total
#: row, both derived from the same canonical fact. Deliberately the
#: platform-only subtotal, not the complete registered catalog: a floor set
#: at the platform-only total still holds as a valid lower bound once the two
#: credential-free local backends are added.
EXPECTED_MIN_TOOLS_FLOOR = TRACKED["tools"]["platform_backend_total"]

#: The complete registered backend total the floor must never be confused
#: with (platform API tools + design-core + interop-core).
COMPLETE_BACKEND_TOTAL = TRACKED["tools"]["registered_total"]

_COMMAND_PATTERN = re.compile(
    r"--catalog-products all --strict-tool-index --min-tools (\d+)"
)

#: The required disambiguating label, as committed in every doc that ships
#: the command above. Matched against whitespace-normalized text so the
#: sentence may wrap across Markdown lines.
_LABEL_PATTERN = re.compile(
    r"`--min-tools (\d+)` is the platform API compatibility floor "
    r"\(the ([\d,]+) vendor-facing platform API tools\), "
    r"not the complete registered backend total of ([\d,]+)"
)

_EXCLUDED_TOP_LEVEL_DIRS = {".git", "data", "dist", "node_modules"}


def _current_doc_files() -> list[Path]:
    """Every tracked markdown file except historical release-notes snapshots."""
    docs = []
    for path in sorted(REPO_ROOT.rglob("*.md")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
            continue
        if rel.name.startswith("release-notes-"):
            continue
        docs.append(path)
    return docs


def _normalize(text: str) -> str:
    return " ".join(text.split())


def test_complete_catalog_min_tools_examples_match_current_floor():
    checked = []
    for path in _current_doc_files():
        text = path.read_text(encoding="utf-8")
        matches = _COMMAND_PATTERN.findall(text)
        for value in matches:
            assert int(value) == EXPECTED_MIN_TOOLS_FLOOR, (
                f"{path.relative_to(REPO_ROOT)} documents "
                f"--min-tools {value}, expected "
                f"{EXPECTED_MIN_TOOLS_FLOOR} (the platform API backend total "
                "from docs/project-facts.json). Update the example, or "
                "regenerate docs/project-facts.json alongside "
                "docs/capability-gap-matrix.md and docs/tool-catalog.md if "
                "the catalog legitimately changed."
            )
            checked.append(path)

    assert checked, (
        "No current doc matched the complete-catalog validate_release.py "
        "example command -- the wording likely changed and this regression "
        "test needs its pattern updated so it keeps guarding real drift."
    )


def test_every_doc_shipping_the_command_labels_the_floor_unambiguously():
    """The floor must never read as the complete backend total.

    A bare ``--min-tools 6703`` next to prose about "the complete backend
    catalog" is exactly how the platform-API subtotal got published as the
    complete total. Any doc that ships the copy/pasteable command must also
    state, in canonical numbers, that the value is the platform API
    compatibility floor and that the complete registered total is larger.
    """
    problems = []
    for path in _current_doc_files():
        text = path.read_text(encoding="utf-8")
        if not _COMMAND_PATTERN.search(text):
            continue
        match = _LABEL_PATTERN.search(_normalize(text))
        if match is None:
            problems.append(f"{path.relative_to(REPO_ROOT)}: no platform API floor label")
            continue
        floor, floor_repeat, complete = match.groups()
        if {int(floor), int(floor_repeat.replace(",", ""))} != {EXPECTED_MIN_TOOLS_FLOOR}:
            problems.append(
                f"{path.relative_to(REPO_ROOT)}: floor label says "
                f"{floor}/{floor_repeat}, expected {EXPECTED_MIN_TOOLS_FLOOR}"
            )
        if int(complete.replace(",", "")) != COMPLETE_BACKEND_TOTAL:
            problems.append(
                f"{path.relative_to(REPO_ROOT)}: floor label calls the complete backend "
                f"total {complete}, expected {COMPLETE_BACKEND_TOTAL:,}"
            )

    assert problems == []


def test_the_floor_stays_below_the_complete_backend_total():
    """A floor at or above the complete total would be a different claim."""
    assert EXPECTED_MIN_TOOLS_FLOOR < COMPLETE_BACKEND_TOTAL


def test_current_docs_referencing_min_tools_use_the_complete_catalog_form():
    """Any current doc mentioning --min-tools uses the command or the label.

    Catches a doc that adds a bare ``--min-tools N`` example without the
    accompanying ``--catalog-products all --strict-tool-index``
    flags, which would otherwise silently skip the check above. The inline
    floor label checked above is the only other permitted form.
    """
    bare_pattern = re.compile(r"--min-tools \d+")
    for path in _current_doc_files():
        text = _normalize(path.read_text(encoding="utf-8"))
        bare_matches = bare_pattern.findall(text)
        full_matches = _COMMAND_PATTERN.findall(text)
        label_matches = _LABEL_PATTERN.findall(text)
        assert len(bare_matches) == len(full_matches) + len(label_matches), (
            f"{path.relative_to(REPO_ROOT)} has a --min-tools mention that is "
            "neither the full --catalog-products all --strict-tool-index "
            "command nor the documented platform API "
            "compatibility floor label; add one of those forms or update "
            "this test."
        )
