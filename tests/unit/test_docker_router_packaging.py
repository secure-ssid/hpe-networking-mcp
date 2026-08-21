"""Focused tests for the production Docker packaging: Dockerfile, the
router Compose overlay, and the secrets/ example scaffolding.

These are static/structural checks (YAML parsing, file presence, string
assertions) rather than a real `docker build`/`docker run` -- that is
exercised manually against the local Docker daemon (see
docs/production-deployment.md), which CI/unit test runs may not have
available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
ROUTER_OVERLAY = REPO_ROOT / "docker-compose.router.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
SECRETS_DIR = REPO_ROOT / "secrets"
GITIGNORE = REPO_ROOT / ".gitignore"


def test_dockerfile_runs_as_non_root_user():
    text = DOCKERFILE.read_text()
    assert re.search(r"^USER\s+mcp\s*$", text, re.MULTILINE), (
        "Dockerfile must switch to a non-root USER before CMD"
    )
    # The non-root user must not be uid 0 and should not rely on a
    # --system account sharing uid space with real system accounts.
    assert "useradd" in text and "--uid 10001" in text


def test_dockerfile_pins_dependency_resolution_and_skips_runtime_sync():
    text = DOCKERFILE.read_text()
    assert "uv sync --frozen" in text, "dependency install must use the committed lockfile"
    assert "UV_NO_SYNC=1" in text, (
        "runtime image must not attempt network dependency resolution at container start"
    )


def _dockerfile_instructions() -> str:
    """Dockerfile text with full-line comments stripped, so assertions about
    what actually executes aren't tripped up by the file's own prose
    (which necessarily *mentions* credentials.yaml, secrets/, and
    download_indexes.py while explaining why they're absent from the image).
    """
    lines = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    return "\n".join(lines)


def test_dockerfile_never_copies_credentials_or_secrets():
    text = _dockerfile_instructions()
    for forbidden in ("credentials.yaml", "secrets/", "COPY .env", "COPY secrets"):
        assert forbidden not in text, f"Dockerfile must not reference {forbidden!r}"


def test_dockerfile_does_not_download_indexes_at_build_or_start():
    text = _dockerfile_instructions()
    assert "download_indexes" not in text, (
        "prebuilt index download must stay an explicit operator action, "
        "not something the image build or entrypoint runs automatically"
    )


def test_dockerfile_declares_a_healthcheck_against_livez():
    text = DOCKERFILE.read_text()
    assert "HEALTHCHECK" in text
    assert "/livez" in text


def test_dockerignore_excludes_secrets_env_and_state():
    text = DOCKERIGNORE.read_text()
    for pattern in (".env", "config/credentials.yaml", "secrets/**", "state/", "data/"):
        assert pattern in text, f".dockerignore must exclude {pattern!r}"


def test_entrypoint_script_is_syntactically_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_entrypoint_only_fills_in_unset_variables_from_file_secrets():
    text = ENTRYPOINT.read_text()
    # Precedence must match the rest of the codebase: an explicitly-set
    # value always wins over a *_FILE hint.
    assert "_FILE" in text
    assert re.search(r'-n\s+"\$\{!base_var\+set\}"', text), (
        "entrypoint must skip already-set variables before reading a _FILE secret"
    )


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_router_overlay_is_valid_yaml_with_expected_service():
    config = _load_yaml(ROUTER_OVERLAY)
    assert "mcp-router" in config["services"]


def test_router_overlay_service_is_opt_in_via_profile():
    config = _load_yaml(ROUTER_OVERLAY)
    service = config["services"]["mcp-router"]
    assert service.get("profiles") == ["router"], (
        "the router service must stay behind an explicit Compose profile so "
        "`docker compose up` with no --profile flag is unaffected"
    )


def test_router_overlay_publishes_loopback_only():
    config = _load_yaml(ROUTER_OVERLAY)
    ports = config["services"]["mcp-router"]["ports"]
    for port in ports:
        assert port.startswith("127.0.0.1:"), f"mcp-router exposes {port}"


def test_router_overlay_does_not_redefine_redis_or_ollama():
    config = _load_yaml(ROUTER_OVERLAY)
    assert "redis" not in config["services"]
    assert "ollama" not in config["services"]


def test_router_overlay_uses_file_backed_secrets_not_inline_values():
    config = _load_yaml(ROUTER_OVERLAY)
    secrets = config.get("secrets", {})
    assert secrets, "router overlay must declare Compose secrets"
    for name, definition in secrets.items():
        assert "file" in definition, f"secret {name!r} must be file-backed"
        assert definition["file"].startswith("./secrets/"), (
            f"secret {name!r} must resolve under the gitignored secrets/ directory"
        )

    service = config["services"]["mcp-router"]
    environment = service.get("environment", {})
    for key, value in environment.items():
        assert "BEGIN" not in str(value), f"{key} looks like an embedded credential"
    # Credential material must come from CREDS_PATH / *_FILE indirection,
    # never a literal secret value in `environment:`.
    assert environment.get("CREDS_PATH") == "/run/secrets/credentials_yaml"
    assert environment.get("MCP_HTTP_BEARER_TOKEN_FILE") == "/run/secrets/mcp_http_bearer_token"
    assert "MCP_HTTP_BEARER_TOKEN" not in environment


def test_router_overlay_requires_explicit_allowed_hosts_and_origins():
    config = _load_yaml(ROUTER_OVERLAY)
    environment = config["services"]["mcp-router"]["environment"]
    assert environment["MCP_HOST"] == "0.0.0.0"
    for key in ("MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS"):
        value = environment[key]
        assert value, f"{key} must be set explicitly alongside MCP_HOST=0.0.0.0"
        assert "*" not in value, f"{key} must not use a wildcard"


def test_router_overlay_data_mount_is_read_only_and_starts_unpopulated():
    config = _load_yaml(ROUTER_OVERLAY)
    volumes = config["services"]["mcp-router"]["volumes"]
    data_mount = next(v for v in volumes if v.startswith("./data:"))
    assert data_mount.endswith(":ro"), "data/ must be mounted read-only into the container"


def test_router_overlay_validates_standalone_and_merged_with_docker_cli(tmp_path):
    """Static structural checks above don't catch cross-file Compose
    validation errors (e.g. a dangling `depends_on` on an undefined
    service). Run the real `docker compose config` when the CLI plugin is
    installed, regardless of whether a daemon is reachable -- `config` is a
    pure parse/merge step and doesn't need one."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    docker_compose = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10
    )
    if docker_compose.returncode != 0:
        pytest.skip("docker compose CLI plugin not available")

    standalone = subprocess.run(
        ["docker", "compose", "-f", str(ROUTER_OVERLAY), "--profile", "router", "config"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert standalone.returncode == 0, standalone.stderr

    merged = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(ROUTER_OVERLAY),
            "--profile",
            "router",
            "config",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert merged.returncode == 0, merged.stderr

    default_profile = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(ROUTER_OVERLAY),
            "config",
            "--services",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert default_profile.returncode == 0, default_profile.stderr
    services = set(default_profile.stdout.split())
    assert services == {"redis", "ollama"}, (
        "docker-compose.yml's default (no --profile) service set must stay "
        "unchanged by layering docker-compose.router.yml on top"
    )


def test_secrets_directory_only_tracks_example_and_readme_files():
    example_files = sorted(p.name for p in SECRETS_DIR.glob("*.example"))
    assert example_files, "secrets/ must ship at least one *.example template"
    assert (SECRETS_DIR / "README.md").exists()

    tracked = subprocess.run(
        ["git", "ls-files", "secrets"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for entry in tracked:
        name = Path(entry).name
        assert name == "README.md" or name.endswith(".example"), (
            f"secrets/{entry} is tracked by git but is not a README or *.example template"
        )


def test_gitignore_excludes_real_secret_files():
    text = GITIGNORE.read_text()
    assert "secrets/*" in text
    assert "!secrets/*.example" in text
    assert "!secrets/README.md" in text
