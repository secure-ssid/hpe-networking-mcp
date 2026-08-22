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


DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
# `FROM <image> AS <stage>`, external references only -- `FROM builder AS …`
# has no registry reference to pin.
FROM_LINE = re.compile(r"^FROM\s+(?P<ref>\S+)\s+AS\s+(?P<stage>\S+)\s*$", re.MULTILINE)


def _external_from_lines() -> dict[str, str]:
    stages = {m.group("stage") for m in FROM_LINE.finditer(DOCKERFILE.read_text())}
    return {
        m.group("stage"): m.group("ref")
        for m in FROM_LINE.finditer(DOCKERFILE.read_text())
        if m.group("ref") not in stages
    }


def test_builder_and_runtime_share_one_interpreter_reference():
    """One interpreter version, one CVE surface.

    `ARG PYTHON_VERSION` used to guarantee this structurally, but Dependabot's
    Docker parser cannot match a tag through `${VAR}` and skips the line
    entirely, so the references had to be inlined to be updatable at all. That
    trades a structural guarantee for two literal strings a hand edit can
    drift apart -- this test is what replaces it.
    """
    refs = _external_from_lines()
    assert refs["builder"] == refs["runtime"], (
        "builder and runtime must resolve to a byte-identical image reference"
    )


def test_external_base_images_are_digest_pinned_and_literal():
    """A floating tag means two builds a week apart are not the same image.

    The reference must also be literal: `${VAR}` is invisible to Dependabot,
    so an ARG-indirected pin would never receive a bump PR.
    """
    for stage, ref in _external_from_lines().items():
        assert "@sha256:" in ref, f"{stage} base image is not digest-pinned: {ref}"
        assert "$" not in ref, f"{stage} base image is ARG-indirected: {ref}"
        tag, _, digest = ref.partition("@")
        assert ":" in tag, f"{stage} keeps no human-readable tag beside {digest}"


def test_dependabot_watches_the_dockerfile():
    """A digest pin with no bump mechanism silently stops receiving security
    updates. The pin and this ecosystem entry are one change."""
    config = yaml.safe_load(DEPENDABOT.read_text())
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert "docker" in ecosystems, (
        "digest-pinned base images require a `docker` Dependabot ecosystem"
    )


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


def test_entrypoint_script_is_syntactically_valid_bash(functional_bash):
    result = subprocess.run(
        [functional_bash, "-n", str(ENTRYPOINT)],
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


def test_router_service_declares_no_depends_on():
    """`... --profile router up -d mcp-router` must start the router alone.

    `redis` and `ollama` sit in docker-compose.yml's default profile, so a
    profile-only invocation starts them too -- and the default image installs
    neither the `redis` client nor the LanceDB stack, so they would run
    unused. Naming the service is the documented command, and a `depends_on`
    here would silently drag them back in.
    """
    service = _load_yaml(ROUTER_OVERLAY)["services"]["mcp-router"]
    assert "depends_on" not in service


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


def _router_volumes() -> list[str]:
    return _load_yaml(ROUTER_OVERLAY)["services"]["mcp-router"]["volumes"]


def _host_mount_problems(volumes: list[str]) -> list[str]:
    """Every rule a host bind mount in the router overlay has to obey.

    Kept as a pure function over a volume list so the rules can be exercised
    against deliberately bad configurations as well as the real one. The
    overlay currently declares no host mounts at all -- the default image has
    no LanceDB stack, so mounting a corpus into it would advertise a
    capability that is not there -- and these rules are what any future mount
    has to satisfy.
    """
    def tracked(relative: str) -> list[str]:
        return subprocess.run(
            ["git", "ls-files", "--", relative],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

    problems: list[str] = []
    for volume in volumes:
        source, target, *rest = volume.split(":")
        if not source.startswith("./"):
            continue
        relative = source.removeprefix("./")
        if (rest[-1] if rest else "") != "ro":
            # A writable bind mount lets a compromised container rewrite the
            # operator's own data/ on the host.
            problems.append(f"{source} is not mounted read-only")
        if target != f"/app/{relative}":
            problems.append(f"{source} maps to {target}, not /app/{relative}")
        if target.rstrip("/") == "/app/data" or target == "/app/data/specs.sqlite":
            # /app/data holds the spec index the image builds from
            # vendor/openapi/. Mounting over it leaves the documented
            # deployment as the one path without the index.
            problems.append(f"{source} shadows the baked spec index at {target}")
        if tracked(relative):
            # A mount must supply artifacts the operator built, never content
            # a clone already carries. data/docs.lance in particular is
            # scraped vendor documentation this project cannot redistribute.
            problems.append(f"{source} is tracked in git, so it is not unpopulated")
    return problems


def test_router_overlay_host_mounts_obey_every_rule():
    assert _host_mount_problems(_router_volumes()) == []


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        ("./data:/app/data:ro", "shadows the baked spec index"),
        ("./data/specs.sqlite:/app/data/specs.sqlite:ro", "shadows the baked spec index"),
        ("./data/docs.lance:/app/data/docs.lance", "not mounted read-only"),
        ("./data/docs.lance:/app/elsewhere:ro", "not /app/data/docs.lance"),
        ("./tests:/app/tests:ro", "not unpopulated"),
    ],
)
def test_host_mount_rules_reject_unsafe_mounts(volume, expected):
    """The rules above are not vacuous: each unsafe shape is actually caught."""
    problems = _host_mount_problems([volume, "router_state:/app/state"])
    assert any(expected in problem for problem in problems), problems


def test_router_overlay_mounts_nothing_over_the_baked_spec_index():
    """The image builds `data/specs.sqlite` from `vendor/openapi/` and ships it.

    The overlay used to mount `./data:/app/data:ro`, which hid that file and
    made the documented production deployment the one path that ran without
    the index the image had just built.
    """
    assert "/app/data/specs.sqlite" in DOCKERFILE.read_text(), (
        "the Dockerfile no longer bakes the spec index; this guard is stale"
    )
    assert not [
        volume
        for volume in _router_volumes()
        if volume.split(":")[1].startswith("/app/data")
    ]


def test_default_image_installs_no_optional_extras():
    """RAG needs a corpus that cannot ship in the image, so its ~700 MB of
    LanceDB/ONNX dependencies are opt-in rather than paid for by every user.

    `--build-arg INSTALL_EXTRAS=ingestion` is the documented way back; the
    default has to stay empty and has to reach both `uv sync` invocations,
    or the dependency layer and the project layer disagree.
    """
    text = DOCKERFILE.read_text()
    assert 'ARG INSTALL_EXTRAS=""' in text
    syncs = [line for line in text.splitlines() if "uv sync --frozen" in line]
    assert len(syncs) == 2, syncs
    assert text.count("for extra in ${INSTALL_EXTRAS}") == 2


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
