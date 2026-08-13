"""Regression tests for the runtime-config precedence chain: process
environment > ``.env`` > YAML > built-in defaults.

Two independent bugs are covered here:

1. ``hpe_networking_mcp.pipeline.config.load_credentials`` used to call
   ``load_dotenv(override=True)``, which let a stale ``.env`` value
   silently *replace* a value the process environment already set --
   backwards from the documented precedence.
2. ``hpe_networking_mcp.mcp_servers.shared`` used to call
   ``load_dotenv(Path(__file__).resolve().parents[1] / ".env")``, which
   after the src-layout move resolved to ``src/hpe_networking_mcp/.env`` --
   one directory too shallow to ever reach the repository root where
   ``scripts/setup_wizard.py``/``scripts/doctor.py``/
   ``scripts/run_http_router.sh`` all read or write ``.env`` -- so a
   repo-root ``.env`` was silently never loaded before this module's own
   MCP_TRANSPORT/MCP_HOST/etc. reads.

``python-dotenv``'s zero-argument ``load_dotenv()`` locates the file by
walking up from the *calling module's own file location* (not the test's,
and not the current working directory, under a normal -- non "-c"/REPL --
interpreter invocation). That makes ``monkeypatch.chdir`` irrelevant to
where ``load_credentials`` looks for ``.env``, so the "does .env actually
get read" behavior is exercised two ways below:
  - a fast, in-process test that captures the ``override=`` kwarg
    ``load_credentials`` passes to ``load_dotenv`` (the precise bug fixed);
  - a real end-to-end subprocess test that drops a temporary ``.env`` next
    to the repository root (where ``load_credentials``'s and
    ``mcp_servers.shared``'s own upward search actually lands) and confirms
    both the fill-in and the non-override behavior.

No network calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hpe_networking_mcp.pipeline.config as config_module
from hpe_networking_mcp.pipeline.config import load_credentials

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_stray_env(monkeypatch):
    for var in (
        "SOURCE_BASE_URL",
        "SOURCE_CLIENT_ID",
        "SOURCE_CLIENT_SECRET",
        "SOURCE_GLP_WORKSPACE",
        "TARGET_BASE_URL",
        "TARGET_CLIENT_ID",
        "TARGET_CLIENT_SECRET",
        "TARGET_GLP_WORKSPACE",
        "GLP_TOKEN_URL",
        "GLP_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestLoadCredentialsDotenvOverrideFlag:
    """``pipeline.config.load_credentials`` must call ``load_dotenv`` with
    ``override=False`` -- process env wins over .env, not the reverse."""

    def test_load_dotenv_is_called_with_override_false(self, monkeypatch, tmp_path):
        calls: list[dict] = []

        def _fake_load_dotenv(*args, **kwargs):
            calls.append(kwargs)
            return False

        monkeypatch.setattr(config_module, "load_dotenv", _fake_load_dotenv)

        load_credentials(str(tmp_path / "missing-credentials.yaml"))

        assert calls, "load_credentials must call load_dotenv"
        assert calls[0].get("override") is False

    def test_yaml_still_wins_over_defaults_when_env_and_dotenv_are_unset(self, tmp_path):
        creds_path = tmp_path / "credentials.yaml"
        creds_path.write_text(
            "central_account:\n  base_url: https://from-yaml.example.com\n",
            encoding="utf-8",
        )

        creds = load_credentials(str(creds_path))

        assert creds["source"]["base_url"] == "https://from-yaml.example.com"


@pytest.fixture
def _temp_repo_root_dotenv():
    """Create/restore a temporary ``.env`` at the real repository root.

    ``load_dotenv()`` with no explicit path (used by both
    ``pipeline.config.load_credentials`` and ``mcp_servers.shared``) walks
    upward from the calling module's own file location, which for both of
    those modules lands on the real repository root -- not a pytest
    ``tmp_path`` -- so an end-to-end test of "is .env actually read" has to
    use that real location. ``.env`` is git-ignored; any pre-existing local
    file is restored afterward.

    Swapping a *real* developer file is inherently dangerous, so two
    failure modes are guarded explicitly. Both have destroyed a populated
    local ``.env`` in practice:

    1. **Crash safety.** The original contents are copied to an on-disk
       sidecar *before* the marker is written, not just held in memory. If
       the interpreter dies mid-test the sidecar survives, and the next run
       restores from it instead of treating the leftover marker file as the
       developer's real config.
    2. **Concurrency safety.** Two pytest sessions running at once used to
       interleave as snapshot(real) -> snapshot(marker) -> restore(real) ->
       restore(marker), permanently leaving the one-line marker in place.
       An exclusive lock file serializes the swap; if another session holds
       it, the test skips rather than clobbering.
    """
    dotenv_path = REPO_ROOT / ".env"
    sidecar = REPO_ROOT / ".env.pytest-backup"
    lock_path = REPO_ROOT / ".env.pytest-lock"

    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pytest.skip(
            "another pytest session is swapping the repo-root .env "
            f"({lock_path.name} exists); skipping rather than risk clobbering it"
        )
    os.close(lock_fd)

    # A leftover sidecar means a previous run died before restoring; its
    # contents are the developer's real file, not the marker now on disk.
    if sidecar.exists():
        dotenv_path.write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
        sidecar.unlink(missing_ok=True)

    pre_existing = dotenv_path.read_text(encoding="utf-8") if dotenv_path.exists() else None
    if pre_existing is not None:
        sidecar.write_text(pre_existing, encoding="utf-8")

    def _write(marker: str, value: str) -> None:
        dotenv_path.write_text(f"{marker}={value}\n", encoding="utf-8")

    try:
        yield _write
    finally:
        if pre_existing is None:
            dotenv_path.unlink(missing_ok=True)
        else:
            dotenv_path.write_text(pre_existing, encoding="utf-8")
        sidecar.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def _run_subprocess(code: str, *, env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestLoadCredentialsDotenvEndToEnd:
    def test_dotenv_fills_in_when_process_env_unset(self, _temp_repo_root_dotenv):
        _temp_repo_root_dotenv("SOURCE_CLIENT_ID", "from-dotenv")
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_CLIENT_ID"}
        code = textwrap.dedent(
            """
            from hpe_networking_mcp.pipeline.config import load_credentials
            creds = load_credentials("does-not-exist.yaml")
            print(creds["source"]["client_id"])
            """
        )

        output = _run_subprocess(code, env=env)

        assert output == "from-dotenv"

    def test_process_env_wins_over_dotenv(self, _temp_repo_root_dotenv):
        _temp_repo_root_dotenv("SOURCE_CLIENT_ID", "from-dotenv")
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_CLIENT_ID"}
        env["SOURCE_CLIENT_ID"] = "from-process-env"
        code = textwrap.dedent(
            """
            from hpe_networking_mcp.pipeline.config import load_credentials
            creds = load_credentials("does-not-exist.yaml")
            print(creds["source"]["client_id"])
            """
        )

        output = _run_subprocess(code, env=env)

        assert output == "from-process-env"


class TestSharedDotenvLocation:
    """``mcp_servers.shared`` must discover the repo-root ``.env``, not a
    src-layout-relative path, and must not let it override an
    already-exported process env var."""

    def test_repo_root_dotenv_is_discovered(self, _temp_repo_root_dotenv):
        marker = "HPE_MCP_TEST_DOTENV_MARKER"
        _temp_repo_root_dotenv(marker, "repo-root-value")
        env = {k: v for k, v in os.environ.items() if k != marker}
        code = textwrap.dedent(
            f"""
            import os
            import hpe_networking_mcp.mcp_servers.shared  # noqa: F401
            print(os.environ.get({marker!r}, ""))
            """
        )

        output = _run_subprocess(code, env=env)

        assert output == "repo-root-value"

    def test_process_env_wins_over_repo_root_dotenv(self, _temp_repo_root_dotenv):
        marker = "HPE_MCP_TEST_DOTENV_MARKER"
        _temp_repo_root_dotenv(marker, "repo-root-value")
        env = {k: v for k, v in os.environ.items() if k != marker}
        env[marker] = "process-env-value"
        code = textwrap.dedent(
            f"""
            import os
            import hpe_networking_mcp.mcp_servers.shared  # noqa: F401
            print(os.environ.get({marker!r}, ""))
            """
        )

        output = _run_subprocess(code, env=env)

        assert output == "process-env-value"


class TestRepoRootDotenvFixtureSafety:
    """The fixture swaps a real developer file; these guard that swap.

    A concurrent-session race previously left the one-line marker file in
    place permanently, destroying a populated local ``.env``.
    """

    def test_lock_is_released_so_consecutive_tests_can_run(self, _temp_repo_root_dotenv):
        _temp_repo_root_dotenv("SOURCE_CLIENT_ID", "x")
        assert (REPO_ROOT / ".env.pytest-lock").exists()

    def test_second_session_skips_instead_of_clobbering(self):
        lock_path = REPO_ROOT / ".env.pytest-lock"
        dotenv_path = REPO_ROOT / ".env"
        sentinel = "REAL_LOCAL_VALUE=do-not-destroy\n"

        had_dotenv = dotenv_path.exists()
        original = dotenv_path.read_text(encoding="utf-8") if had_dotenv else None
        dotenv_path.write_text(sentinel, encoding="utf-8")
        # Simulate another pytest session mid-swap.
        os.close(os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        try:
            gen = _temp_repo_root_dotenv.__wrapped__()
            # pytest.skip raises Skipped, which derives from BaseException.
            with pytest.raises(BaseException, match="another pytest session"):
                next(gen)
            assert dotenv_path.read_text(encoding="utf-8") == sentinel
        finally:
            lock_path.unlink(missing_ok=True)
            if original is None:
                dotenv_path.unlink(missing_ok=True)
            else:
                dotenv_path.write_text(original, encoding="utf-8")

    def test_leftover_sidecar_from_a_crashed_run_is_restored(self):
        dotenv_path = REPO_ROOT / ".env"
        sidecar = REPO_ROOT / ".env.pytest-backup"
        real = "REAL_LOCAL_VALUE=recovered\n"

        had_dotenv = dotenv_path.exists()
        original = dotenv_path.read_text(encoding="utf-8") if had_dotenv else None
        # State a crashed run leaves behind: marker on disk, real file in sidecar.
        dotenv_path.write_text("SOURCE_CLIENT_ID=from-dotenv\n", encoding="utf-8")
        sidecar.write_text(real, encoding="utf-8")
        try:
            gen = _temp_repo_root_dotenv.__wrapped__()
            write = next(gen)
            # The crashed run's real contents win over the leftover marker.
            assert dotenv_path.read_text(encoding="utf-8") == real
            write("SOURCE_CLIENT_ID", "from-dotenv")
            list(gen)
            assert dotenv_path.read_text(encoding="utf-8") == real
            assert not sidecar.exists()
        finally:
            sidecar.unlink(missing_ok=True)
            (REPO_ROOT / ".env.pytest-lock").unlink(missing_ok=True)
            if original is None:
                dotenv_path.unlink(missing_ok=True)
            else:
                dotenv_path.write_text(original, encoding="utf-8")
