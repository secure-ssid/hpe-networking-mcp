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

#: GitHub only credits a co-author when the trailer carries an address it can
#: resolve to an account -- ``Co-authored-by: Name <name@example.com>``. Both
#: patterns therefore require the angle-bracketed address, so prose that merely
#: names the trailer (this repository documents its own authorship policy)
#: cannot be mistaken for one. Verified against commit 4a91c4d, whose body
#: line-wrapped the token to the start of a line: GitHub added no co-author.
_IDENTITY = rb"(?P<identity>[^\r\n]*?<[^>@\s]+@[^>\s]+>)"

_COAUTHOR = re.compile(rb"(?im)^[ \t]*co-authored-by:[ \t]*" + _IDENTITY + rb"[ \t]*$")

#: Some commits store escaped ``\n`` literals instead of real newlines, so a
#: trailer can hide mid-line where the anchored pattern above cannot see it.
_ESCAPED_COAUTHOR = re.compile(rb"(?i)\\n[ \t]*co-authored-by:[ \t]*" + _IDENTITY)


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ("git", *args), cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout


#: The single committer identity that is exempt on its own: every merge the
#: GitHub web UI creates (merge, squash, or rebase button) is stamped
#: ``GitHub <noreply@github.com>`` while the *author* stays the human who
#: clicked. GitHub builds the contributor list from authors, so this
#: committer renders nobody — provided the author side is clean, which the
#: author guard (and the check below) still enforces. Any other automated
#: committer, and this one paired with an agent-flagged author, still fails.
_GITHUB_WEB_COMMITTER = "github <noreply@github.com>"


def _is_agent(identity: str) -> bool:
    lowered = identity.lower()
    return any(marker in lowered for marker in _AGENT_MARKERS)


def _offenders(history: list[str], index: int) -> list[str]:
    """Sorted unique automated identities at ``index`` (0=author, 1=committer).

    ``history`` rows are ``author\\tcommitter`` identities. The committer leg
    exempts the canonical GitHub web-UI committer (case-insensitive) only
    when the same commit's author is not itself agent-flagged — attribution
    authority stays with the author field.
    """
    found = set()
    for row in history:
        author, _, committer = row.partition("\t")
        identity = (author, committer)[index]
        if (
            index == 1
            and identity.lower() == _GITHUB_WEB_COMMITTER
            and not _is_agent(author)
        ):
            continue
        if _is_agent(identity):
            found.add(identity)
    return sorted(found)


def _synthetic_pr_merge_sha() -> str | None:
    """SHA of the merge commit CI synthesizes for a ``pull_request`` run.

    ``actions/checkout`` checks out ``refs/pull/N/merge`` — a commit GitHub
    creates on the fly, commits as ``GitHub <noreply@github.com>``, and never
    pushes to any branch. It is an artifact of running CI, not repository
    history, and it never renders as a contributor, so counting it would fail
    every pull request while saying nothing about the code under review.
    ``GITHUB_SHA`` is exactly that commit, so drop it by identity.

    The default checkout is depth-1, so its parents are hidden behind a
    shallow graft and cannot be walked to reach the PR's own head — matching
    on the SHA is the only reliable handle. On ``push`` events this returns
    ``None`` and nothing is dropped, so a bot-committed merge that someone
    actually landed is still caught.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return None
    return os.environ.get("GITHUB_SHA") or None


def _log_records(body_format: str) -> list[tuple[str, str]]:
    """``(sha, formatted_body)`` for each commit, minus the CI merge artifact."""
    raw = _git_bytes("log", "HEAD", f"--pretty=format:%H\x1f{body_format}%x00")
    skip = _synthetic_pr_merge_sha()
    records = []
    for chunk in raw.decode("utf-8", errors="replace").split("\0"):
        chunk = chunk.strip("\n")
        if not chunk or "\x1f" not in chunk:
            continue
        sha, _, body = chunk.partition("\x1f")
        if skip and sha == skip:
            continue
        records.append((sha, body))
    return records


@pytest.fixture(scope="module")
def history() -> list[str]:
    """Commit identities in this repository's history, as ``author\tcommitter`` rows."""
    try:
        return [body for _sha, body in _log_records("%an <%ae>\t%cn <%ce>")]
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")


def test_no_commit_is_authored_by_an_agent(history):
    offenders = _offenders(history, 0)

    assert offenders == [], (
        "commits reachable from HEAD are authored by an automated identity, "
        f"which GitHub renders as a repository contributor: {offenders}. "
        "Re-apply the change as your own commit."
    )


def test_no_commit_is_committed_by_an_agent(history):
    offenders = _offenders(history, 1)

    assert offenders == [], (
        f"commits reachable from HEAD carry an automated committer: {offenders}"
    )


@pytest.mark.parametrize(
    "author, committer, expected",
    [
        # A human's web-UI merge: the canonical GitHub committer is exempt,
        # and case-insensitively so.
        ("Ada Lovelace <ada@example.com>", "GitHub <Noreply@GitHub.com>", []),
        # An agent merging through the web UI is caught on the author side,
        # so its committer exemption does not apply.
        (
            "some-bot[bot] <bot@github.com>",
            "GitHub <noreply@github.com>",
            ["GitHub <noreply@github.com>"],
        ),
        # Any other automated committer still fails, human author or not.
        (
            "Ada Lovelace <ada@example.com>",
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            ["dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"],
        ),
    ],
)
def test_committer_exemption_keeps_attribution_with_author(author, committer, expected):
    assert _offenders([f"{author}\t{committer}"], 1) == expected


def test_no_commit_message_co_credits_an_agent():
    try:
        records = _log_records("%B")
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")

    raw = "\n".join(body for _sha, body in records).encode("utf-8", errors="replace")
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


@pytest.mark.parametrize(
    "body",
    [
        b"Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
        b"work\n\nCo-authored-by: Claude <noreply@anthropic.com>\n",
        b"escaped\\nCo-authored-by: some-bot[bot] <bot@github.com>\\n",
    ],
)
def test_a_real_agent_trailer_is_caught(body):
    """A trailer GitHub can resolve to an agent account must still fail the guard."""
    matches = [
        match.group("identity").decode()
        for pattern in (_COAUTHOR, _ESCAPED_COAUTHOR)
        for match in pattern.finditer(body)
    ]

    assert matches, f"no trailer detected in {body!r}"
    assert any(_is_agent(identity) for identity in matches)


@pytest.mark.parametrize(
    "body",
    [
        b"Commits carry no\nCo-authored-by: Copilot trailer, and the API lists one user.\n",
        b"Document the Co-authored-by: Copilot policy in CONTRIBUTING.md",
    ],
)
def test_prose_naming_the_trailer_is_not_an_offender(body):
    """Prose about authorship must not fail the repository that documents it.

    GitHub needs an address it can resolve before it credits anyone, so a
    sentence that merely names the trailer creates no contributor. Commit
    4a91c4d proves it: its body wraps the token onto the start of a line and
    GitHub still lists a single contributor.
    """
    matches = [
        match.group("identity").decode()
        for pattern in (_COAUTHOR, _ESCAPED_COAUTHOR)
        for match in pattern.finditer(body)
    ]

    assert matches == []
