"""No AI agent may appear as a contributor on this repository.

GitHub builds its contributor list from commit *authors* and from
``Co-authored-by:`` trailers. A coding agent that commits under its own
identity, or that appends its own co-author trailer, therefore shows up
next to the humans who are accountable for the code.

This project attributes tooling in prose, not by manufacturing contributor
identities: a human reviewed and shipped every commit, and a human owns the
consequences. These tests fail on any reintroduction of an agent identity.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Substrings that identify a coding agent rather than a person. Matched
#: case-insensitively against the whole ``Name <email>`` identity, so both
#: ``Copilot <copilot@github.com>`` and a bare ``...@users.noreply.github.com``
#: agent address are caught.
_AGENT_MARKERS = (
    "copilot",
    "claude",
    "chatgpt",
    "openai",
    "anthropic",
    "cursor",
    "devin",
    "codex",
    "gemini",
    "[bot]",
    "-bot@",
    "bot@github.com",
    "noreply@github.com",
)

_COAUTHOR = re.compile(rb"(?im)^\s*co-authored-by:\s*(?P<identity>.+?)\s*$")


def _git(*args: str) -> str:
    """Run git in the repository, returning stdout as text."""
    result = subprocess.run(
        ("git", *args),
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _is_agent(identity: str) -> bool:
    lowered = identity.lower()
    return any(marker in lowered for marker in _AGENT_MARKERS)


@pytest.fixture(scope="module")
def _has_git_history() -> bool:
    try:
        _git("rev-parse", "--git-dir")
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")
    return True


def test_no_commit_is_authored_by_an_ai_agent(_has_git_history):
    offenders = sorted(
        {
            line
            for line in _git(
                "log", "--all", "--pretty=format:%an <%ae>"
            ).splitlines()
            if line.strip() and _is_agent(line)
        }
    )

    assert offenders == [], (
        "commits are authored by an AI agent identity, which GitHub renders as "
        f"a repository contributor: {offenders}"
    )


def test_no_commit_is_committed_by_an_ai_agent(_has_git_history):
    offenders = sorted(
        {
            line
            for line in _git(
                "log", "--all", "--pretty=format:%cn <%ce>"
            ).splitlines()
            if line.strip() and _is_agent(line)
        }
    )

    assert offenders == [], (
        f"commits carry an AI agent committer identity: {offenders}"
    )


def test_no_commit_message_co_credits_an_ai_agent(_has_git_history):
    raw = subprocess.run(
        ("git", "log", "--all", "--pretty=format:%B%x00"),
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout

    offenders = sorted(
        {
            match.group("identity").decode("utf-8", errors="replace")
            for match in _COAUTHOR.finditer(raw)
            if _is_agent(match.group("identity").decode("utf-8", errors="replace"))
        }
    )

    assert offenders == [], (
        "Co-authored-by trailers name an AI agent, which GitHub counts as a "
        f"contributor: {offenders}"
    )
