"""Repository hygiene: no credential-bearing file may ever be tracked again.

This is the regression test for the 2026-08 incident in which four wizard and
hand-edited environment backups were committed to ``main``:

    .env.bak.20260813151653
    .env.bak.pre-hostname.20260813151929
    .env.broken.20260814123843
    .env.truncated-incident.20260813165839

They carried a live ClearPass API token, a prior Mist API token, private
RFC1918 lab base URLs, and a read-write access profile with every platform
write gate enabled. The root cause was narrow: ``.gitignore`` listed only
``.env``, so every timestamped sibling that ``scripts/setup_wizard.py`` (and
manual editing) drops next to it was tracked. The files even carried a
"This file is gitignored" header while fully committed, which is exactly the
kind of false assurance a test has to replace.

These tests assert on ``git ls-files`` -- the tracked set -- rather than on
the working tree, because the working tree is where credentials are *supposed*
to live. A local ``.env`` full of real tokens must keep passing.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Value-bearing assignments we refuse to track. Each pattern captures the value
# so the assertion can report the key without echoing the secret into CI logs.
_SECRET_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY|CLIENT_SECRET))"
    r"\s*=\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)

# Anything that is obviously a stand-in rather than a credential. Markers are
# matched anywhere in the value, not just at the start: real code writes things
# like PLACEHOLDER_SECRET = "__runtime_secret_placeholder__", and anchoring at
# the start flags those as live credentials.
_PLACEHOLDER = re.compile(
    r"YOUR[_-]"
    r"|CHANGE[_-]?ME"
    r"|REPLACE"
    r"|PLACEHOLDER"
    r"|EXAMPLE"
    r"|DUMMY"
    r"|FAKE"
    r"|SAMPLE"
    r"|REDACTED"
    r"|NOT[_-]?A[_-]?REAL"
    r"|x{4,}"
    r"|\$\{"        # shell/CI interpolation
    r"|\{\{"        # template interpolation
    r"|^[\"']?<",   # <angle-bracket-placeholder>
    re.IGNORECASE,
)

# Private lab ranges must not be published as defaults either: they disclose
# internal topology and were part of the same incident.
_RFC1918_URL = re.compile(
    r"https?://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})",
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return _tracked_files()


def test_no_env_files_are_tracked(tracked: list[str]) -> None:
    """Only ``*.example`` env templates may be tracked.

    The incident files all matched ``.env.*``; ``.gitignore`` now carries that
    glob with explicit ``!*.example`` negations.
    """
    offenders = [
        name
        for name in tracked
        if (Path(name).name == ".env" or Path(name).name.startswith(".env."))
        and not name.endswith(".example")
    ]
    assert offenders == [], (
        "environment files must never be tracked (they carry live tokens); "
        f"remove and purge from history: {offenders}"
    )


def test_gitignore_covers_env_backup_variants() -> None:
    """``git check-ignore`` must reject the real filenames from the incident.

    Asserting on the ignore *behaviour* rather than on the text of
    ``.gitignore`` means reordering or reformatting the file cannot silently
    reopen the hole.
    """
    must_be_ignored = [
        ".env",
        ".env.bak.20260813151653",
        ".env.bak.pre-hostname.20260813151929",
        ".env.broken.20260814123843",
        ".env.truncated-incident.20260813165839",
        ".env.local",
        ".env.production",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT,
        input="\n".join(must_be_ignored),
        capture_output=True,
        text=True,
    )
    ignored = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    missing = [name for name in must_be_ignored if name not in ignored]
    assert missing == [], f".gitignore must ignore these credential paths: {missing}"

    # The escape hatch for templates must still work, or contributors will
    # start deleting the glob instead of using it.
    template = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=ROOT,
        capture_output=True,
    )
    assert template.returncode != 0, ".env.example must remain trackable as a template"


def test_no_tracked_file_assigns_a_real_looking_secret(tracked: list[str]) -> None:
    """Scan every tracked text file for credential-shaped assignments."""
    offenders: list[str] = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to parse
        for match in _SECRET_ASSIGNMENT.finditer(text):
            value = match.group("value").strip().strip("\"'")
            if not value or _PLACEHOLDER.search(match.group("value").strip()):
                continue
            # Short values cannot be a usable API credential; long ones can.
            if len(value) < 16:
                continue
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{name}:{line}: {match.group('key')} (len={len(value)})")
    assert offenders == [], (
        "tracked files must not assign real-looking credentials; "
        f"use a placeholder or move the value into an ignored .env: {offenders}"
    )


def _digest_pinned_vendor_files() -> frozenset[str]:
    """Repo-relative paths of the vendored documents covered by a digest pin.

    Derived from ``vendor/openapi/MANIFEST.json`` at test time, never
    hardcoded, so vendoring another spec needs no edit here — and, more
    importantly, a file dropped into ``vendor/`` without a manifest entry is
    undeclared, therefore unpinned, therefore scanned. An exemption has to be
    earned by a digest; it is not inherited from a directory name.
    """
    manifest = ROOT / "vendor" / "openapi" / "MANIFEST.json"
    try:
        specs = json.loads(manifest.read_text(encoding="utf-8"))["specs"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return frozenset()
    return frozenset(
        f"vendor/openapi/{entry['file']}"
        for entry in specs
        if isinstance(entry, dict) and entry.get("file")
    )


def test_no_tracked_file_hardcodes_private_lab_urls(tracked: list[str]) -> None:
    """Private RFC1918 endpoints disclose internal topology; keep them local.

    The disclosure this guards against is *ours*: a lab address left in code or
    config that we published. ``tests/`` and ``docs/`` are out of scope because
    they legitimately discuss private ranges as examples.

    The vendored corpus is exempt only where a digest says it must be. The Mist
    OpenAPI spec carries ``https://10.3.5.1:8080/about`` as an upstream
    ``examples`` value on a deprecated synthetic-test schema — Juniper's own
    illustration, already public at the pinned commit, revealing nothing about
    our network. Editing it out is not available to us: every byte is pinned to
    an upstream SHA-256, so the corpus would stop verifying. Scanning it is
    also redundant as a smuggling check, since
    ``tests/unit/test_vendor_corpus.py`` proves the file hashes to a digest an
    upstream published — stronger than any regex.

    That argument covers the *payloads* and nothing else. ``MANIFEST.json`` and
    ``NOTICE.md`` are first-party files we author and keep editing, with no
    digest over them, so they stay scanned like any other source file. The
    exempt set is therefore the ``file`` values the manifest declares, not the
    ``vendor/`` tree.
    """
    exempt = _digest_pinned_vendor_files()
    offenders: list[str] = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        if name.startswith(("tests/", "docs/")) or name in exempt:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _RFC1918_URL.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{name}:{line}: {match.group(0)}")
    assert offenders == [], (
        f"tracked files must not hardcode private lab endpoints: {offenders}"
    )
