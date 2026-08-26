"""Contract pins for ``setup_wizard.py --docker`` (slice W1: core + secrets).

Plan: PLANS/DOCKER_SETUP_WIZARD_SLICE_PLAN.md. The K1 manifest fields and the
K2 module-level path constants asserted here are the merge-carried interface
that slices W2 (overlay emission) and W3 (.env audit) consume.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scripts import setup_wizard

REPO_ROOT = Path(setup_wizard.__file__).resolve().parents[1]


@pytest.fixture
def docker_root(tmp_path, monkeypatch):
    """Point ROOT and every ROOT-derived module constant at tmp_path."""
    monkeypatch.setattr(setup_wizard, "ROOT", tmp_path)
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(setup_wizard, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(
        setup_wizard, "BEARER_TOKEN_PATH", secrets_dir / "mcp_http_bearer_token"
    )
    monkeypatch.setattr(
        setup_wizard, "DOCKER_CREDENTIALS_PATH", secrets_dir / "credentials.yaml"
    )
    monkeypatch.setattr(
        setup_wizard, "OVERLAY_PATH", tmp_path / "docker-compose.router.local.yml"
    )
    monkeypatch.setattr(setup_wizard, "ENV_PATH", tmp_path / ".env")
    return tmp_path


def _seed_real_credentials(root: Path) -> None:
    """Write credentials containing no PLACEHOLDER_MARKERS substring."""
    creds = root / "secrets" / "credentials.yaml"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        "central_account:\n"
        "  base_url: https://apigw-prod2.central.arubanetworks.com\n"
        "  client_id: real-central-client-id\n"
        "  client_secret: real-central-client-secret\n"
        "  glp_workspace_id: real-workspace-id\n"
        "glp_account:\n"
        "  base_url: https://apigw-prod2.central.arubanetworks.com\n"
        "  client_id: real-glp-client-id\n"
        "  client_secret: real-glp-client-secret\n"
        "  glp_workspace_id: real-workspace-id\n"
    )


# ---------------------------------------------------------------------------
# K2 — module-level path constants (merge-carried interface)
# ---------------------------------------------------------------------------


def test_k2_module_path_constants_derive_from_root():
    assert setup_wizard.SECRETS_DIR == setup_wizard.ROOT / "secrets"
    assert (
        setup_wizard.BEARER_TOKEN_PATH
        == setup_wizard.SECRETS_DIR / "mcp_http_bearer_token"
    )
    assert (
        setup_wizard.DOCKER_CREDENTIALS_PATH
        == setup_wizard.SECRETS_DIR / "credentials.yaml"
    )
    assert (
        setup_wizard.OVERLAY_PATH == setup_wizard.ROOT / "docker-compose.router.local.yml"
    )
    assert setup_wizard.ENV_PATH == setup_wizard.ROOT / ".env"


# ---------------------------------------------------------------------------
# Happy path: loopback-only --yes run emits both secret files
# ---------------------------------------------------------------------------


def test_docker_yes_emits_secret_files_and_never_echoes_token(docker_root, capsys):
    exit_code = setup_wizard.main(["--docker", "--yes"])

    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    creds_file = docker_root / "secrets" / "credentials.yaml"

    assert exit_code == 0
    assert re.fullmatch(r"[0-9a-f]{64}", token_file.read_text().strip())
    token_bytes = token_file.read_bytes()
    assert token_bytes.endswith(b"\n")
    assert b"\r" not in token_bytes  # W1a: LF-only secret bytes on win32 too
    assert b"\r" not in creds_file.read_bytes()

    creds_text = creds_file.read_text()
    assert "central_account:" in creds_text
    assert "glp_account:" in creds_text

    captured = capsys.readouterr()
    token_value = token_file.read_text().strip()
    assert token_value not in captured.out
    assert token_value not in captured.err


def test_manifest_carries_k1_fields_for_loopback_default(docker_root):
    args = setup_wizard._build_parser().parse_args(["--docker", "--yes"])

    manifest = setup_wizard._run_docker_mode(args)

    assert manifest.port == 8010
    assert manifest.host_ip is None
    # W2 prompts the client-facing hostname (loopback default); backend stays
    # neutral until W3 lands its prompt.
    assert manifest.client_hostname == "localhost"
    assert manifest.rag is False
    assert manifest.backend is None
    assert manifest.products == []
    assert manifest.access_profile == "custom"
    assert manifest.token_path == docker_root / "secrets" / "mcp_http_bearer_token"
    assert manifest.creds_path == docker_root / "secrets" / "credentials.yaml"


def test_manifest_carries_flags_through_to_k1_fields(docker_root):
    _seed_real_credentials(docker_root)
    # --yes acknowledges exposure defaults; the flags themselves still pin
    # through to the K1 fields (W2 fills client_hostname from the ack).
    args = setup_wizard._build_parser().parse_args(
        [
            "--docker",
            "--yes",
            "--port",
            "9443",
            "--products",
            "clearpass,mist",
            "--access-profile",
            "full-read-write",
            "--expose",
            "192.168.10.5",
            "--expose",
            "192.168.10.5",
        ]
    )

    manifest = setup_wizard._run_docker_mode(args)

    assert manifest.port == 9443
    assert manifest.host_ip == "192.168.10.5"
    assert manifest.client_hostname == "192.168.10.5"
    assert manifest.products == ["clearpass", "mist"]
    assert manifest.access_profile == "full-read-write"


# ---------------------------------------------------------------------------
# Exposure resolution (R9: typed-twice acknowledgment, fail-fast refusals)
# ---------------------------------------------------------------------------


def test_single_expose_is_rejected_before_any_write(docker_root):
    with pytest.raises(SystemExit):
        setup_wizard.main(["--docker", "--yes", "--expose", "192.168.10.5"])

    assert not (docker_root / "secrets").exists()


def test_mismatched_expose_pair_is_rejected(docker_root):
    with pytest.raises(SystemExit):
        setup_wizard.main(
            ["--docker", "--yes", "--expose", "192.168.10.5", "--expose", "192.168.10.6"]
        )

    assert not (docker_root / "secrets").exists()


def test_yes_with_nonloopback_host_requires_explicit_expose(docker_root):
    with pytest.raises(SystemExit):
        setup_wizard.main(["--docker", "--yes", "--host", "192.168.10.5"])

    assert not (docker_root / "secrets").exists()


def test_loopback_expose_address_collapses_to_loopback_deployment(docker_root):
    # Placeholders are fatal only on genuinely non-loopback deployments; a
    # loopback --expose value must therefore still finish cleanly under --yes.
    exit_code = setup_wizard.main(
        ["--docker", "--yes", "--expose", "127.0.0.1", "--expose", "127.0.0.1"]
    )

    assert exit_code == 0
    assert (docker_root / "secrets" / "credentials.yaml").exists()


def test_interactive_exposure_accepts_typed_twice_address(docker_root, monkeypatch):
    _seed_real_credentials(docker_root)
    answers = iter(["y", "192.168.10.5", "n", "", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    manifest = setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--host", "192.168.10.5"])
    )

    assert manifest.host_ip == "192.168.10.5"
    assert manifest.client_hostname == "192.168.10.5"


def test_interactive_exposure_mismatch_refuses_to_expose(docker_root, monkeypatch):
    answers = iter(["y", "192.168.10.99"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    with pytest.raises(SystemExit):
        setup_wizard.main(["--docker", "--host", "192.168.10.5"])

    assert not (docker_root / "secrets").exists()


# ---------------------------------------------------------------------------
# Placeholder gate (C-pin): non-loopback + placeholders refuses to finish
# ---------------------------------------------------------------------------


def test_placeholder_credentials_abort_acknowledged_non_loopback(docker_root, capsys):
    with pytest.raises(SystemExit) as excinfo:
        setup_wizard.main(
            ["--docker", "--yes", "--expose", "192.168.10.5", "--expose", "192.168.10.5"]
        )

    assert excinfo.value.code != 0
    assert setup_wizard.NON_LOOPBACK_WARNING in capsys.readouterr().out
    # K3: abort leaves no overlay/.env that could reference missing secrets.
    assert not (docker_root / "docker-compose.router.local.yml").exists()
    assert not (docker_root / ".env").exists()
    # The gate fires AFTER secret emission (K3 write order), so both exist.
    assert (docker_root / "secrets" / "mcp_http_bearer_token").exists()
    assert (docker_root / "secrets" / "credentials.yaml").exists()


def test_skip_credentials_exposed_run_refuses_missing_credentials(docker_root):
    """W1a K3 amendment: exposed deployments require credentials.yaml to exist."""
    with pytest.raises(SystemExit) as excinfo:
        setup_wizard.main(
            [
                "--docker",
                "--yes",
                "--skip-credentials",
                "--expose",
                "192.168.10.5",
                "--expose",
                "192.168.10.5",
            ]
        )

    assert excinfo.value.code != 0
    assert "credentials.yaml" in str(excinfo.value)
    # K3: abort leaves no overlay/.env referencing the missing secret file.
    assert not (docker_root / "docker-compose.router.local.yml").exists()
    assert not (docker_root / ".env").exists()
    assert not (docker_root / "secrets" / "credentials.yaml").exists()


# ---------------------------------------------------------------------------
# Token file hygiene
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_preexisting_0644_token_file_tightened_to_0600(docker_root):
    """Pins the keep-path 0600 repair of _docker_token_step (:707-713)."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("a" * 64 + "\n")
    os.chmod(token_file, 0o644)

    setup_wizard.main(["--docker", "--yes"])

    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    # Valid stale content is kept byte-for-byte; only the mode gets repaired.
    assert token_file.read_text() == "a" * 64 + "\n"


def test_existing_valid_token_is_kept_without_force(docker_root, capsys):
    """W1a: a valid existing token survives a rerun byte-for-byte."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("b" * 64 + "\n")

    exit_code = setup_wizard.main(["--docker", "--yes"])

    assert exit_code == 0
    assert token_file.read_text() == "b" * 64 + "\n"
    assert "kept existing token" in capsys.readouterr().out


def test_force_rotates_existing_valid_token(docker_root):
    """W1a: --force is the only path that replaces an existing valid token."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("b" * 64 + "\n")

    setup_wizard.main(["--docker", "--yes", "--force"])

    content = token_file.read_text()
    assert content != "b" * 64 + "\n"
    assert re.fullmatch(r"[0-9a-f]{64}", content.strip())


def test_garbage_token_content_aborts_without_force(docker_root):
    """W1a: invalid token content is neither kept nor silently rotated."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("stale-token-from-an-earlier-run\n")

    with pytest.raises(SystemExit) as excinfo:
        setup_wizard.main(["--docker", "--yes"])

    assert excinfo.value.code != 0
    refusal = str(excinfo.value)
    assert "mcp_http_bearer_token" in refusal
    assert "--force" in refusal
    # Neither branch fired: bytes untouched, credential step never ran.
    assert token_file.read_text() == "stale-token-from-an-earlier-run\n"
    assert not (docker_root / "secrets" / "credentials.yaml").exists()



def test_binary_token_content_takes_garbage_refusal_path(docker_root):
    """W1b: non-decodable token bytes hit the same refusal as invalid text."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    payload = b"not-a-token\xff\xfe\x80\n"
    token_file.write_bytes(payload)

    with pytest.raises(SystemExit) as excinfo:
        setup_wizard.main(["--docker", "--yes"])

    assert excinfo.value.code != 0
    refusal = str(excinfo.value)
    assert "mcp_http_bearer_token" in refusal
    assert "--force" in refusal
    # Bytes untouched, credential step never ran.
    assert token_file.read_bytes() == payload
    assert not (docker_root / "secrets" / "credentials.yaml").exists()


def test_binary_env_file_refuses_cleanly_with_nonzero_exit(
    tmp_path, monkeypatch, capsys
):
    """W1b (sibling sweep): undecodable .env bytes WARN cleanly, exit 1."""
    payload = b"\x81\x9dHPE_MCP_ROUTER_MODE=junk\xff\n"
    (tmp_path / ".env").write_bytes(payload)
    monkeypatch.setattr(setup_wizard, "ROOT", tmp_path)

    exit_code = setup_wizard.main(
        [
            "--yes",
            "--skip-install",
            "--skip-credentials",
            "--skip-stdio",
            "--skip-http",
            "--skip-catalog",
            "--skip-doctor",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "[WARN] .env:" in captured.out
    assert "could not merge existing entries" in captured.out
    # The refusal leaves the undecodable bytes untouched.
    assert (tmp_path / ".env").read_bytes() == payload


def test_binary_optional_products_json_degrades_to_clean_warn(tmp_path):
    """W1b (sibling sweep): undecodable .mcp.json bytes WARN instead of raising."""
    target = tmp_path / ".mcp.json"
    payload = b"\x81\x9d{\"mcpServers\":\xff"
    target.write_bytes(payload)

    step = setup_wizard._merge_json_env(
        target, "hpe-networking-mcp", {"HPE_MCP_ROUTER_MODE": "minimal"}
    )

    assert step.status == "WARN"
    assert "could not update optional product env" in step.detail
    assert target.read_bytes() == payload


# ---------------------------------------------------------------------------
# C6 hygiene: generated artifacts stay untracked
# ---------------------------------------------------------------------------


def test_generated_artifacts_are_gitignored():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-z", "--stdin"],
        input=(
            b"docker-compose.router.local.yml\0"
            b".env\0"
            b"secrets/mcp_http_bearer_token\0"
            b"secrets/credentials.yaml\0"
        ),
        capture_output=True,
        check=True,
    )

    ignored = {entry for entry in result.stdout.decode().split("\0") if entry}
    assert ignored == {
        "docker-compose.router.local.yml",
        ".env",
        "secrets/mcp_http_bearer_token",
        "secrets/credentials.yaml",
    }


# ---------------------------------------------------------------------------
# W2 — compose overlay emission
# ---------------------------------------------------------------------------


EXPOSED_IP = "192.168.10.5"


def _overlay_manifest(**overrides):
    values = dict(
        port=8010,
        host_ip=None,
        client_hostname="localhost",
        rag=False,
        backend=None,
        products=[],
        access_profile="custom",
        token_path=Path("secrets/mcp_http_bearer_token"),
        creds_path=Path("secrets/credentials.yaml"),
    )
    values.update(overrides)
    return setup_wizard.DockerManifest(**values)


@pytest.mark.parametrize(
    ("exposed", "rag", "products"),
    [(e, r, p) for e in (False, True) for r in (False, True) for p in (False, True)],
)
def test_overlay_emission_matrix(docker_root, monkeypatch, exposed, rag, products):
    """W2 acceptance: loopback/exposed x RAG/no-RAG x products on/off."""
    if exposed:
        _seed_real_credentials(docker_root)
    if rag:
        monkeypatch.setattr(
            setup_wizard, "_choose_rag_image", lambda *, assume_yes: True
        )
    argv = ["--docker", "--yes", "--port", "8443"]
    if exposed:
        argv += ["--expose", EXPOSED_IP, "--expose", EXPOSED_IP]
    if products:
        argv += ["--products", "clearpass,mist"]
    manifest = setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(argv)
    )

    bind = EXPOSED_IP if exposed else "127.0.0.1"
    assert manifest.client_hostname == (EXPOSED_IP if exposed else "localhost")
    assert manifest.rag is rag
    overlay_path = docker_root / "docker-compose.router.local.yml"
    text = overlay_path.read_text()
    config = yaml.safe_load(text)
    service = config["services"]["mcp-router"]

    # R1d: the single ports entry is the literal full form, same port twice.
    match = re.fullmatch(rf"{re.escape(bind)}:(\d+):\1", service["ports"][0])
    assert match, f"published line is not literal <bind>:<p>:<p>: {service['ports']}"
    assert not re.search(r'- "\d+:\d+"', text), "shorthand publish line present"

    assert service["profiles"] == ["router"]
    assert "depends_on" not in service
    assert service["secrets"] == ["credentials_yaml", "mcp_http_bearer_token"]
    assert service["restart"] == "unless-stopped"
    assert config["secrets"] == {
        "credentials_yaml": {"file": "./secrets/credentials.yaml"},
        "mcp_http_bearer_token": {"file": "./secrets/mcp_http_bearer_token"},
    }
    assert set(config["volumes"]) == {"router_state", "router_outputs"}

    env = service["environment"]
    assert env["MCP_TRANSPORT"] == "streamable-http"
    assert env["MCP_HOST"] == "0.0.0.0"
    assert env["MCP_PORT"] == "8443"
    assert env["CREDS_PATH"] == "/run/secrets/credentials_yaml"
    assert env["MCP_HTTP_BEARER_TOKEN_FILE"] == "/run/secrets/mcp_http_bearer_token"
    assert env["HPE_MCP_ROUTER_MODE"] == "${HPE_MCP_ROUTER_MODE:-minimal}"
    assert env["HPE_MCP_TOOLSETS"] == "${HPE_MCP_TOOLSETS:-central,glp,rag}"
    assert env["HPE_MCP_ACCESS_PROFILE"] == "${HPE_MCP_ACCESS_PROFILE:-custom}"
    assert env["HPE_MCP_RAG_BACKEND"] == "${HPE_MCP_RAG_BACKEND:-}"
    host_names = ["127.0.0.1", "localhost"] + ([EXPOSED_IP] if exposed else [])
    assert env["MCP_ALLOWED_HOSTS"] == ",".join(f"{h}:*" for h in host_names)
    assert env["MCP_ALLOWED_ORIGINS"] == ",".join(f"http://{h}:*" for h in host_names)

    if rag:
        assert "build" not in service
        assert service["image"] == "hpe-networking-mcp-router:rag"
        assert service["volumes"] == [
            "router_state:/app/state",
            "router_outputs:/app/outputs",
            "./data/docs.lance:/app/data/docs.lance:ro",
            "./data/tools.lance:/app/data/tools.lance:ro",
        ]
    else:
        assert service["build"] == {"context": ".", "dockerfile": "Dockerfile"}
        assert service["image"] == "hpe-networking-mcp-router:local"
        assert all(".lance" not in mount for mount in service["volumes"])

    # Standing fences (R8/C4/C7), asserted in every matrix cell.
    assert "./data:/app/data" not in text
    assert "HPE_MCP_ALLOW_INSECURE_HTTP_BINDING" not in text
    assert "privileged" not in text
    assert "devices" not in text
    assert "docker.sock" not in text


def test_overlay_text_ignores_product_selection():
    """W2 consumes no product fields; W3 owns them as .env keys."""
    plain = setup_wizard._compose_overlay_text(_overlay_manifest())
    loaded = setup_wizard._compose_overlay_text(
        replace(_overlay_manifest(), products=["clearpass", "mist"])
    )
    assert plain == loaded


@pytest.mark.parametrize(
    "bad_bind", ["", "not-an-ip", "8010:8010", f"{EXPOSED_IP}:8010", "fe80::1"]
)
def test_shorthand_or_unparseable_bind_refused_before_any_write(docker_root, bad_bind):
    """R1d negative: shorthand input can never reach a written overlay."""
    with pytest.raises(SystemExit):
        setup_wizard._write_compose_overlay(_overlay_manifest(host_ip=bad_bind), force=True)
    assert not (docker_root / "docker-compose.router.local.yml").exists()


def test_custom_client_hostname_drives_allowlists_not_the_bind_ip(
    docker_root, monkeypatch
):
    """W2 acceptance: ORIGINS carry the prompted hostname, never the bind IP."""
    _seed_real_credentials(docker_root)
    monkeypatch.setattr(
        setup_wizard, "_ask_text", lambda prompt, default="": "mcp.example.com"
    )
    monkeypatch.setattr(
        setup_wizard, "_ask", lambda prompt, default, *, assume_yes: False
    )
    args = setup_wizard._build_parser().parse_args(
        ["--docker", "--skip-credentials", "--expose", EXPOSED_IP, "--expose", EXPOSED_IP]
    )
    manifest = setup_wizard._run_docker_mode(args)

    assert manifest.client_hostname == "mcp.example.com"
    overlay = docker_root / "docker-compose.router.local.yml"
    env = yaml.safe_load(overlay.read_text())["services"]["mcp-router"]["environment"]
    assert env["MCP_ALLOWED_HOSTS"] == "127.0.0.1:*,localhost:*,mcp.example.com:*"
    assert env["MCP_ALLOWED_ORIGINS"] == (
        "http://127.0.0.1:*,http://localhost:*,http://mcp.example.com:*"
    )
    assert EXPOSED_IP not in env["MCP_ALLOWED_HOSTS"]
    assert EXPOSED_IP not in env["MCP_ALLOWED_ORIGINS"]


@pytest.mark.parametrize("answer", ["has space", "host:8010", "*", "a,b"])
def test_invalid_client_hostname_refuses_before_any_write(
    docker_root, monkeypatch, answer
):
    """Allowlist-source answers are validated before ANY artifact is written."""
    _seed_real_credentials(docker_root)
    monkeypatch.setattr(setup_wizard, "_ask_text", lambda prompt, default="": answer)
    monkeypatch.setattr(
        setup_wizard, "_ask", lambda prompt, default, *, assume_yes: False
    )
    args = setup_wizard._build_parser().parse_args(
        ["--docker", "--skip-credentials", "--expose", EXPOSED_IP, "--expose", EXPOSED_IP]
    )
    with pytest.raises(SystemExit):
        setup_wizard._run_docker_mode(args)
    # Prompts precede every write: neither secrets nor overlay may exist.
    assert not (docker_root / "secrets" / "mcp_http_bearer_token").exists()
    assert not (docker_root / "docker-compose.router.local.yml").exists()


def test_existing_overlay_kept_without_force_regenerated_with_force(docker_root, capsys):
    yes_args = ["--docker", "--yes"]
    setup_wizard._run_docker_mode(setup_wizard._build_parser().parse_args(yes_args))
    overlay = docker_root / "docker-compose.router.local.yml"
    original = overlay.read_bytes()

    overlay.write_text("# hand-edited\n", encoding="utf-8")
    capsys.readouterr()
    setup_wizard._run_docker_mode(setup_wizard._build_parser().parse_args(yes_args))
    assert overlay.read_text(encoding="utf-8") == "# hand-edited\n"
    assert "kept existing overlay" in capsys.readouterr().out

    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args([*yes_args, "--force"])
    )
    assert overlay.read_bytes() == original


def test_rag_image_choice_is_opt_in(monkeypatch):
    assert setup_wizard._choose_rag_image(assume_yes=True) is False
    monkeypatch.setattr(
        setup_wizard, "_ask", lambda prompt, default, *, assume_yes: True
    )
    assert setup_wizard._choose_rag_image(assume_yes=False) is True


def test_next_steps_point_at_emitted_overlay(docker_root, capsys):
    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )
    out = capsys.readouterr().out
    assert "docker-compose.router.local.yml" in out
    assert "--profile router up -d mcp-router" in out
    assert "does not emit it yet" not in out


def test_generated_overlay_validates_standalone_and_merged_with_docker_cli(
    docker_root, monkeypatch
):
    """Same `docker compose config` gate as test_docker_router_packaging.py."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    plugin = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10
    )
    if plugin.returncode != 0:
        pytest.skip("docker compose CLI plugin not available")
    shutil.copy(REPO_ROOT / "docker-compose.yml", docker_root / "docker-compose.yml")
    # The RAG variant references operator-provisioned corpus mounts; give the
    # parse step real paths even though `config` is a pure merge.
    for lance in ("docs.lance", "tools.lance"):
        (docker_root / "data" / lance).mkdir(parents=True, exist_ok=True)

    def _validate() -> None:
        overlay = docker_root / "docker-compose.router.local.yml"
        standalone = subprocess.run(
            [
                "docker", "compose", "-f", str(overlay),
                "--profile", "router", "config",
            ],
            capture_output=True,
            text=True,
            cwd=docker_root,
            timeout=30,
        )
        assert standalone.returncode == 0, standalone.stderr
        merged = subprocess.run(
            [
                "docker", "compose", "-f", "docker-compose.yml",
                "-f", "docker-compose.router.local.yml",
                "--profile", "router", "config",
            ],
            capture_output=True,
            text=True,
            cwd=docker_root,
            timeout=30,
        )
        assert merged.returncode == 0, merged.stderr

    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )
    _validate()

    monkeypatch.setattr(
        setup_wizard, "_choose_rag_image", lambda *, assume_yes: True
    )
    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes", "--force"])
    )
    _validate()

    services = subprocess.run(
        [
            "docker", "compose", "-f", "docker-compose.yml",
            "-f", "docker-compose.router.local.yml", "config", "--services",
        ],
        capture_output=True,
        text=True,
        cwd=docker_root,
        timeout=30,
    )
    assert services.returncode == 0, services.stderr
    assert set(services.stdout.split()) == {"redis", "ollama"}, (
        "the generated overlay must stay opt-in behind the router profile"
    )


# ---------------------------------------------------------------------------
# W3 — .env emission + R7 combined-output audit + C3 pair-absence
# ---------------------------------------------------------------------------


def _env_lines(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key = setup_wizard._env_assignment_key(line)
        if key is not None:
            parsed[key] = setup_wizard._env_assignment_value(line) or ""
    return parsed


def test_docker_yes_emits_allowlisted_env_defaults(docker_root):
    exit_code = setup_wizard.main(["--docker", "--yes"])

    assert exit_code == 0
    env = _env_lines(docker_root / ".env")
    assert set(env) <= setup_wizard.DOCKER_ENV_ALLOWLIST
    assert not any(setup_wizard._is_secret_env_var(key) for key in env)
    # C8: defaults stay read-only/custom; nothing escalates by default.
    assert env["HPE_MCP_ROUTER_MODE"] == "minimal"
    assert env["HPE_MCP_ACCESS_PROFILE"] == "custom"
    assert env["HPE_MCP_PRODUCT_ACCESS"] == "read-only"
    assert not any(name in env for name in setup_wizard.PLATFORM_WRITE_ENV_VARS)
    assert "HPE_MCP_RAG_BACKEND" not in env


@pytest.mark.parametrize(
    ("profile", "product_access", "expected_access", "gate_value"),
    [
        ("safe-read-only", None, "read-only", "0"),
        ("full-read-write", None, "read-write", "1"),
        ("custom", "read-write", "read-write", None),
    ],
)
def test_env_profile_matrix_pins_gates_and_product_access(
    docker_root, profile, product_access, expected_access, gate_value
):
    argv = [
        "--docker",
        "--yes",
        "--access-profile",
        profile,
        "--products",
        "clearpass",
    ]
    if product_access:
        argv += ["--product-access", product_access]
    assert setup_wizard.main(argv) == 0

    env = _env_lines(docker_root / ".env")
    assert env["HPE_MCP_ACCESS_PROFILE"] == profile
    assert env["HPE_MCP_PRODUCT_ACCESS"] == expected_access
    gates = {
        name: env[name]
        for name in setup_wizard.PLATFORM_WRITE_ENV_VARS
        if name in env
    }
    if gate_value is None:
        assert gates == {}
    else:
        assert gates == {name: gate_value for name in setup_wizard.PLATFORM_WRITE_ENV_VARS}


def test_rag_redis_backend_lands_in_manifest_and_env(docker_root, monkeypatch):
    monkeypatch.setattr(setup_wizard, "_choose_rag_image", lambda *, assume_yes: True)
    monkeypatch.setattr(
        setup_wizard, "_choose_rag_backend", lambda *, assume_yes: "redis"
    )

    manifest = setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )

    assert manifest.backend == "redis"
    assert _env_lines(docker_root / ".env")["HPE_MCP_RAG_BACKEND"] == "redis"


def test_rag_lancedb_backend_omits_the_env_key(docker_root, monkeypatch):
    monkeypatch.setattr(setup_wizard, "_choose_rag_image", lambda *, assume_yes: True)

    manifest = setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )

    assert manifest.backend == "lancedb"
    assert "HPE_MCP_RAG_BACKEND" not in _env_lines(docker_root / ".env")


def test_choose_rag_backend_is_opt_in(monkeypatch):
    assert setup_wizard._choose_rag_backend(assume_yes=True) == "lancedb"
    monkeypatch.setattr(
        setup_wizard, "_ask", lambda prompt, default, *, assume_yes: True
    )
    assert setup_wizard._choose_rag_backend(assume_yes=False) == "redis"


def test_stale_secret_env_key_warns_and_stays_byte_identical(docker_root, capsys):
    env_path = docker_root / ".env"
    env_path.write_text("CENTRAL_API_TOKEN=old\n", encoding="utf-8")

    exit_code = setup_wizard.main(["--docker", "--yes"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "CENTRAL_API_TOKEN" in out  # the WARN names the offending key
    content = env_path.read_text(encoding="utf-8")
    assert "CENTRAL_API_TOKEN=old\n" in content  # planted line byte-identical
    assert _env_lines(env_path)["CENTRAL_API_TOKEN"] == "old"
    assert "CENTRAL_API_TOKEN" not in setup_wizard.DOCKER_ENV_ALLOWLIST


def test_credential_affecting_env_keys_are_listed_and_untouched(docker_root, capsys):
    env_path = docker_root / ".env"
    env_path.write_text(
        "CREDS_PATH=config/other.yaml\nGLP_TOKEN_URL=https://stale.example.com\n",
        encoding="utf-8",
    )

    assert setup_wizard.main(["--docker", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "CREDS_PATH" in out and "GLP_TOKEN_URL" in out
    content = env_path.read_text(encoding="utf-8")
    assert "CREDS_PATH=config/other.yaml\n" in content
    assert "GLP_TOKEN_URL=https://stale.example.com\n" in content


def test_force_refuses_to_overwrite_env_holding_secret_keys(docker_root):
    env_path = docker_root / ".env"
    env_path.write_text("CENTRAL_API_TOKEN=old\n", encoding="utf-8")
    planted = env_path.read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        setup_wizard.main(["--docker", "--yes", "--force"])

    assert "CENTRAL_API_TOKEN" in str(excinfo.value)
    assert env_path.read_bytes() == planted


def test_binary_existing_env_degrades_to_clean_warn_in_docker_mode(
    docker_root, capsys
):
    """W1b crash class, docker-mode sibling: no traceback, bytes untouched."""
    (docker_root / ".env").write_bytes(b"\x81\x9d\xff")

    exit_code = setup_wizard.main(["--docker", "--yes"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "could not merge existing entries" in captured.out
    assert captured.err.strip() == ""
    assert (docker_root / ".env").read_bytes() == b"\x81\x9d\xff"


def test_no_plain_twin_of_referenced_file_vars_across_artifacts(docker_root):
    assert setup_wizard.main(["--docker", "--yes"]) == 0

    artifacts = [
        docker_root / "secrets" / "mcp_http_bearer_token",
        docker_root / "secrets" / "credentials.yaml",
        docker_root / "docker-compose.router.local.yml",
        docker_root / ".env",
    ]
    texts = {path.name: path.read_text(encoding="utf-8") for path in artifacts}

    for name, text in texts.items():
        assert not setup_wizard._plain_twin_violations(text), name

    referenced_files = {
        match
        for text in texts.values()
        for match in setup_wizard._FILE_ENV_REF_RE.findall(text)
    }
    assert referenced_files == {"MCP_HTTP_BEARER_TOKEN_FILE"}


def test_plain_twin_violations_helper_shapes():
    text = (
        "services:\n"
        "  MCP_HTTP_BEARER_TOKEN_FILE: /run/secrets/x\n"
        "MCP_HTTP_BEARER_TOKEN=leak\n"
    )
    assert setup_wizard._plain_twin_violations(text) == ["MCP_HTTP_BEARER_TOKEN"]
    assert setup_wizard._plain_twin_violations("MCP_HTTP_BEARER_TOKEN_FILE: x\n") == []


def test_overlay_writer_refuses_text_carrying_a_plain_twin(docker_root, monkeypatch):
    monkeypatch.setattr(
        setup_wizard,
        "_compose_overlay_text",
        lambda manifest: (
            "services:\n  mcp-router:\n"
            "      MCP_HTTP_BEARER_TOKEN_FILE: /run/secrets/mcp_http_bearer_token\n"
            "      MCP_HTTP_BEARER_TOKEN=leaked\n"
        ),
    )

    with pytest.raises(SystemExit):
        setup_wizard._write_compose_overlay(_overlay_manifest(), force=True)
    assert not (docker_root / "docker-compose.router.local.yml").exists()


def test_next_steps_mention_env_knobs_and_redis_hint(docker_root, capsys, monkeypatch):
    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )
    out = capsys.readouterr().out
    assert ".env" in out
    capsys.readouterr()
    monkeypatch.setattr(setup_wizard, "_choose_rag_image", lambda *, assume_yes: True)
    monkeypatch.setattr(
        setup_wizard, "_choose_rag_backend", lambda *, assume_yes: "redis"
    )
    setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--yes"])
    )
    out = capsys.readouterr().out
    assert "HPE_MCP_RAG_BACKEND=redis" in out
    assert "redis service started" in out
