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

The workflows are covered too, and that is not decoration. The gate that
decides whether a release ships lives in ``.github/workflows/*.yml``, not in
prose, and it was the one place a markdown-only guard could never look: CI
and the release job enforced ``--min-tools 6703`` for as long as the docs
promised 6711, and nothing failed. A workflow must therefore take the floor
from ``scripts/validate_release.py``'s canonical default rather than type a
second copy of a published number.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

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


def _current_doc_files() -> list[Path]:
    """Every tracked markdown file except historical release-notes snapshots.

    Tracked, from ``git ls-files``, rather than every ``*.md`` in the working
    tree: walking the tree also swept up gitignored scratch notes and
    vendored virtualenv documentation, so a local plan file quoting the old
    floor could fail a guard about this repository's published docs.
    """
    output = subprocess.check_output(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        text=True,
    )
    return [
        REPO_ROOT / line
        for line in output.splitlines()
        if not Path(line).name.startswith("release-notes-")
    ]


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


WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_VALIDATE_RELEASE = "scripts/validate_release.py"
_MIN_TOOLS_FLAG = re.compile(r"--min-tools\s+(\S+)")


def _workflow_run_steps() -> list[tuple[Path, str]]:
    """Every ``run:`` script in every workflow, parsed rather than grepped."""
    steps: list[tuple[Path, str]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps", ()):
                if "run" in step:
                    steps.append((path, step["run"]))
    return steps


def test_validate_release_takes_its_default_floor_from_the_canonical_fact():
    """Dropping ``--min-tools`` from the workflows only works if this holds.

    The floor a workflow enforces by omission is
    ``validate_release._DEFAULT_MIN_TOOLS``; if that stopped tracking
    ``tools.platform_backend_total`` the workflows would silently enforce
    something else again, with no flag left to inspect.
    """
    from scripts import validate_release

    assert validate_release._DEFAULT_MIN_TOOLS == EXPECTED_MIN_TOOLS_FLOOR


def test_no_workflow_types_a_tool_floor_that_can_drift_from_the_fact():
    """The enforced gate must read the canonical fact, not restate it.

    This is the check whose absence let ``--min-tools 6703`` sit in both
    ``ci.yml`` and ``release-artifacts.yml`` while every doc promised 6711:
    the markdown guard above covers the *documentation of* the gate, and the
    gate itself is YAML.
    """
    problems = []
    invocations = []
    for path, run in _workflow_run_steps():
        if _VALIDATE_RELEASE not in run:
            continue
        invocations.append(path)
        match = _MIN_TOOLS_FLAG.search(_normalize(run))
        if match is None:
            continue
        value = match.group(1)
        name = path.relative_to(REPO_ROOT)
        if value.isdigit() and int(value) != EXPECTED_MIN_TOOLS_FLOOR:
            problems.append(
                f"{name} enforces --min-tools {value}, but the canonical "
                f"tools.platform_backend_total in docs/project-facts.json is "
                f"{EXPECTED_MIN_TOOLS_FLOOR}: the release gate is "
                f"{EXPECTED_MIN_TOOLS_FLOOR - int(value)} tool(s) more "
                "permissive than the floor the docs promise operators"
            )
        problems.append(
            f"{name} types a --min-tools value ({value}); drop the flag so the "
            "gate takes docs/project-facts.json's tools.platform_backend_total "
            "from scripts/validate_release.py's default"
        )

    assert invocations, (
        f"No workflow invokes {_VALIDATE_RELEASE} -- the release gate moved and "
        "this guard needs repointing so it keeps watching the real gate."
    )
    assert problems == []
