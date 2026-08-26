#!/usr/bin/env python3
"""Interactive local setup wizard for hpe-networking-mcp."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# K2 surface for the --docker slices (W2/W3 consume exactly these names).
# They bind ROOT at import time: the local-uv flow in main() derives
# ROOT / ".env" live instead so tests patching only ROOT keep working, while
# docker-mode code reads these constants and tests patch them alongside ROOT.
SECRETS_DIR = ROOT / "secrets"

BEARER_TOKEN_PATH = SECRETS_DIR / "mcp_http_bearer_token"
DOCKER_CREDENTIALS_PATH = SECRETS_DIR / "credentials.yaml"
# Generated compose overlay; gitignored, written by --docker (W2).
OVERLAY_PATH = ROOT / "docker-compose.router.local.yml"
ENV_PATH = ROOT / ".env"

# R1d guard: the published-port bind segment must be an explicit dotted-quad
# IPv4 address, which makes the shorthand "<port>:<port>" form (LAN-wide
# publish) structurally unreachable in generated overlays.
_PUBLISH_BIND_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")

NON_LOOPBACK_WARNING = (
    "WARNING: Non-loopback MCP_HOST may expose credential-backed MCP tools "
    "to your network. Use firewall/auth/TLS before sharing this endpoint.\n"
)

CENTRAL_BASE_URLS = [
    (
        "US / common API gateway",
        "https://apigw-prod2.central.arubanetworks.com",
    ),
    (
        "EU Central",
        "https://apigw-eucentral3.central.arubanetworks.com",
    ),
    (
        "APAC",
        "https://apigw-apac.central.arubanetworks.com",
    ),
    (
        "Legacy/internal gateway",
        "https://internal.api.central.arubanetworks.com",
    ),
]

PRODUCT_ENV = {
    "clearpass": {
        "label": "ClearPass",
        "vars": {
            "CLEARPASS_BASE_URL": "https://clearpass.example.com",
            "CLEARPASS_API_TOKEN": "YOUR_CLEARPASS_API_TOKEN",
        },
    },
    "mist": {
        "label": "Juniper Mist",
        "vars": {
            "MIST_HOST": "https://api.mist.com",
            "MIST_API_TOKEN": "YOUR_MIST_API_TOKEN",
        },
    },
    "apstra": {
        "label": "Apstra",
        "vars": {
            "APSTRA_BASE_URL": "https://apstra.example.com",
            "APSTRA_API_TOKEN": "YOUR_APSTRA_API_TOKEN",
        },
    },
    "aos8": {
        "label": "ArubaOS 8",
        "vars": {
            "AOS8_BASE_URL": "https://mobility-conductor.example.com",
            "AOS8_API_TOKEN": "YOUR_AOS8_API_TOKEN",
        },
    },
    "edgeconnect": {
        "label": "EdgeConnect",
        "vars": {
            "EDGECONNECT_BASE_URL": "https://orchestrator.example.com",
            "EDGECONNECT_API_TOKEN": "YOUR_EDGECONNECT_API_TOKEN",
            "EDGECONNECT_AUTH_HEADER": "Authorization",
        },
    },
    "uxi": {
        "label": "HPE Aruba UXI",
        "vars": {
            "UXI_CLIENT_ID": "YOUR_UXI_CLIENT_ID",
            "UXI_CLIENT_SECRET": "YOUR_UXI_CLIENT_SECRET",
            "UXI_BASE_URL": "https://api.capenetworks.com/networking-uxi/v1alpha1",
            "UXI_TOKEN_URL": "https://sso.common.cloud.hpe.com/as/token.oauth2",
        },
    },
    "axis": {
        "label": "Axis Atmos Cloud",
        "vars": {
            "AXIS_BASE_URL": "https://admin-api.axissecurity.com/api/v1.0",
            "AXIS_API_TOKEN": "YOUR_AXIS_API_TOKEN",
        },
    },
    "design": {
        "label": "Network design diagrams (Draw.io / Graphviz / NeXt)",
        # No required secrets. Optionally set HPE_MCP_DIAGRAM_ICON_DIR in .env
        # to a local vendor icon pack (see resources/diagram_icons/README.md).
        "vars": {},
    },
}
PLACEHOLDER_MARKERS = ("YOUR_", "REPLACE_ME", "PLACEHOLDER")
SECRET_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")
PLATFORM_WRITE_ENV_VARS = (
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_GLP_V2BETA1_WRITES",
    "HPE_MCP_AOS8_WRITES",
    "HPE_MCP_EDGECONNECT_WRITES",
    "HPE_MCP_APSTRA_WRITES",
    "HPE_MCP_MIST_WRITES",
    "HPE_MCP_CLEARPASS_WRITES",
    "HPE_MCP_UXI_WRITES",
    "HPE_MCP_AXIS_WRITES",
)
PROFILE_WRITE_ENV_VARS = ("HPE_MCP_READONLY", *PLATFORM_WRITE_ENV_VARS)


@dataclass
class Step:
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class DockerManifest:
    """Cross-slice contract K1: everything downstream docker slices consume.

    W2 consumes host_ip/client_hostname/rag for overlay emission (the latter
    two prompted since W2); W3 reads backend/products/access_profile for .env
    keys. backend stays neutral here until W3 lands its prompt.
    """

    port: int
    host_ip: str | None
    client_hostname: str | None
    rag: bool
    backend: str | None
    products: list[str]
    access_profile: str
    token_path: Path
    creds_path: Path


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_secret_file(target: Path, text: str) -> None:
    """Write a file holding secrets with 0600 perms (owner-only).

    Plain ``write_text()`` creates world-readable files under the default
    umask — these files hold longer-lived secrets (client_secret, product API
    tokens) than the token cache, which already enforces 0600. ``os.fchmod``
    tightens a pre-existing (possibly 0644) file BEFORE the secret bytes land
    in it, since O_CREAT's mode only applies to newly-created files.
    The text fd passes ``newline="\n"`` so win32 hosts emit LF-only secret
    bytes instead of CRLF (which would corrupt byte-exact consumers such as
    the bearer token).
    """
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _ask(prompt: str, default: bool, *, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_secret(prompt: str, default: str) -> str:
    answer = getpass.getpass(f"{prompt} [leave blank to keep placeholder]: ").strip()
    return answer or default


def _is_secret_env_var(name: str) -> bool:
    return name.endswith(SECRET_ENV_SUFFIXES)


def _csv(values: str) -> list[str]:
    return [item.strip().lower() for item in values.split(",") if item.strip()]


def _selected_products(args: argparse.Namespace) -> list[str]:
    def validate(requested: list[str]) -> list[str]:
        if "all" in requested:
            return list(PRODUCT_ENV)
        unknown = sorted(set(requested) - set(PRODUCT_ENV))
        if unknown:
            accepted = ", ".join([*PRODUCT_ENV, "all"])
            raise SystemExit(
                f"Unknown optional product(s): {', '.join(unknown)}. Accepted values: {accepted}"
            )
        return requested

    if args.with_products:
        return list(PRODUCT_ENV)
    if args.products:
        return validate(_csv(args.products))
    if args.yes:
        return []
    if not _ask("Enable optional product starter backends?", False, assume_yes=False):
        return []

    print("\nOptional products")
    for name, meta in PRODUCT_ENV.items():
        print(f"  - {name}: {meta['label']}")
    raw = _ask_text("Enter products as comma-separated names, or all", "")
    return validate(_csv(raw))


def _product_access(args: argparse.Namespace, selected_products: list[str]) -> str:
    profile = getattr(args, "access_profile", "custom")
    value = getattr(args, "product_access", None)
    if profile == "safe-read-only":
        if value == "read-write":
            raise SystemExit(
                "--product-access read-write conflicts with "
                "--access-profile safe-read-only"
            )
        return "read-only"
    if profile == "full-read-write":
        if value == "read-only":
            raise SystemExit(
                "--product-access read-only conflicts with "
                "--access-profile full-read-write"
            )
        return "read-write"
    if profile != "custom":
        raise SystemExit(
            "--access-profile must be one of: safe-read-only, custom, full-read-write"
        )
    if not selected_products:
        return "read-only"
    value = value or "read-only"
    if value not in {"read-only", "read-write"}:
        raise SystemExit("--product-access must be one of: read-only, read-write")
    return value


def _choose_base_url(label: str, *, default: str, assume_yes: bool) -> str:
    if assume_yes:
        return default

    print(f"\n{label} base URL")
    for idx, (name, url) in enumerate(CENTRAL_BASE_URLS, start=1):
        print(f"  {idx}. {name}: {url}")
    print(f"  {len(CENTRAL_BASE_URLS) + 1}. Custom URL")

    choice = _ask_text("Choose a Central API gateway", "1")
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(CENTRAL_BASE_URLS):
            return CENTRAL_BASE_URLS[index - 1][1]
        if index == len(CENTRAL_BASE_URLS) + 1:
            return _ask_text("Custom Central API base URL", default)
    return default


def _yaml_string(value: str) -> str:
    return json.dumps(value)


def _shell_line(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}"


def _env_assignment_key(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    key, sep, _ = line.partition("=")
    if not sep:
        return None
    key = key.strip()
    return key if key else None


def _env_assignment_value(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    _, sep, value = line.partition("=")
    if not sep:
        return None
    try:
        parsed = shlex.split(value, comments=True, posix=True)
    except ValueError:
        return None
    return parsed[0] if len(parsed) == 1 else None


def _normalized_access_profile(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _is_placeholder_value(value: str) -> bool:
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _should_replace_env_assignment(line: str, env: dict[str, str]) -> bool:
    key = _env_assignment_key(line)
    if key not in env:
        return False
    if key in {
        "HPE_MCP_ACCESS_PROFILE",
        "HPE_MCP_PRODUCTS",
        "HPE_MCP_PRODUCT_ACCESS",
        *PROFILE_WRITE_ENV_VARS,
    }:
        return True
    return _is_placeholder_value(line) and not _is_placeholder_value(env[key])


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def _write_from_template(
    source: Path,
    target: Path,
    *,
    force: bool,
    replacements: dict[str, str] | None = None,
) -> Step:
    if target.exists() and not force:
        return Step(_rel(target), "SKIP", "already exists; use --force to overwrite")
    text = source.read_text()
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return Step(_rel(target), "OK", f"created from {_rel(source)}")


def _has_placeholders(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(errors="replace")
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


def _write_credentials(target: Path, *, force: bool, assume_yes: bool) -> Step:
    if target.exists() and not force:
        if assume_yes or not _has_placeholders(target):
            return Step(_rel(target), "SKIP", "already exists; use --force to overwrite")
        if not _ask(
            "Existing config/credentials.yaml contains placeholders; update it now?",
            True,
            assume_yes=False,
        ):
            return Step(_rel(target), "SKIP", "left existing placeholder file unchanged")

    default_url = CENTRAL_BASE_URLS[0][1]
    central_url = _choose_base_url(
        "Central/source account",
        default=default_url,
        assume_yes=assume_yes,
    )
    target_url = central_url
    if not assume_yes and not _ask(
        "Use the same Central base URL for the target/GLP account?",
        True,
        assume_yes=False,
    ):
        target_url = _choose_base_url("Target/GLP account", default=default_url, assume_yes=False)

    values = {
        "central_client_id": "YOUR_CENTRAL_CLIENT_ID",
        "central_client_secret": "YOUR_CENTRAL_CLIENT_SECRET",
        "central_workspace": "YOUR_GLP_WORKSPACE_ID",
        "target_client_id": "YOUR_GLP_CLIENT_ID",
        "target_client_secret": "YOUR_GLP_CLIENT_SECRET",
        "target_workspace": "YOUR_GLP_WORKSPACE_ID",
    }

    if not assume_yes and _ask("Fill OAuth credentials now?", False, assume_yes=False):
        values["central_client_id"] = _ask_text(
            "Central client ID",
            values["central_client_id"],
        )
        values["central_client_secret"] = _ask_secret(
            "Central client secret",
            values["central_client_secret"],
        )
        values["central_workspace"] = _ask_text(
            "Central GLP workspace ID",
            values["central_workspace"],
        )
        if _ask("Fill separate target/GLP OAuth credentials?", False, assume_yes=False):
            values["target_client_id"] = _ask_text(
                "Target/GLP client ID",
                values["target_client_id"],
            )
            values["target_client_secret"] = _ask_secret(
                "Target/GLP client secret",
                values["target_client_secret"],
            )
            values["target_workspace"] = _ask_text(
                "Target/GLP workspace ID",
                values["target_workspace"],
            )
        else:
            values["target_client_id"] = values["central_client_id"]
            values["target_client_secret"] = values["central_client_secret"]
            values["target_workspace"] = values["central_workspace"]

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_file(
        target,
        "\n".join(
            [
                "# Generated by scripts/setup_wizard.py.",
                "# credentials.yaml is gitignored - never commit real credentials.",
                "# Common Central API gateways:",
                *[f"#   {name}: {url}" for name, url in CENTRAL_BASE_URLS],
                "",
                "central_account:",
                f"  base_url: {_yaml_string(central_url)}",
                f"  client_id: {_yaml_string(values['central_client_id'])}",
                f"  client_secret: {_yaml_string(values['central_client_secret'])}",
                f"  glp_workspace_id: {_yaml_string(values['central_workspace'])}",
                "",
                "glp_account:",
                f"  base_url: {_yaml_string(target_url)}",
                f"  client_id: {_yaml_string(values['target_client_id'])}",
                f"  client_secret: {_yaml_string(values['target_client_secret'])}",
                f"  glp_workspace_id: {_yaml_string(values['target_workspace'])}",
                "",
                "glp:",
                '  token_url: "https://sso.common.cloud.hpe.com/as/token.oauth2"',
                '  base_url: "https://global.api.greenlake.hpe.com"',
                "",
            ]
        )
    )
    return Step(_rel(target), "OK", "created with region choices and placeholders/secrets")


def _product_env(
    selected_products: list[str],
    *,
    assume_yes: bool,
    product_access: str = "read-only",
    access_profile: str = "custom",
) -> dict[str, str]:
    env: dict[str, str] = {
        "HPE_MCP_ACCESS_PROFILE": access_profile,
        "HPE_MCP_PRODUCTS": "",
    }
    if access_profile == "safe-read-only":
        env["HPE_MCP_READONLY"] = "1"
        env["HPE_MCP_PRODUCT_ACCESS"] = "read-only"
        env.update({name: "0" for name in PLATFORM_WRITE_ENV_VARS})
    elif access_profile == "full-read-write":
        env["HPE_MCP_READONLY"] = "0"
        env["HPE_MCP_PRODUCT_ACCESS"] = "read-write"
        env.update({name: "1" for name in PLATFORM_WRITE_ENV_VARS})
    else:
        env["HPE_MCP_PRODUCT_ACCESS"] = product_access
    if not selected_products:
        return env

    env["HPE_MCP_PRODUCTS"] = ",".join(selected_products)
    env["HPE_MCP_PRODUCT_ACCESS"] = product_access
    for product in selected_products:
        meta = PRODUCT_ENV[product]
        print(f"\n{meta['label']} settings")
        for name, default in meta["vars"].items():
            if assume_yes:
                env[name] = default
            elif _is_secret_env_var(name):
                env[name] = _ask_secret(name, default)
            else:
                env[name] = _ask_text(name, default)
    return env


def _write_env_file(target: Path, env: dict[str, str], *, force: bool) -> Step:
    if not env:
        return Step(_rel(target), "SKIP", "no runtime environment updates requested")
    if target.exists() and not force:
        try:
            lines = target.read_text().splitlines()
        except UnicodeDecodeError as exc:
            return Step(
                _rel(target), "WARN", f"could not merge existing entries: {exc}"
            )
        existing = {key for line in lines if (key := _env_assignment_key(line))}
        existing_profile = next(
            (
                _env_assignment_value(line)
                for line in lines
                if _env_assignment_key(line) == "HPE_MCP_ACCESS_PROFILE"
            ),
            None,
        )
        clear_aggregate_gates = (
            env.get("HPE_MCP_ACCESS_PROFILE") == "custom"
            and _normalized_access_profile(existing_profile)
            in {"safe-read-only", "full-read-write"}
        )
        update_keys = {
            "HPE_MCP_ACCESS_PROFILE",
            "HPE_MCP_PRODUCTS",
            "HPE_MCP_PRODUCT_ACCESS",
            *PROFILE_WRITE_ENV_VARS,
        }
        updated_lines = []
        for line in lines:
            key = _env_assignment_key(line)
            if clear_aggregate_gates and key in PROFILE_WRITE_ENV_VARS:
                continue
            if key in env and (
                key in update_keys or _should_replace_env_assignment(line, env)
            ):
                updated_lines.append(_shell_line(key, env[key]))
            else:
                updated_lines.append(line)
        additions = [
            _shell_line(name, value)
            for name, value in env.items()
            if name not in existing
        ]
        if additions:
            if updated_lines and updated_lines[-1].strip():
                updated_lines.append("")
            updated_lines.extend(additions)
        _write_secret_file(target, "\n".join([*updated_lines, ""]))
        detail = (
            "merged runtime settings; existing token values preserved"
            if additions or updated_lines != lines
            else "already contains requested runtime settings"
        )
        return Step(_rel(target), "OK", detail)
    lines = [
        "# Generated by scripts/setup_wizard.py.",
        "# This file is gitignored. It can contain optional product tokens.",
        *[_shell_line(name, value) for name, value in env.items()],
        "",
    ]
    _write_secret_file(target, "\n".join(lines))
    return Step(_rel(target), "OK", "created runtime environment file")


def _merge_json_env(target: Path, server_name: str, env: dict[str, str]) -> Step:
    if not env:
        return Step(_rel(target), "SKIP", "no MCP environment updates requested")
    mcp_env = {
        name: env[name]
        for name in (
            "HPE_MCP_ACCESS_PROFILE",
            "HPE_MCP_ROUTER_MODE",
            "HPE_MCP_PRODUCTS",
            "HPE_MCP_PRODUCT_ACCESS",
            *PROFILE_WRITE_ENV_VARS,
        )
        if name in env
    }
    if not mcp_env:
        return Step(_rel(target), "SKIP", "no MCP environment updates requested")
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return Step(_rel(target), "WARN", f"could not update optional product env: {exc}")

    servers = data.get("mcpServers") or data.get("servers") or {}
    server = servers.get(server_name)
    if not isinstance(server, dict):
        return Step(_rel(target), "WARN", f"{server_name} server entry not found")
    server_env = server.setdefault("env", {})
    if not isinstance(server_env, dict):
        return Step(_rel(target), "WARN", f"{server_name} env is not an object")
    if (
        mcp_env.get("HPE_MCP_ACCESS_PROFILE") == "custom"
        and _normalized_access_profile(server_env.get("HPE_MCP_ACCESS_PROFILE"))
        in {"safe-read-only", "full-read-write"}
    ):
        for name in PROFILE_WRITE_ENV_VARS:
            server_env.pop(name, None)
    server_env.update(mcp_env)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return Step(_rel(target), "OK", "added optional product selector")


def _catalog_env(env: dict[str, str]) -> dict[str, str]:
    return {
        name: env[name]
        for name in (
            "HPE_MCP_ACCESS_PROFILE",
            "HPE_MCP_PRODUCTS",
            "HPE_MCP_PRODUCT_ACCESS",
            *PROFILE_WRITE_ENV_VARS,
        )
        if name in env
    }


def _run(command: list[str], label: str, *, env: dict[str, str] | None = None) -> Step:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    try:
        subprocess.run(command, cwd=ROOT, check=True, env=run_env)
    except FileNotFoundError as exc:
        return Step(label, "WARN", f"command not found: {exc.filename}")
    except subprocess.CalledProcessError as exc:
        return Step(label, "WARN", f"command exited {exc.returncode}")
    return Step(label, "OK", "completed")


def _print_steps(steps: list[Step]) -> None:
    print("\nSetup summary")
    for step in steps:
        print(f"[{step.status}] {step.label}: {step.detail}")


def _resolve_docker_exposure(args: argparse.Namespace) -> str | None:
    """Return the published bind address for --docker, or None for loopback-only."""
    exposes = [value.strip() for value in (args.expose or [])]
    if len(exposes) == 1:
        raise SystemExit(
            "--expose must be passed TWICE with the same address to acknowledge "
            f"a non-loopback deployment (got only: {exposes[0]})"
        )
    if len(set(exposes)) > 1:
        raise SystemExit("--expose addresses disagree; pass the same address twice")
    if exposes:
        address = exposes[0]
        if not address:
            raise SystemExit("--expose requires an address")
        if _is_loopback_host(address):
            print("Loopback --expose address; keeping the deployment loopback-only.\n")
            return None
        print(NON_LOOPBACK_WARNING)
        print(
            "Firewall rules, a reverse proxy, and TLS are REQUIRED before "
            "sharing this endpoint.\n"
        )
        return address
    if not _is_loopback_host(args.host):
        if args.yes:
            raise SystemExit(
                f"--host {args.host} is non-loopback and --yes cannot acknowledge "
                "exposure; pass --expose <ip> twice, or rerun with --host "
                f"{DEFAULT_HOST} to stay loopback-only"
            )
        if not _ask(
            f"Expose the router beyond loopback on {args.host}?", False, assume_yes=False
        ):
            raise SystemExit(
                f"--host {args.host} is non-loopback but exposure was declined; rerun "
                f"with --host {DEFAULT_HOST}, or pass --expose <ip> twice to acknowledge"
            )
        confirmed = _ask_text("Re-enter the bind address to confirm", "")
        if confirmed != args.host:
            raise SystemExit(
                "Bind addresses did not match; refusing to expose. Rerun with "
                f"--host {DEFAULT_HOST}, or retry with both entries identical."
            )
        print(NON_LOOPBACK_WARNING)
        print(
            "Firewall rules, a reverse proxy, and TLS are REQUIRED before "
            "sharing this endpoint.\n"
        )
        return args.host
    return None


def _validate_client_hostname(answer: str) -> str:
    """Reject values that could fake ports/wildcards inside an allowlist."""
    value = answer.strip()
    if not value:
        raise SystemExit("client-facing hostname cannot be empty")
    if re.search(r"\s", value) or any(ch in value for ch in ':,*"\''):
        raise SystemExit(
            f"invalid client-facing hostname {value!r}: use a bare hostname or "
            "IP with no whitespace, colons, commas, or wildcard characters"
        )
    return value


def _resolve_client_hostname(host_ip: str | None, *, assume_yes: bool) -> str:
    """W2 prompt: what clients type in their MCP client config (R3 source).

    Loopback deployments face local clients only; acknowledged non-loopback
    deployments default to the acknowledged address but accept a DNS hostname.
    The bind address is never silently promoted into the allowlists -- the
    value here is the operator-stated client-facing name.
    """
    if host_ip is None:
        return "localhost"
    if assume_yes:
        return host_ip
    return _validate_client_hostname(
        _ask_text(
            "Hostname clients will use to reach the router "
            "(e.g. mcp.example.com; blank for the bare address)",
            host_ip,
        )
    )


def _choose_rag_image(*, assume_yes: bool) -> bool:
    """W2 prompt: RAG-capable image choice (opt-in; default stays non-RAG)."""
    return _ask(
        "Use the RAG-enabled image hpe-networking-mcp-router:rag "
        "(must be built separately)?",
        False,
        assume_yes=assume_yes,
    )


def _published_port_spec(host_ip: str | None, port: int) -> str:
    """R1d: emit only a literal "<bind>:<port>:<port>" publish line."""
    bind = DEFAULT_HOST if host_ip is None else host_ip.strip()
    spec = f"{bind}:{port}:{port}"
    if not _PUBLISH_BIND_RE.fullmatch(bind):
        raise SystemExit(
            f"refusing to emit published-port line {spec!r}: the bind must be "
            'an explicit IPv4 address -- the shorthand "<port>:<port>" form '
            "publishes on every interface"
        )
    return spec


def _compose_allowlists(client_hostname: str | None) -> tuple[str, str]:
    """R3: explicit client-facing entries only -- never the bind address."""
    hosts: list[str] = []
    for candidate in ("127.0.0.1", "localhost", client_hostname or ""):
        if candidate and candidate not in hosts:
            hosts.append(candidate)
    allowed_hosts = ",".join(f"{host}:*" for host in hosts)
    allowed_origins = ",".join(f"http://{host}:*" for host in hosts)
    return allowed_hosts, allowed_origins


def _compose_overlay_text(manifest: DockerManifest) -> str:
    """Render docker-compose.router.local.yml from the K1 manifest."""
    published = _published_port_spec(manifest.host_ip, manifest.port)
    allowed_hosts, allowed_origins = _compose_allowlists(manifest.client_hostname)
    port = str(manifest.port)
    if manifest.rag:
        image_block = "    image: hpe-networking-mcp-router:rag\n"
        rag_mounts = (
            "      - ./data/docs.lance:/app/data/docs.lance:ro\n"
            "      - ./data/tools.lance:/app/data/tools.lance:ro\n"
        )
    else:
        image_block = (
            "    build:\n"
            "      context: .\n"
            "      dockerfile: Dockerfile\n"
            "    image: hpe-networking-mcp-router:local\n"
        )
        rag_mounts = ""
    lines = [
        "# Generated by scripts/setup_wizard.py --docker; regenerate with --force.",
        "# Layer it over the tracked bundle:",
        "#   docker compose -f docker-compose.yml \\",
        "#     -f docker-compose.router.local.yml --profile router up -d mcp-router",
        "services:",
        "  mcp-router:",
    ]
    lines.extend(image_block.rstrip("\n").split("\n"))
    lines.extend(
        [
            "    profiles:",
            "      - router",
            "    ports:",
            f'      - "{published}"',
            "    environment:",
            "      MCP_TRANSPORT: streamable-http",
            '      MCP_HOST: "0.0.0.0"',
            f'      MCP_PORT: "{port}"',
            f'      MCP_ALLOWED_HOSTS: "{allowed_hosts}"',
            f'      MCP_ALLOWED_ORIGINS: "{allowed_origins}"',
            '      HPE_MCP_ROUTER_MODE: "${HPE_MCP_ROUTER_MODE:-minimal}"',
            '      HPE_MCP_TOOLSETS: "${HPE_MCP_TOOLSETS:-central,glp,rag}"',
            '      HPE_MCP_ACCESS_PROFILE: "${HPE_MCP_ACCESS_PROFILE:-custom}"',
            '      HPE_MCP_RAG_BACKEND: "${HPE_MCP_RAG_BACKEND:-}"',
            "      CREDS_PATH: /run/secrets/credentials_yaml",
            "      MCP_HTTP_BEARER_TOKEN_FILE: /run/secrets/mcp_http_bearer_token",
            "    secrets:",
            "      - credentials_yaml",
            "      - mcp_http_bearer_token",
            "    volumes:",
            "      - router_state:/app/state",
            "      - router_outputs:/app/outputs",
        ]
    )
    if rag_mounts:
        lines.extend(rag_mounts.rstrip("\n").split("\n"))
    lines.extend(
        [
            "    restart: unless-stopped",
            "",
            "secrets:",
            "  credentials_yaml:",
            "    file: ./secrets/credentials.yaml",
            "  mcp_http_bearer_token:",
            "    file: ./secrets/mcp_http_bearer_token",
            "",
            "volumes:",
            "  router_state:",
            "  router_outputs:",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_compose_overlay(manifest: DockerManifest, *, force: bool) -> Step:
    """Emit the compose overlay LAST (K3): only after the secret gate passes."""
    text = _compose_overlay_text(manifest)  # R1d refusal happens before any I/O
    if OVERLAY_PATH.exists() and not force:
        return Step(
            _rel(OVERLAY_PATH),
            "OK",
            "kept existing overlay (rerun with --force to regenerate)",
        )
    with open(OVERLAY_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return Step(_rel(OVERLAY_PATH), "OK", "created compose overlay")


def _docker_token_step(args: argparse.Namespace) -> Step:
    """Generate + write the HTTP bearer token; the value never reaches stdout.

    An existing file holding a valid 64-hex token is kept byte-for-byte (with
    a 0600 repair) unless --force rotates it; invalid content aborts without
    being silently replaced.
    """
    BEARER_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BEARER_TOKEN_PATH.exists() and not args.force:
        try:
            existing = BEARER_TOKEN_PATH.read_text(encoding="utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            existing = ""
        if re.fullmatch(r"[0-9a-f]{64}", existing):
            os.chmod(BEARER_TOKEN_PATH, 0o600)
            return Step(
                _rel(BEARER_TOKEN_PATH), "OK", "kept existing token (mode 0600)"
            )
        raise SystemExit(
            f"{_rel(BEARER_TOKEN_PATH)} exists but holds no valid 64-hex bearer "
            "token; refusing to overwrite it silently. Fix or remove the file, "
            "or rerun with --force to replace it."
        )
    _write_secret_file(BEARER_TOKEN_PATH, secrets.token_hex(32) + "\n")
    return Step(_rel(BEARER_TOKEN_PATH), "OK", "created 64-hex bearer token (mode 0600)")


def _finalize_docker_deployment(manifest: DockerManifest) -> None:
    """K3 seam: validate emitted secrets BEFORE any overlay/.env can exist.

    Slices W2/W3 write OVERLAY_PATH/ENV_PATH only after this gate passes, so
    an abort never leaves a compose file referencing missing secret files.
    Amended (W1a, Lead-ratified): an acknowledged non-loopback deployment
    additionally requires credentials.yaml to exist and be placeholder-free,
    since compose bind-mounts it and a missing host path would be auto-created
    as a directory.
    """
    if manifest.host_ip is None:
        return
    if not manifest.creds_path.exists():
        raise SystemExit(
            f"{_rel(manifest.creds_path)} is required for a non-loopback "
            "deployment; refusing to finish without it. Rerun without "
            "--skip-credentials to seed it, or without --host/--expose to "
            "stay loopback-only."
        )
    if _has_placeholders(manifest.creds_path):
        raise SystemExit(
            f"{_rel(manifest.creds_path)} still contains placeholder credentials; "
            "refusing to finish a non-loopback deployment. Fill in real OAuth "
            "credentials, or rerun without --host/--expose to stay loopback-only."
        )


def _print_docker_next_steps(manifest: DockerManifest, *, product_access: str) -> None:
    print("\nNext steps (Docker bundle)")
    if _has_placeholders(manifest.creds_path):
        print(
            f"1. Fill placeholders in {_rel(manifest.creds_path)} before starting "
            "API-backed tools."
        )
    else:
        print(f"1. Review {_rel(manifest.creds_path)} before starting API-backed tools.")
    print(f"2. Bearer token file: {_rel(manifest.token_path)} (0600; value never printed).")
    if manifest.host_ip is not None:
        print(
            f"3. Publishing on {manifest.host_ip}:{manifest.port}; firewall/reverse-proxy/"
            "TLS are REQUIRED before sharing this endpoint."
        )
    else:
        print(f"3. Loopback-only publishing on 127.0.0.1:{manifest.port}.")
    next_step = 4
    if manifest.products:
        print(f"4. Optional products enabled: {', '.join(manifest.products)} ({product_access}).")
        next_step = 5
    print(
        f"{next_step}. Compose overlay written to {_rel(OVERLAY_PATH)}: start it "
        "with docker compose -f docker-compose.yml -f "
        "docker-compose.router.local.yml --profile router up -d mcp-router "
        "(six-step checklist in docs/production-deployment.md remains the "
        "manual fallback)."
    )


def _run_docker_mode(args: argparse.Namespace) -> DockerManifest:
    """--docker flow: resolve exposure, prompt W2 choices, emit secrets +
    compose overlay.

    All interactive prompts and refusals precede the first write; overlay/.env
    emission MUST hook in after _finalize_docker_deployment (K3 write-order
    invariant). .env emission itself arrives in W3.
    """
    host_ip = _resolve_docker_exposure(args)
    selected_products = _selected_products(args)
    product_access = _product_access(args, selected_products)
    client_hostname = _resolve_client_hostname(host_ip, assume_yes=args.yes)
    rag_image = _choose_rag_image(assume_yes=args.yes)

    steps: list[Step] = [_docker_token_step(args)]
    if not args.skip_credentials:
        steps.append(
            _write_credentials(
                DOCKER_CREDENTIALS_PATH,
                force=args.force,
                assume_yes=args.yes,
            )
        )

    manifest = DockerManifest(
        port=args.port,
        host_ip=host_ip,
        client_hostname=client_hostname,
        rag=rag_image,
        backend=None,
        products=selected_products,
        access_profile=args.access_profile,
        token_path=BEARER_TOKEN_PATH,
        creds_path=DOCKER_CREDENTIALS_PATH,
    )
    _finalize_docker_deployment(manifest)
    steps.append(_write_compose_overlay(manifest, force=args.force))
    _print_steps(steps)
    _print_docker_next_steps(manifest, product_access=product_access)
    print(f"\nRouter exposure mode: {args.router_mode}.")
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="accept default wizard choices")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing local config files",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"HTTP MCP host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP MCP port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--expose",
        action="append",
        default=None,
        metavar="IP",
        help=(
            "publish the router on this non-loopback address instead of binding "
            "loopback; MUST be passed twice with the same value to acknowledge "
            "the exposure (--docker only)"
        ),
    )
    parser.add_argument("--with-vscode", action="store_true", help="also create .vscode/mcp.json")
    parser.add_argument(
        "--with-products",
        action="store_true",
        help="enable all optional product starters",
    )
    parser.add_argument(
        "--products",
        default="",
        help=(
            "comma-separated optional products to enable "
            "(clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design,all)"
        ),
    )
    parser.add_argument(
        "--access-profile",
        choices=("safe-read-only", "custom", "full-read-write"),
        default="custom",
        help=(
            "aggregate write-access profile (default: custom, preserving the "
            "existing per-platform gates)"
        ),
    )
    parser.add_argument(
        "--product-access",
        choices=("read-only", "read-write"),
        default=None,
        help=(
            "optional-product access used by the custom profile "
            "(default: read-only)"
        ),
    )
    parser.add_argument(
        "--router-mode",
        choices=("minimal", "default", "direct"),
        default="minimal",
        help=(
            "router exposure mode: minimal discovery, default convenience wrappers, "
            "or direct registration of every enabled backend tool"
        ),
    )
    parser.add_argument("--skip-install", action="store_true", help="do not run uv sync")
    parser.add_argument(
        "--skip-credentials",
        action="store_true",
        help="do not create config/credentials.yaml",
    )
    parser.add_argument("--skip-stdio", action="store_true", help="do not create .mcp.json")
    parser.add_argument("--skip-http", action="store_true", help="do not create .mcp.http.json")
    parser.add_argument(
        "--skip-catalog",
        action="store_true",
        help="do not build the router tool catalog",
    )
    parser.add_argument(
        "--skip-doctor",
        action="store_true",
        help="do not run scripts/doctor.py at the end",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help=(
            "emit the Docker deployment bundle (bearer token + container "
            "credentials.yaml) instead of the local uv-run config"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print("hpe-networking-mcp setup wizard")
    print(f"Repository: {ROOT}")
    print("This wizard writes only local git-ignored config files.\n")
    if args.docker:
        _run_docker_mode(args)
        return 0
    if not _is_loopback_host(args.host):
        print(NON_LOOPBACK_WARNING)

    steps: list[Step] = []
    selected_products = _selected_products(args)
    product_access = _product_access(args, selected_products)
    product_env = _product_env(
        selected_products,
        assume_yes=args.yes,
        product_access=product_access,
        access_profile=args.access_profile,
    )
    runtime_env = {"HPE_MCP_ROUTER_MODE": args.router_mode, **product_env}

    if not args.skip_install and _ask(
        "Install dependencies with uv sync?",
        True,
        assume_yes=args.yes,
    ):
        steps.append(_run(["uv", "sync"], "install dependencies"))

    if not args.skip_credentials and _ask(
        "Create config/credentials.yaml?", True, assume_yes=args.yes
    ):
        steps.append(
            _write_credentials(
                ROOT / "config" / "credentials.yaml",
                force=args.force,
                assume_yes=args.yes,
            )
        )

    env_path = ROOT / ".env"
    if (
        selected_products
        or args.router_mode != "minimal"
        or args.access_profile != "custom"
        or env_path.exists()
    ):
        steps.append(_write_env_file(env_path, runtime_env, force=args.force))

    if not args.skip_stdio and _ask(
        "Create .mcp.json for stdio MCP clients?", True, assume_yes=args.yes
    ):
        steps.append(
            _write_from_template(
                ROOT / ".mcp.json.example",
                ROOT / ".mcp.json",
                force=args.force,
                replacements={"/path/to/hpe-networking-mcp": str(ROOT)},
            )
        )
        steps.append(_merge_json_env(ROOT / ".mcp.json", "hpe-networking-mcp", runtime_env))

    if not args.skip_http and _ask(
        "Create .mcp.http.json for streamable HTTP MCP clients?", True, assume_yes=args.yes
    ):
        endpoint = f"http://{args.host}:{args.port}/mcp"
        steps.append(
            _write_from_template(
                ROOT / ".mcp.http.json.example",
                ROOT / ".mcp.http.json",
                force=args.force,
                replacements={"http://127.0.0.1:8010/mcp": endpoint},
            )
        )

    if args.with_vscode or _ask("Create .vscode/mcp.json for VS Code?", False, assume_yes=args.yes):
        steps.append(
            _write_from_template(
                ROOT / ".vscode" / "mcp.json.example",
                ROOT / ".vscode" / "mcp.json",
                force=args.force,
            )
        )
        steps.append(
            _merge_json_env(
                ROOT / ".vscode" / "mcp.json",
                "hpe-networking-mcp",
                runtime_env,
            )
        )

    if not args.skip_catalog and _ask(
        "Build the router tool catalog now?", True, assume_yes=args.yes
    ):
        command = ["uv", "run", "python", "scripts/ingest_tools.py"]
        if selected_products:
            command.extend(["--products", ",".join(selected_products)])
        steps.append(_run(command, "tool catalog", env=_catalog_env(product_env) or None))

    if not args.skip_doctor and _ask("Run the local doctor now?", True, assume_yes=args.yes):
        steps.append(
            _run(
                ["uv", "run", "python", "scripts/doctor.py"],
                "doctor",
                env={
                    **runtime_env,
                    "MCP_HOST": args.host,
                    "MCP_PORT": str(args.port),
                },
            )
        )

    _print_steps(steps)
    print("\nNext steps")
    print("1. Review config/credentials.yaml and .env before starting API-backed tools.")
    print(
        "2. For HTTP MCP clients, run: "
        f"MCP_HOST={args.host} MCP_PORT={args.port} bash scripts/run_http_router.sh"
    )
    print(f"3. Router exposure mode: {args.router_mode}.")
    print(f"4. Aggregate access profile: {args.access_profile}.")
    if selected_products:
        print(f"5. Optional products enabled locally: {', '.join(selected_products)}.")
        print(f"6. Optional product access mode: {product_access}.")
    else:
        print(
            "5. Optional products stayed disabled; enable them later with "
            "--products or --with-products."
        )

    return 0 if all(step.status != "WARN" for step in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
