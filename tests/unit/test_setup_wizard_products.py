from __future__ import annotations

import argparse
import json
import re
import sys

import pytest

from scripts import setup_wizard


def test_product_env_includes_access_mode():
    env = setup_wizard._product_env(
        ["clearpass", "mist"],
        assume_yes=True,
        product_access="read-write",
    )

    assert env["HPE_MCP_ACCESS_PROFILE"] == "custom"
    assert env["HPE_MCP_PRODUCTS"] == "clearpass,mist"
    assert env["HPE_MCP_PRODUCT_ACCESS"] == "read-write"
    assert env["CLEARPASS_BASE_URL"] == "https://clearpass.example.com"
    assert env["MIST_HOST"] == "https://api.mist.com"


def test_product_env_includes_uxi_credentials():
    env = setup_wizard._product_env(
        ["uxi"],
        assume_yes=True,
        product_access="read-only",
    )

    assert env["HPE_MCP_PRODUCTS"] == "uxi"
    assert env["UXI_CLIENT_ID"] == "YOUR_UXI_CLIENT_ID"
    assert env["UXI_CLIENT_SECRET"] == "YOUR_UXI_CLIENT_SECRET"


def test_uxi_client_secret_uses_secret_prompt():
    assert setup_wizard._is_secret_env_var("UXI_CLIENT_SECRET") is True


def test_public_docs_list_wizard_product_env_vars():
    docs = [
        setup_wizard.ROOT / "docs" / "optional-products.md",
        setup_wizard.ROOT / "docs" / "getting-started.md",
        setup_wizard.ROOT / "docs" / "index.md",
    ]

    for path in docs:
        lines = path.read_text().splitlines()
        for spec in setup_wizard.PRODUCT_ENV.values():
            label = spec["label"]
            row = next((line for line in lines if line.startswith(f"| {label} |")), None)
            assert row is not None, f"{path.relative_to(setup_wizard.ROOT)} missing {label}"
            documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", row))
            assert set(spec["vars"]) <= documented


def test_product_access_defaults_to_read_only_for_products():
    args = argparse.Namespace(product_access="read-only", yes=False)

    assert setup_wizard._product_access(args, ["clearpass"]) == "read-only"


def test_product_access_accepts_explicit_read_write_for_labs():
    args = argparse.Namespace(
        access_profile="custom", product_access="read-write", yes=False
    )

    assert setup_wizard._product_access(args, ["clearpass"]) == "read-write"


def test_product_access_respects_read_only_without_prompt(monkeypatch):
    args = argparse.Namespace(product_access="read-only", yes=False)
    monkeypatch.setattr(
        setup_wizard,
        "_ask",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert setup_wizard._product_access(args, ["clearpass"]) == "read-only"


def test_product_access_no_products_stays_read_only():
    args = argparse.Namespace(product_access="read-write", yes=False)

    assert setup_wizard._product_access(args, []) == "read-only"


def test_product_env_explicitly_disables_unselected_products():
    env = setup_wizard._product_env([], assume_yes=True)

    assert env["HPE_MCP_PRODUCTS"] == ""
    assert env["HPE_MCP_PRODUCT_ACCESS"] == "read-only"


def test_full_profile_derives_read_write_product_access():
    args = argparse.Namespace(
        access_profile="full-read-write", product_access=None, yes=False
    )

    assert setup_wizard._product_access(args, ["clearpass"]) == "read-write"


@pytest.mark.parametrize(
    ("profile", "readonly", "product_access", "platform_value"),
    [
        ("safe-read-only", "1", "read-only", "0"),
        ("full-read-write", "0", "read-write", "1"),
    ],
)
def test_aggregate_profile_env_overrides_stale_legacy_gates(
    profile,
    readonly,
    product_access,
    platform_value,
):
    env = setup_wizard._product_env(
        [],
        assume_yes=True,
        product_access=product_access,
        access_profile=profile,
    )

    assert env["HPE_MCP_ACCESS_PROFILE"] == profile
    assert env["HPE_MCP_PRODUCTS"] == ""
    assert env["HPE_MCP_READONLY"] == readonly
    assert env["HPE_MCP_PRODUCT_ACCESS"] == product_access
    assert all(
        env[name] == platform_value
        for name in setup_wizard.PLATFORM_WRITE_ENV_VARS
    )


def test_safe_profile_rejects_read_write_product_access():
    args = argparse.Namespace(
        access_profile="safe-read-only", product_access="read-write", yes=False
    )

    with pytest.raises(SystemExit, match="conflicts"):
        setup_wizard._product_access(args, ["clearpass"])


def test_write_env_file_merges_existing_values_without_overwriting_tokens(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "\n".join(
            [
                "# local lab tokens",
                "export HPE_MCP_PRODUCTS=clearpass",
                "export CLEARPASS_API_TOKEN=real-token",
                "",
            ]
        )
    )

    step = setup_wizard._write_env_file(
        target,
        {
            "HPE_MCP_PRODUCTS": "clearpass,mist",
            "HPE_MCP_PRODUCT_ACCESS": "read-write",
            "CLEARPASS_API_TOKEN": "YOUR_CLEARPASS_API_TOKEN",
            "MIST_HOST": "https://api.mist.com",
            "MIST_API_TOKEN": "YOUR_MIST_API_TOKEN",
        },
        force=False,
    )

    text = target.read_text()
    assert step.status == "OK"
    assert "export HPE_MCP_PRODUCTS=clearpass,mist" in text
    assert "export HPE_MCP_PRODUCT_ACCESS=read-write" in text
    assert "export CLEARPASS_API_TOKEN=real-token" in text
    assert "YOUR_CLEARPASS_API_TOKEN" not in text
    assert "export MIST_API_TOKEN=YOUR_MIST_API_TOKEN" in text


def test_write_env_file_replaces_placeholder_values_on_rerun(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "\n".join(
            [
                "export HPE_MCP_PRODUCTS=mist",
                "export MIST_API_TOKEN=YOUR_MIST_API_TOKEN",
                "export MIST_HOST=https://old.example.com",
                "",
            ]
        )
    )

    step = setup_wizard._write_env_file(
        target,
        {
            "HPE_MCP_PRODUCTS": "mist",
            "HPE_MCP_PRODUCT_ACCESS": "read-write",
            "MIST_HOST": "https://api.mist.com",
            "MIST_API_TOKEN": "real-token",
        },
        force=False,
    )

    text = target.read_text()
    assert step.status == "OK"
    assert "export MIST_API_TOKEN=real-token" in text
    assert "YOUR_MIST_API_TOKEN" not in text
    assert "export MIST_HOST=https://old.example.com" in text
    assert "export HPE_MCP_PRODUCT_ACCESS=read-write" in text


def test_write_env_file_force_replaces_existing_env(tmp_path):
    target = tmp_path / ".env"
    target.write_text("export HPE_MCP_PRODUCTS=clearpass\n")

    step = setup_wizard._write_env_file(
        target,
        {
            "HPE_MCP_PRODUCTS": "mist",
            "HPE_MCP_PRODUCT_ACCESS": "read-only",
        },
        force=True,
    )

    assert step.status == "OK"
    assert "HPE_MCP_PRODUCTS=mist" in target.read_text()


def test_write_env_file_clears_stale_products_when_none_are_selected(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "export HPE_MCP_PRODUCTS=mist\n"
        "export HPE_MCP_PRODUCT_ACCESS=read-write\n"
    )

    step = setup_wizard._write_env_file(
        target,
        setup_wizard._product_env([], assume_yes=True),
        force=False,
    )

    text = target.read_text()
    assert step.status == "OK"
    assert "export HPE_MCP_PRODUCTS=''" in text
    assert "export HPE_MCP_PRODUCT_ACCESS=read-only" in text


def test_write_env_file_clears_aggregate_gates_when_switching_to_custom(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "export HPE_MCP_ACCESS_PROFILE='  FULL-READ-WRITE  ' # generated\n"
        "export HPE_MCP_READONLY=0\n"
        "export HPE_MCP_GLP_V2BETA1_WRITES=1\n"
        "export HPE_MCP_MIST_WRITES=1\n"
        "export MIST_API_TOKEN=real-token\n"
    )

    step = setup_wizard._write_env_file(
        target,
        setup_wizard._product_env([], assume_yes=True),
        force=False,
    )

    text = target.read_text()
    assert step.status == "OK"
    assert "export HPE_MCP_ACCESS_PROFILE=custom" in text
    assert "HPE_MCP_READONLY" not in text
    assert "HPE_MCP_GLP_V2BETA1_WRITES" not in text
    assert "HPE_MCP_MIST_WRITES" not in text
    assert "export MIST_API_TOKEN=real-token" in text


def test_write_env_file_preserves_deliberate_custom_override(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "export HPE_MCP_ACCESS_PROFILE=custom\n"
        "export HPE_MCP_MIST_WRITES=1\n"
    )

    setup_wizard._write_env_file(
        target,
        setup_wizard._product_env([], assume_yes=True),
        force=False,
    )

    assert "export HPE_MCP_MIST_WRITES=1" in target.read_text()


def test_merge_json_env_adds_product_access(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "command": "uv",
                        "env": {"HPE_MCP_ROUTER_MODE": "minimal"},
                    }
                }
            }
        )
    )

    step = setup_wizard._merge_json_env(
        target,
        "hpe-networking-mcp",
        {
            "HPE_MCP_ACCESS_PROFILE": "full-read-write",
            "HPE_MCP_ROUTER_MODE": "direct",
            "HPE_MCP_PRODUCTS": "clearpass,mist",
            "HPE_MCP_PRODUCT_ACCESS": "read-write",
        },
    )

    data = json.loads(target.read_text())
    env = data["mcpServers"]["hpe-networking-mcp"]["env"]
    assert step.status == "OK"
    assert env["HPE_MCP_ACCESS_PROFILE"] == "full-read-write"
    assert env["HPE_MCP_ROUTER_MODE"] == "direct"
    assert env["HPE_MCP_PRODUCTS"] == "clearpass,mist"
    assert env["HPE_MCP_PRODUCT_ACCESS"] == "read-write"


def test_merge_json_env_clears_aggregate_gates_when_switching_to_custom(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "command": "uv",
                        "env": {
                            "HPE_MCP_ACCESS_PROFILE": " FULL-READ-WRITE ",
                            "HPE_MCP_READONLY": "0",
                            "HPE_MCP_GLP_V2BETA1_WRITES": "1",
                            "HPE_MCP_MIST_WRITES": "1",
                            "UNRELATED": "preserved",
                        },
                    }
                }
            }
        )
    )

    step = setup_wizard._merge_json_env(
        target,
        "hpe-networking-mcp",
        {
            "HPE_MCP_ACCESS_PROFILE": "custom",
            "HPE_MCP_PRODUCTS": "",
            "HPE_MCP_PRODUCT_ACCESS": "read-only",
        },
    )

    env = json.loads(target.read_text())["mcpServers"]["hpe-networking-mcp"]["env"]
    assert step.status == "OK"
    assert env["HPE_MCP_ACCESS_PROFILE"] == "custom"
    assert "HPE_MCP_READONLY" not in env
    assert "HPE_MCP_GLP_V2BETA1_WRITES" not in env
    assert "HPE_MCP_MIST_WRITES" not in env
    assert env["UNRELATED"] == "preserved"


def test_merge_json_env_preserves_deliberate_custom_override(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "env": {
                            "HPE_MCP_ACCESS_PROFILE": "custom",
                            "HPE_MCP_MIST_WRITES": "1",
                        }
                    }
                }
            }
        )
    )

    setup_wizard._merge_json_env(
        target,
        "hpe-networking-mcp",
        {
            "HPE_MCP_ACCESS_PROFILE": "custom",
            "HPE_MCP_PRODUCTS": "",
            "HPE_MCP_PRODUCT_ACCESS": "read-only",
        },
    )

    env = json.loads(target.read_text())["mcpServers"]["hpe-networking-mcp"]["env"]
    assert env["HPE_MCP_MIST_WRITES"] == "1"


def test_merge_json_env_can_set_direct_mode_without_products(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "command": "uv",
                        "env": {"HPE_MCP_ROUTER_MODE": "minimal"},
                    }
                }
            }
        )
    )

    step = setup_wizard._merge_json_env(
        target,
        "hpe-networking-mcp",
        {"HPE_MCP_ROUTER_MODE": "direct"},
    )

    data = json.loads(target.read_text())
    assert step.status == "OK"
    assert (
        data["mcpServers"]["hpe-networking-mcp"]["env"]["HPE_MCP_ROUTER_MODE"]
        == "direct"
    )


def test_catalog_build_receives_product_access_env(monkeypatch):
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_wizard.py",
            "--yes",
            "--skip-install",
            "--skip-credentials",
            "--skip-stdio",
            "--skip-http",
            "--skip-doctor",
            "--products",
            "clearpass",
            "--product-access",
            "read-only",
        ],
    )
    monkeypatch.setattr(
        setup_wizard,
        "_write_env_file",
        lambda *args, **kwargs: setup_wizard.Step(".env", "OK", "captured"),
    )

    def fake_run(
        command: list[str],
        label: str,
        *,
        env: dict[str, str] | None = None,
    ) -> setup_wizard.Step:
        calls.append((command, label, env))
        return setup_wizard.Step(label, "OK", "captured")

    monkeypatch.setattr(setup_wizard, "_run", fake_run)

    assert setup_wizard.main() == 0

    catalog_calls = [call for call in calls if call[1] == "tool catalog"]
    assert len(catalog_calls) == 1
    command, _, env = catalog_calls[0]
    assert command[-2:] == ["--products", "clearpass"]
    assert env is not None
    assert set(env) == {
        "HPE_MCP_ACCESS_PROFILE",
        "HPE_MCP_PRODUCTS",
        "HPE_MCP_PRODUCT_ACCESS",
    }
    assert env["HPE_MCP_ACCESS_PROFILE"] == "custom"
    assert env["HPE_MCP_PRODUCTS"] == "clearpass"
    assert env["HPE_MCP_PRODUCT_ACCESS"] == "read-only"


def test_with_products_catalog_uses_all_products_without_tokens(monkeypatch):
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_wizard.py",
            "--yes",
            "--skip-install",
            "--skip-credentials",
            "--skip-stdio",
            "--skip-http",
            "--skip-doctor",
            "--with-products",
            "--product-access",
            "read-only",
        ],
    )
    monkeypatch.setattr(
        setup_wizard,
        "_write_env_file",
        lambda *args, **kwargs: setup_wizard.Step(".env", "OK", "captured"),
    )

    def fake_run(
        command: list[str],
        label: str,
        *,
        env: dict[str, str] | None = None,
    ) -> setup_wizard.Step:
        calls.append((command, label, env))
        return setup_wizard.Step(label, "OK", "captured")

    monkeypatch.setattr(setup_wizard, "_run", fake_run)

    assert setup_wizard.main() == 0

    catalog_calls = [call for call in calls if call[1] == "tool catalog"]
    assert len(catalog_calls) == 1
    command, _, env = catalog_calls[0]
    products = ",".join(setup_wizard.PRODUCT_ENV)
    assert command[-2:] == ["--products", products]
    assert env == {
        "HPE_MCP_ACCESS_PROFILE": "custom",
        "HPE_MCP_PRODUCTS": products,
        "HPE_MCP_PRODUCT_ACCESS": "read-only",
    }


def test_default_custom_profile_updates_an_existing_env(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("HPE_MCP_ACCESS_PROFILE=full-read-write\n")
    captured: list[dict[str, str]] = []
    monkeypatch.setattr(setup_wizard, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_wizard.py",
            "--yes",
            "--skip-install",
            "--skip-credentials",
            "--skip-stdio",
            "--skip-http",
            "--skip-catalog",
            "--skip-doctor",
        ],
    )
    monkeypatch.setattr(
        setup_wizard,
        "_write_env_file",
        lambda target, env, force: (
            captured.append(env.copy())
            or setup_wizard.Step(".env", "OK", "captured")
        ),
    )

    assert setup_wizard.main() == 0
    assert captured == [
        {
            "HPE_MCP_ROUTER_MODE": "minimal",
            "HPE_MCP_ACCESS_PROFILE": "custom",
            "HPE_MCP_PRODUCTS": "",
            "HPE_MCP_PRODUCT_ACCESS": "read-only",
        }
    ]


def test_write_secret_file_is_owner_only(tmp_path):
    """Secret files (credentials.yaml / .env) are created 0600, not
    world-readable under the default umask."""
    import stat

    target = tmp_path / "credentials.yaml"
    setup_wizard._write_secret_file(target, "client_secret: hunter2\n")

    assert target.read_text() == "client_secret: hunter2\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_secret_file_tightens_preexisting_world_readable_file(tmp_path):
    """A pre-existing 0644 file is tightened to 0600 before the secret bytes
    land in it (O_CREAT's mode only applies to newly-created files)."""
    import os
    import stat

    target = tmp_path / ".env"
    target.write_text("PLACEHOLDER=1\n")
    os.chmod(target, 0o644)

    setup_wizard._write_secret_file(target, "MIST_API_TOKEN=secret\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == "MIST_API_TOKEN=secret\n"
