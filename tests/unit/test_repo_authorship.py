"""No bot or AI agent may appear as a contributor on this repository.

GitHub builds the contributor list from the *authors* of commits reachable
from the default branch, and from ``Co-authored-by:`` trailers in their
messages. An agent that commits under its own identity, or that appends its
own co-author trailer, therefore appears next to the humans who are
accountable for the code.

This project credits tooling in prose, not by manufacturing contributor
identities: a human reviewed and shipped every commit, and a human owns the
consequences.

Scope is deliberately ``HEAD``, not ``--all``. The contributor graph is built
from the branch's own history, and a developer who has fetched a bot's pull
request branch should not fail a repository-hygiene test because of a commit
nobody has merged.

To land a dependency-bot change without giving the bot an authorship entry,
apply it as your own commit rather than merging the bot's branch, for example
``git cherry-pick --no-commit <sha>`` followed by your own ``git commit``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Substrings that identify an automated author rather than a person, matched
#: case-insensitively against the whole ``Name <email>`` identity. ``[bot]`` is
#: included because GitHub renders every ``...[bot]`` account in the
#: contributor list exactly like a person.
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
    "bot@github.com",
    "noreply@github.com",
)

_COAUTHOR = re.compile(rb"(?im)^[ \t]*co-authored-by:[ \t]*(?P<identity>.+?)[ \t]*$")

#: Some commits store escaped ``\n`` literals instead of real newlines, so a
#: trailer can hide mid-line where the anchored pattern above cannot see it.
_ESCAPED_COAUTHOR = re.compile(rb"(?i)\\n[ \t]*co-authored-by:[ \t]*(?P<identity>[^\\\r\n]+)")


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ("git", *args), cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout


def _is_agent(identity: str) -> bool:
    lowered = identity.lower()
    return any(marker in lowered for marker in _AGENT_MARKERS)


def _history_ref() -> str:
    """The ref whose history actually belongs to this repository.

    For ``pull_request`` events ``actions/checkout`` checks out a merge commit
    that GitHub synthesizes on the fly, committed by ``GitHub
    <noreply@github.com>``. That commit is never pushed to a branch and never
    renders as a contributor, so counting it would fail every pull request
    while telling us nothing about the repository's real history. Step over it
    to the PR's own head; on ``push`` events (including a real merge commit
    that someone actually landed) ``HEAD`` is used unchanged, so a genuinely
    bot-committed merge is still caught.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return "HEAD"
    parents = _git_bytes("rev-parse", "HEAD^@").decode("utf-8", errors="replace").split()
    committer = (
        _git_bytes("log", "-1", "HEAD", "--pretty=format:%cn <%ce>")
        .decode("utf-8", errors="replace")
        .strip()
        .lower()
    )
    if len(parents) == 2 and committer == "github <noreply@github.com>":
        return "HEAD^2"
    return "HEAD"


@pytest.fixture(scope="module")
def history() -> list[str]:
    """Commit identities in this repository's history, as ``author\tcommitter`` rows."""
    try:
        raw = _git_bytes("log", _history_ref(), "--pretty=format:%an <%ae>\t%cn <%ce>")
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")
    return [line for line in raw.decode("utf-8", errors="replace").splitlines() if line]


def test_no_commit_is_authored_by_an_agent(history):
    offenders = sorted({row.split("\t")[0] for row in history if _is_agent(row.split("\t")[0])})

    assert offenders == [], (
        "commits reachable from HEAD are authored by an automated identity, "
        f"which GitHub renders as a repository contributor: {offenders}. "
        "Re-apply the change as your own commit."
    )


def test_no_commit_is_committed_by_an_agent(history):
    offenders = sorted({row.split("\t")[1] for row in history if _is_agent(row.split("\t")[1])})

    assert offenders == [], (
        f"commits reachable from HEAD carry an automated committer: {offenders}"
    )


def test_no_commit_message_co_credits_an_agent():
    try:
        raw = _git_bytes("log", _history_ref(), "--pretty=format:%B%x00")
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")

    offenders = sorted(
        {
            identity
            for pattern in (_COAUTHOR, _ESCAPED_COAUTHOR)
            for match in pattern.finditer(raw)
            if _is_agent(identity := match.group("identity").decode("utf-8", errors="replace"))
        }
    )

    assert offenders == [], (
        "Co-authored-by trailers name an automated identity, which GitHub "
        f"counts as a contributor: {offenders}"
    )
