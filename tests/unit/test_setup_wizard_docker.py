"""Contract pins for ``setup_wizard.py --docker`` (slice W1: core + secrets).

Plan: PLANS/DOCKER_SETUP_WIZARD_SLICE_PLAN.md. The K1 manifest fields and the
K2 module-level path constants asserted here are the merge-carried interface
that slices W2 (overlay emission) and W3 (.env audit) consume.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

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
    # Neutral until the consuming slice prompts for them (W2 hostname/RAG, W3 backend).
    assert manifest.client_hostname is None
    assert manifest.rag is False
    assert manifest.backend is None
    assert manifest.products == []
    assert manifest.access_profile == "custom"
    assert manifest.token_path == docker_root / "secrets" / "mcp_http_bearer_token"
    assert manifest.creds_path == docker_root / "secrets" / "credentials.yaml"


def test_manifest_carries_flags_through_to_k1_fields(docker_root):
    _seed_real_credentials(docker_root)
    args = setup_wizard._build_parser().parse_args(
        [
            "--docker",
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
    answers = iter(["y", "192.168.10.5", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    manifest = setup_wizard._run_docker_mode(
        setup_wizard._build_parser().parse_args(["--docker", "--host", "192.168.10.5"])
    )

    assert manifest.host_ip == "192.168.10.5"


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


def test_skip_credentials_leaves_placeholder_gate_vacuous(docker_root):
    exit_code = setup_wizard.main(
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

    assert exit_code == 0
    assert not (docker_root / "secrets" / "credentials.yaml").exists()


# ---------------------------------------------------------------------------
# Token file hygiene
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_preexisting_0644_token_file_tightened_to_0600(docker_root):
    """Pins the os.fchmod branch of _write_secret_file (:138-139)."""
    token_file = docker_root / "secrets" / "mcp_http_bearer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("stale-token-from-an-earlier-run\n")
    os.chmod(token_file, 0o644)

    setup_wizard.main(["--docker", "--yes"])

    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert re.fullmatch(r"[0-9a-f]{64}", token_file.read_text().strip())


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
