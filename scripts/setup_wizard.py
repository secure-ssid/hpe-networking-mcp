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

# Mirrors _BRIDGE_RE in docker/entrypoint.sh: the container fills <VAR> from a
# <VAR>_FILE hint only for these families and logs a refusal for anything
# else. A secret file outside this set yields a stack that looks configured
# and silently is not, so the wizard refuses to emit one. The shell literal
# and this pattern are pinned equal in tests/unit/test_setup_wizard_docker.py.
_BRIDGE_FAMILY_RE = re.compile(
    r"^(MCP_HTTP_BEARER_TOKEN|[A-Z0-9_]+_(API_TOKEN|CLIENT_SECRET|PASSWORD"
    r"|SESSION_COOKIE|CSRF_TOKEN))$"
)


@dataclass(frozen=True)
class SecretFile:
    """One secret value: one 0600 file, one compose secret, one <VAR>_FILE.

    Rotation blast radius is the point. A credential lives in exactly one
    file, so revoking it rewrites that file and restarts the router without
    reading, rewriting, or re-exposing any other secret.

    ``path`` resolves SECRETS_DIR on access instead of binding it at
    construction, so tests that repoint the module constant see the move.
    """

    env_var: str
    name: str
    label: str

    @property
    def path(self) -> Path:
        return SECRETS_DIR / self.name

    @property
    def file_env_var(self) -> str:
        return f"{self.env_var}_FILE"

    @property
    def container_path(self) -> str:
        return f"/run/secrets/{self.name}"


BEARER_SECRET = SecretFile(
    "MCP_HTTP_BEARER_TOKEN", "mcp_http_bearer_token", "router HTTP bearer token"
)
# Central and GreenLake secrets live outside credentials.yaml so each rotates
# alone. load_credentials ranks process env above the YAML and reads exactly
# these names, so the entrypoint bridge is the whole mechanism -- no loader
# change, and the YAML keeps carrying identity (base URLs, client ids).
CENTRAL_SECRET = SecretFile(
    "SOURCE_CLIENT_SECRET", "central_client_secret", "Aruba Central API client secret"
)
GLP_SECRET = SecretFile(
    "TARGET_CLIENT_SECRET", "glp_client_secret", "HPE GreenLake API client secret"
)

BEARER_TOKEN_PATH = BEARER_SECRET.path
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
            "APSTRA_USERNAME": "YOUR_APSTRA_USERNAME",
            "APSTRA_PASSWORD": "YOUR_APSTRA_PASSWORD",
            "APSTRA_API_TOKEN": "YOUR_APSTRA_API_TOKEN",
        },
    },
    "aos8": {
        "label": "ArubaOS 8",
        "vars": {
            "AOS8_BASE_URL": "https://mobility-conductor.example.com",
            "AOS8_USERNAME": "YOUR_AOS8_USERNAME",
            "AOS8_PASSWORD": "YOUR_AOS8_PASSWORD",
            "AOS8_API_TOKEN": "YOUR_AOS8_API_TOKEN",
        },
    },
    "edgeconnect": {
        "label": "EdgeConnect",
        "vars": {
            "EDGECONNECT_BASE_URL": "https://orchestrator.example.com",
            "EDGECONNECT_API_TOKEN": "YOUR_EDGECONNECT_API_TOKEN",
            "EDGECONNECT_AUTH_HEADER": "X-Auth-Token",
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
# Mirror of tool_router._VALID_TOOLSETS (keys of _TOOLSET_BACKENDS plus
# "all"). This script is stdlib-only and must import without src/ on the
# path, so the list is duplicated rather than imported; a unit test pins the
# three copies (here, tool_router, cli/doctor) equal.
VALID_TOOLSETS = (
    "aos8",
    "all",
    "apstra",
    "axis",
    "central",
    "central-generated",
    "clearpass",
    "config",
    "design",
    "edgeconnect",
    "glp",
    "interop",
    "mist",
    "monitoring",
    "nac",
    "ops",
    "rag",
    "site-health",
    "uxi",
)
DEFAULT_TOOLSETS = "central,glp,rag"
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
# Which toolset/product names load the backend each write gate guards. Used
# to ask about writes only for platforms the deployment actually runs, and to
# pin every other gate to "0" so the emitted `.env` is never partial.
PLATFORM_WRITE_SOURCES = {
    "HPE_MCP_CENTRAL_WRITES": (
        "Aruba Central",
        ("central", "central-generated", "config", "monitoring", "nac", "ops", "site-health"),
    ),
    "HPE_MCP_GLP_V2BETA1_WRITES": ("HPE GreenLake", ("glp",)),
    "HPE_MCP_AOS8_WRITES": ("ArubaOS 8", ("aos8",)),
    "HPE_MCP_EDGECONNECT_WRITES": ("EdgeConnect", ("edgeconnect",)),
    "HPE_MCP_APSTRA_WRITES": ("Apstra", ("apstra",)),
    "HPE_MCP_MIST_WRITES": ("Juniper Mist", ("mist",)),
    "HPE_MCP_CLEARPASS_WRITES": ("ClearPass", ("clearpass",)),
    "HPE_MCP_UXI_WRITES": ("HPE Aruba UXI", ("uxi",)),
    "HPE_MCP_AXIS_WRITES": ("Axis Atmos Cloud", ("axis",)),
}
PROFILE_WRITE_ENV_VARS = ("HPE_MCP_READONLY", *PLATFORM_WRITE_ENV_VARS)

# W3 docker-mode `.env` emission: only these keys may be written, and the
# defaults stay read-only/custom (C8).
DOCKER_ENV_ALLOWLIST = frozenset(
    {
        "HPE_MCP_ROUTER_MODE",
        "HPE_MCP_TOOLSETS",
        "HPE_MCP_ACCESS_PROFILE",
        "HPE_MCP_PRODUCT_ACCESS",
        "HPE_MCP_PRODUCTS",
        *PLATFORM_WRITE_ENV_VARS,
        "HPE_MCP_RAG_BACKEND",
    }
)

# R7 audit set: host-side credential resolution ranks `.env` above the YAML
# credentials -- load_credentials in src/hpe_networking_mcp/pipeline/config.py
# reads process env > .env > YAML, and CREDS_PATH redirects where
# credentials.yaml is loaded from. Any of these keys in a pre-existing `.env`
# silently changes what the host connects as, so the wizard warns listing them
# and never modifies them.
CREDENTIAL_AFFECTING_ENV_VARS = (
    "CREDS_PATH",
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
    # Not caught by SECRET_ENV_SUFFIXES, but mist.py reads it as a live
    # session credential, so it belongs in the do-not-touch audit set.
    "MIST_SESSION_COOKIE",
)


@dataclass
class Step:
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class DockerManifest:
    """Cross-slice contract K1: everything the docker emitters consume.

    host_ip/client_hostname/rag shape the overlay; backend carries the
    vector-backend choice ("redis"/"lancedb") when rag is set, else None.
    toolsets/products/access_profile/platform_gates are the routing and write
    decisions that must reach the container. secret_files lists every
    credential materialised as its own 0600 file beside the always-present
    bearer token; plain_env carries the non-secret product identity values
    (base URLs, client ids) the overlay writes literally.

    Mapping fields are stored as sorted pairs so the dataclass stays frozen
    and hashable, and so emitted artifacts are byte-stable across runs.
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
    toolsets: str = DEFAULT_TOOLSETS
    secret_files: tuple[SecretFile, ...] = ()
    plain_env: tuple[tuple[str, str], ...] = ()
    platform_gates: tuple[tuple[str, str], ...] = ()


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


def _refuse_unbridgeable_secret(secret: SecretFile) -> None:
    """Never emit a secret file the container is going to ignore."""
    if _BRIDGE_FAMILY_RE.fullmatch(secret.env_var):
        return
    raise SystemExit(
        f"refusing to emit secrets/{secret.name}: {secret.env_var} is not a "
        "secret family docker/entrypoint.sh bridges, so the container would "
        "log a refusal and start without it. Extend _BRIDGE_RE in "
        "docker/entrypoint.sh and _BRIDGE_FAMILY_RE here together."
    )


def _write_prompted_secret(secret: SecretFile, value: str, *, force: bool) -> Step:
    """Materialise one secret file, or keep an existing one byte-for-byte.

    An unreadable or empty existing file aborts instead of being overwritten:
    an empty secret silently disables whatever it guards, which is the exact
    failure docker/entrypoint.sh also refuses to start on.
    """
    _refuse_unbridgeable_secret(secret)
    target = secret.path
    label = _rel(target)
    if target.exists() and not force:
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(
                f"{label} exists but could not be read as UTF-8 text ({exc}); "
                "refusing to overwrite a secret the wizard cannot verify. Fix "
                "or remove the file, or rerun with --force to replace it."
            ) from exc
        if not existing.strip():
            raise SystemExit(
                f"{label} exists but is empty; an empty value silently disables "
                f"whatever {secret.env_var} guards. Remove the file, or rerun "
                "with --force to write a new value."
            )
        os.chmod(target, 0o600)
        return Step(label, "OK", "kept existing secret (mode 0600)")
    cleaned = value.strip()
    if not cleaned:
        raise SystemExit(
            f"refusing to write an empty {label}: an empty value silently "
            f"disables whatever {secret.env_var} guards."
        )
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_file(target, cleaned + "\n")
    detail = "rotated secret (mode 0600)" if existed else "created secret (mode 0600)"
    return Step(label, "OK", detail)


def _product_secret_files(product: str) -> dict[str, SecretFile]:
    """Secret-shaped vars of one product, keyed by env var name.

    Derived from PRODUCT_ENV rather than hand-listed, so a product gaining a
    credential gains its secret file with no second edit.
    """
    label = PRODUCT_ENV[product]["label"]
    return {
        name: SecretFile(name, name.lower(), label)
        for name in PRODUCT_ENV[product]["vars"]
        if _is_secret_env_var(name)
    }


def _read_answer(prompt: str, *, secret: bool = False) -> str:
    """Read one prompted answer, turning EOF/interrupt into a clean exit.

    ``input()`` raises EOFError the moment stdin closes -- piped input that
    ran out, Ctrl-D, or a non-interactive shell. The bare traceback reads
    like a crash inside the wizard when it is only end of input, and it
    buries the one thing the operator needs to know: which flag skips the
    prompts. Ctrl-C gets the same treatment for the same reason.

    Deliberately does not claim "nothing was written": prompts run either
    side of the first write, so the summary already printed above is the
    honest record of what exists.
    """
    reader = getpass.getpass if secret else input
    try:
        return reader(prompt)
    except EOFError:
        raise SystemExit(
            f"\nsetup wizard: stdin closed while waiting for '{prompt.strip()}'. "
            "Re-run attached to a terminal, or pass --yes to accept every "
            "default without prompting."
        ) from None
    except KeyboardInterrupt:
        raise SystemExit("\nsetup wizard: cancelled at a prompt.") from None


def _ask(prompt: str, default: bool, *, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    suffix = "Y/n" if default else "y/N"
    answer = _read_answer(f"{prompt} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = _read_answer(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_secret(prompt: str, default: str) -> str:
    answer = _read_answer(
        f"{prompt} [leave blank to keep placeholder]: ", secret=True
    ).strip()
    return answer or default


def _is_secret_env_var(name: str) -> bool:
    return name.endswith(SECRET_ENV_SUFFIXES)


def _csv(values: str) -> list[str]:
    return [item.strip().lower() for item in values.split(",") if item.strip()]


def _selected_products(
    args: argparse.Namespace, *, assume_defaults: bool = False
) -> list[str]:
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
    if args.yes or assume_defaults:
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


def _write_credentials(
    target: Path, *, force: bool, assume_yes: bool, split_secrets: bool = False
) -> tuple[Step, dict[str, str]]:
    """Write the credentials YAML; optionally hold the two secrets back.

    With ``split_secrets`` the emitted YAML carries identity only (base URLs,
    client ids, workspace ids) and the captured client secrets are returned
    keyed by the env var that carries them, for the caller to write as their
    own files. load_credentials ranks process env above the YAML and reads
    exactly those names, so an identity-only file loses nothing.
    """
    if target.exists() and not force:
        if assume_yes or not _has_placeholders(target):
            return (
                Step(_rel(target), "SKIP", "already exists; use --force to overwrite"),
                {},
            )
        if not _ask(
            "Existing config/credentials.yaml contains placeholders; update it now?",
            True,
            assume_yes=False,
        ):
            return (
                Step(_rel(target), "SKIP", "left existing placeholder file unchanged"),
                {},
            )

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

    secret_lines = {
        "central": f"  client_secret: {_yaml_string(values['central_client_secret'])}",
        "glp": f"  client_secret: {_yaml_string(values['target_client_secret'])}",
    }
    if split_secrets:
        header = [
            "# Generated by scripts/setup_wizard.py --docker.",
            "# Identity only: each client_secret lives in its own 0600 file",
            f"# ({CENTRAL_SECRET.name}, {GLP_SECRET.name}) so that rotating one",
            "# credential never touches another. See secrets/README.md.",
        ]
        secret_lines = {"central": None, "glp": None}
    else:
        header = [
            "# Generated by scripts/setup_wizard.py.",
            "# credentials.yaml is gitignored - never commit real credentials.",
        ]

    body = [
        *header,
        "# Common Central API gateways:",
        *[f"#   {name}: {url}" for name, url in CENTRAL_BASE_URLS],
        "",
        "central_account:",
        f"  base_url: {_yaml_string(central_url)}",
        f"  client_id: {_yaml_string(values['central_client_id'])}",
        secret_lines["central"],
        f"  glp_workspace_id: {_yaml_string(values['central_workspace'])}",
        "",
        "glp_account:",
        f"  base_url: {_yaml_string(target_url)}",
        f"  client_id: {_yaml_string(values['target_client_id'])}",
        secret_lines["glp"],
        f"  glp_workspace_id: {_yaml_string(values['target_workspace'])}",
        "",
        "glp:",
        '  token_url: "https://sso.common.cloud.hpe.com/as/token.oauth2"',
        '  base_url: "https://global.api.greenlake.hpe.com"',
        "",
    ]

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_file(target, "\n".join(line for line in body if line is not None))
    if split_secrets:
        detail = "created with region choices; secrets written as separate files"
        captured = {
            CENTRAL_SECRET.env_var: values["central_client_secret"],
            GLP_SECRET.env_var: values["target_client_secret"],
        }
        return Step(_rel(target), "OK", detail), captured
    return (
        Step(_rel(target), "OK", "created with region choices and placeholders/secrets"),
        {},
    )


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


_FILE_ENV_REF_RE = re.compile(r"\b([A-Z][A-Z0-9_]*_FILE)\b")


def _plain_twin_violations(text: str) -> list[str]:
    """C3: list plain ``<VAR>=`` stems that shadow a referenced ``<VAR>_FILE``."""
    violations: list[str] = []
    for referenced in sorted(set(_FILE_ENV_REF_RE.findall(text))):
        stem = referenced[: -len("_FILE")]
        if re.search(rf"(?m)^\s*(?:export\s+)?{stem}=", text):
            violations.append(stem)
    return violations


def _refuse_plain_file_twins(text: str, target: Path) -> None:
    """Refuse to emit text pairing a secret *_FILE ref with its plain twin."""
    twins = _plain_twin_violations(text)
    if twins:
        raise SystemExit(
            f"refusing to emit {_rel(target)}: plain twin assignment(s) for "
            f"referenced *_FILE variable(s): {', '.join(twins)}"
        )


def _write_env_file(
    target: Path,
    env: dict[str, str],
    *,
    force: bool,
    update_keys: frozenset[str] | set[str] | None = None,
) -> Step:
    """Write/merge runtime env knobs; ``update_keys`` restricts the merge.

    Docker mode passes DOCKER_ENV_ALLOWLIST so only allowlisted keys are
    rewritten or appended -- every other existing line (including secrets)
    survives byte-identically.
    """
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
        update_keys = (
            {
                "HPE_MCP_ACCESS_PROFILE",
                "HPE_MCP_PRODUCTS",
                "HPE_MCP_PRODUCT_ACCESS",
                *PROFILE_WRITE_ENV_VARS,
            }
            if update_keys is None
            else set(update_keys)
        )
        updated_lines = []
        for line in lines:
            key = _env_assignment_key(line)
            if (
                clear_aggregate_gates
                and key in PROFILE_WRITE_ENV_VARS
                and key in update_keys
            ):
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
        final_text = "\n".join([*updated_lines, ""])
        _refuse_plain_file_twins(final_text, target)
        _write_secret_file(target, final_text)
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
    final_text = "\n".join(lines)
    _refuse_plain_file_twins(final_text, target)
    _write_secret_file(target, final_text)
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


def _choose_quick_start(*, assume_yes: bool) -> bool:
    """First docker question: take the recommended defaults wholesale?

    Accepting skips the toolset, product, access and RAG prompts -- credential
    capture still runs, because a router with no credentials is not a
    deployment. Explicit flags always win over the defaults this implies.
    """
    if assume_yes:
        return True
    print("\nDeployment shape")
    print("  Recommended: loopback-only, Central + GreenLake + API lookup,")
    print("  read-only, no optional products, no RAG image.")
    return _ask("Use the recommended defaults?", True, assume_yes=False)


def _validate_toolsets(raw: str) -> str:
    requested = _csv(raw)
    unknown = sorted(set(requested) - set(VALID_TOOLSETS))
    if unknown:
        raise SystemExit(
            f"Unknown toolset(s): {', '.join(unknown)}. Accepted values: "
            + ", ".join(sorted(VALID_TOOLSETS))
        )
    return ",".join(requested)


def _choose_toolsets(*, assume_yes: bool) -> str:
    """Which backend families the router loads (HPE_MCP_TOOLSETS)."""
    if assume_yes:
        return DEFAULT_TOOLSETS
    print("\nToolsets")
    print("  " + ", ".join(sorted(VALID_TOOLSETS)))
    print(
        "  Optional products selected below are unioned onto this set, so "
        "they need no entry here."
    )
    return _validate_toolsets(
        _ask_text("Toolsets to load, comma-separated", DEFAULT_TOOLSETS)
    )


def _choose_access_profile(args: argparse.Namespace, *, assume_yes: bool) -> str:
    """Aggregate write posture. An explicit --access-profile skips the prompt."""
    if assume_yes or args.access_profile != "custom":
        return args.access_profile
    print("\nWrite access")
    print("  safe-read-only  every write and destructive tool refused")
    print("  custom          per-platform choice, asked next (default)")
    print("  full-read-write every platform's writes enabled")
    answer = _ask_text("Access profile", "custom").strip().lower()
    if answer not in {"safe-read-only", "custom", "full-read-write"}:
        raise SystemExit(
            "--access-profile must be one of: safe-read-only, custom, "
            "full-read-write"
        )
    return answer


def _choose_platform_gates(
    profile: str, toolsets: str, products: list[str], *, assume_yes: bool
) -> dict[str, str]:
    """Per-platform write gates for the custom profile.

    Only platforms actually loaded by the chosen toolsets/products are asked
    about; everything else is pinned to "0" so the emitted `.env` is complete
    and deterministic rather than silently partial.
    """
    selected = set(_csv(toolsets)) | set(products)
    everything = "all" in selected
    gates: dict[str, str] = {}
    asked_header = False
    for gate, (label, sources) in PLATFORM_WRITE_SOURCES.items():
        loaded = everything or bool(selected & set(sources))
        if not loaded or profile != "custom" or assume_yes:
            gates[gate] = "0"
            continue
        if not asked_header:
            print("\nPer-platform writes (each defaults to refused)")
            asked_header = True
        gates[gate] = (
            "1"
            if _ask(
                f"  Allow write and destructive tools for {label}?",
                False,
                assume_yes=False,
            )
            else "0"
        )
    return gates


def _docker_product_inputs(
    products: list[str], *, assume_yes: bool
) -> tuple[list[SecretFile], dict[str, str], dict[str, str]]:
    """Prompt every selected product's settings in one pass.

    Returns the secret files to write, their captured values keyed by env var,
    and the non-secret identity values (base URLs, client ids, header names)
    the overlay carries literally.
    """
    secret_files: list[SecretFile] = []
    secret_values: dict[str, str] = {}
    plain: dict[str, str] = {}
    for product in products:
        meta = PRODUCT_ENV[product]
        secrets_for_product = _product_secret_files(product)
        if meta["vars"] and not assume_yes:
            print(f"\n{meta['label']} settings")
        for name, default in meta["vars"].items():
            secret = secrets_for_product.get(name)
            if secret is None:
                plain[name] = default if assume_yes else _ask_text(name, default)
                continue
            _refuse_unbridgeable_secret(secret)
            secret_files.append(secret)
            # A blank answer keeps the placeholder: the file is never empty
            # (an empty bridged value silently disables the credential) and
            # _has_placeholders still flags it for non-loopback deployments.
            secret_values[name] = default if assume_yes else _ask_secret(name, default)
    return secret_files, secret_values, plain


def _choose_rag_image(*, assume_yes: bool) -> bool:
    """W2 prompt: RAG-capable image choice (opt-in; default stays non-RAG)."""
    return _ask(
        "Use the RAG-enabled image hpe-networking-mcp-router:rag "
        "(built for you by `docker compose ... up --build`)?",
        False,
        assume_yes=assume_yes,
    )


def _choose_rag_backend(*, assume_yes: bool) -> str:
    """W3 prompt: vector backend behind the RAG toolset (default lancedb)."""
    use_redis = _ask(
        "Store the RAG corpus in the Redis Stack service "
        "(HPE_MCP_RAG_BACKEND=redis; otherwise the in-image LanceDB backend)?",
        False,
        assume_yes=assume_yes,
    )
    return "redis" if use_redis else "lancedb"


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
        # The redis backend embeds through the ollama service rather than
        # fastembed, so it needs the redis client extra alone; `ingestion` is
        # the embedded LanceDB stack. INSTALL_EXTRAS is space-separated.
        extras = "redis" if manifest.backend == "redis" else "ingestion"
        image_block = (
            "    build:\n"
            "      context: .\n"
            "      dockerfile: Dockerfile\n"
            "      args:\n"
            f'        INSTALL_EXTRAS: "{extras}"\n'
            "    image: hpe-networking-mcp-router:rag\n"
        )
        # The LanceDB corpus is bind-mounted from the host; the redis corpus
        # lives in the redis service, so those mounts would be dead weight.
        rag_mounts = (
            ""
            if manifest.backend == "redis"
            else (
                "      - ./data/docs.lance:/app/data/docs.lance:ro\n"
                "      - ./data/tools.lance:/app/data/tools.lance:ro\n"
            )
        )
    else:
        image_block = (
            "    build:\n"
            "      context: .\n"
            "      dockerfile: Dockerfile\n"
            "    image: hpe-networking-mcp-router:local\n"
        )
        rag_mounts = ""

    secret_files = sorted(manifest.secret_files, key=lambda item: item.name)
    lines = [
        "# Generated by scripts/setup_wizard.py --docker; regenerate with --force.",
        "# Layer it over the tracked bundle:",
        "#   docker compose -f docker-compose.yml \\",
        "#     -f docker-compose.router.local.yml --profile router up -d mcp-router",
        "# Each credential is its own 0600 file under secrets/: rotate one by",
        "# rewriting that file and restarting mcp-router; no other secret moves.",
        "services:",
        "  mcp-router:",
    ]
    lines.extend(image_block.rstrip("\n").split("\n"))
    lines.extend(["    profiles:", "      - router"])
    if manifest.backend == "redis":
        # `--profile router up mcp-router` starts no other service on its own,
        # so without this the backend would point at containers the command
        # never launched: redis holds the vectors, ollama embeds the query.
        lines.extend(["    depends_on:", "      - redis", "      - ollama"])
    lines.extend(
        [
            "    ports:",
            f'      - "{published}"',
            "    environment:",
            "      MCP_TRANSPORT: streamable-http",
            '      MCP_HOST: "0.0.0.0"',
            f'      MCP_PORT: "{port}"',
            f'      MCP_ALLOWED_HOSTS: "{allowed_hosts}"',
            f'      MCP_ALLOWED_ORIGINS: "{allowed_origins}"',
            '      HPE_MCP_ROUTER_MODE: "${HPE_MCP_ROUTER_MODE:-minimal}"',
            f'      HPE_MCP_TOOLSETS: "${{HPE_MCP_TOOLSETS:-{manifest.toolsets}}}"',
            '      HPE_MCP_ACCESS_PROFILE: "${HPE_MCP_ACCESS_PROFILE:-custom}"',
            '      HPE_MCP_RAG_BACKEND: "${HPE_MCP_RAG_BACKEND:-}"',
            "      HPE_MCP_PRODUCTS: "
            f'"${{HPE_MCP_PRODUCTS:-{",".join(manifest.products)}}}"',
            '      HPE_MCP_PRODUCT_ACCESS: "${HPE_MCP_PRODUCT_ACCESS:-read-only}"',
        ]
    )
    # Deny-by-default in the overlay itself, so a deleted or hand-trimmed .env
    # cannot silently promote a read-only deployment to read-write.
    lines.extend(
        f'      {gate}: "${{{gate}:-0}}"' for gate in PLATFORM_WRITE_ENV_VARS
    )
    if manifest.backend == "redis":
        lines.append('      REDIS_URL: "redis://redis:6379"')
        # OllamaClient defaults to localhost, which inside this container is
        # the router itself.
        lines.append('      OLLAMA_URL: "http://ollama:11434"')
    lines.extend(
        f'      {name}: "{value}"' for name, value in sorted(manifest.plain_env)
    )
    lines.append("      CREDS_PATH: /run/secrets/credentials_yaml")
    lines.append(
        f"      {BEARER_SECRET.file_env_var}: {BEARER_SECRET.container_path}"
    )
    lines.extend(
        f"      {secret.file_env_var}: {secret.container_path}"
        for secret in secret_files
    )
    lines.extend(
        [
            "    secrets:",
            "      - credentials_yaml",
            f"      - {BEARER_SECRET.name}",
        ]
    )
    lines.extend(f"      - {secret.name}" for secret in secret_files)
    lines.extend(
        [
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
            f"  {BEARER_SECRET.name}:",
            f"    file: ./secrets/{BEARER_SECRET.name}",
        ]
    )
    for secret in secret_files:
        lines.append(f"  {secret.name}:")
        lines.append(f"    file: ./secrets/{secret.name}")
    lines.extend(
        [
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
    _refuse_plain_file_twins(text, OVERLAY_PATH)  # C3 guard on the shipped bytes
    if OVERLAY_PATH.exists() and not force:
        return Step(
            _rel(OVERLAY_PATH),
            "OK",
            "kept existing overlay (rerun with --force to regenerate)",
        )
    with open(OVERLAY_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return Step(_rel(OVERLAY_PATH), "OK", "created compose overlay")


def _docker_env_values(
    args: argparse.Namespace, manifest: DockerManifest, *, product_access: str
) -> dict[str, str]:
    """Allowlisted `.env` knobs derived from the run (C8: read-only/custom defaults)."""
    values = {
        "HPE_MCP_ROUTER_MODE": args.router_mode,
        "HPE_MCP_ACCESS_PROFILE": manifest.access_profile,
        "HPE_MCP_TOOLSETS": manifest.toolsets,
        "HPE_MCP_PRODUCTS": ",".join(manifest.products),
    }
    if manifest.access_profile == "safe-read-only":
        values["HPE_MCP_PRODUCT_ACCESS"] = "read-only"
        values.update({name: "0" for name in PLATFORM_WRITE_ENV_VARS})
    elif manifest.access_profile == "full-read-write":
        values["HPE_MCP_PRODUCT_ACCESS"] = "read-write"
        values.update({name: "1" for name in PLATFORM_WRITE_ENV_VARS})
    else:
        values["HPE_MCP_PRODUCT_ACCESS"] = product_access
        gates = dict(manifest.platform_gates)
        values.update({name: gates.get(name, "0") for name in PLATFORM_WRITE_ENV_VARS})
    if manifest.backend == "redis":
        values["HPE_MCP_RAG_BACKEND"] = "redis"
    return values


def _flagged_env_keys(lines: list[str]) -> list[str]:
    """R7: keys in an existing `.env` that shape host-side credentials."""
    flagged = {
        key
        for line in lines
        if (key := _env_assignment_key(line))
        and (_is_secret_env_var(key) or key in CREDENTIAL_AFFECTING_ENV_VARS)
    }
    return sorted(flagged)


def _docker_env_steps(
    args: argparse.Namespace,
    manifest: DockerManifest,
    *,
    product_access: str,
    force: bool,
) -> list[Step]:
    """W3 `.env` emission + R7 audit; runs after the K3 gate, overlay stays LAST.

    The audit lists secret-shaped / credential-affecting keys found in a
    pre-existing file -- host-side credential resolution ranks `.env` above the
    YAML credentials (pipeline/config.py load_credentials) -- and never
    modifies them. ``--force`` refuses to overwrite a file holding such keys
    instead of silently dropping them.
    """
    values = _docker_env_values(args, manifest, product_access=product_access)
    outside = sorted(set(values) - DOCKER_ENV_ALLOWLIST)
    if outside:
        raise SystemExit(
            f"refusing to emit {_rel(ENV_PATH)}: key(s) outside the docker-mode "
            f"allowlist: {', '.join(outside)}"
        )
    steps: list[Step] = []
    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = []  # undecodable content: the writer reports it as a WARN
        flagged = _flagged_env_keys(lines)
        if flagged:
            steps.append(
                Step(
                    _rel(ENV_PATH),
                    "WARN",
                    "credential-affecting keys left untouched: " + ", ".join(flagged),
                )
            )
            if force:
                raise SystemExit(
                    f"{_rel(ENV_PATH)} holds secret-shaped or credential-affecting "
                    f"key(s): {', '.join(flagged)}; refusing to overwrite it with "
                    "--force. Edit the file by hand instead."
                )
    steps.append(
        _write_env_file(
            ENV_PATH, values, force=force, update_keys=set(DOCKER_ENV_ALLOWLIST)
        )
    )
    return steps


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
    missing = [
        _rel(secret.path) for secret in manifest.secret_files if not secret.path.exists()
    ]
    if missing:
        raise SystemExit(
            "refusing to finish a non-loopback deployment: secret file(s) "
            f"{', '.join(missing)} were never written. Rerun the wizard "
            "without --host/--expose to stay loopback-only."
        )
    unfilled = [
        _rel(secret.path)
        for secret in manifest.secret_files
        if _has_placeholders(secret.path)
    ]
    if unfilled:
        raise SystemExit(
            "refusing to finish a non-loopback deployment: secret file(s) "
            f"{', '.join(unfilled)} still hold placeholder values. Write the "
            "real credentials into them, or rerun without --host/--expose to "
            "stay loopback-only."
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
    print(f"{next_step}. Toolsets loaded: {manifest.toolsets}.")
    next_step += 1
    if manifest.products:
        print(
            f"{next_step}. Optional products enabled: "
            f"{', '.join(manifest.products)} ({product_access})."
        )
        next_step += 1
    if manifest.secret_files:
        print(f"{next_step}. Credential files (each 0600, one value per file):")
        for secret in sorted(manifest.secret_files, key=lambda item: item.name):
            print(f"     {_rel(secret.path)}  ->  {secret.env_var} ({secret.label})")
        print(
            "     Rotate one credential by writing the new value into its file "
            "and running"
        )
        print(
            "     docker compose -f docker-compose.yml -f "
            "docker-compose.router.local.yml restart mcp-router;"
        )
        print("     no other secret is read, rewritten, or re-exposed.")
        next_step += 1
    start_services = (
        "mcp-router redis ollama" if manifest.backend == "redis" else "mcp-router"
    )
    print(
        f"{next_step}. Compose overlay written to {_rel(OVERLAY_PATH)}: start it "
        "with docker compose -f docker-compose.yml -f "
        f"docker-compose.router.local.yml --profile router up -d {start_services} "
        "(docs/production-deployment.md carries the same path in prose)."
    )
    print(
        f"{next_step + 1}. Non-secret tuning knobs live in {_rel(ENV_PATH)}; "
        "edit them and re-run the `up -d` command above to apply. Compose "
        "bakes these values in when the container is created, so a plain "
        "`restart` keeps the old ones."
    )


def _run_docker_mode(args: argparse.Namespace) -> DockerManifest:
    """--docker flow: resolve exposure, prompt W2/W3 choices, emit secrets +
    .env knobs + compose overlay.

    All interactive prompts and refusals precede the first write; .env and
    overlay emission MUST hook in after _finalize_docker_deployment (K3
    write-order invariant keeps the overlay LAST).
    """
    # quick_start and --yes both mean "do not ask, take the default"; every
    # prompt helper already implements exactly that under assume_yes, so the
    # two collapse into one mechanism rather than a second short-circuit.
    quick_start = _choose_quick_start(assume_yes=args.yes)
    defaults = args.yes or quick_start
    host_ip = _resolve_docker_exposure(args)
    toolsets = (
        _validate_toolsets(args.toolsets)
        if args.toolsets
        else _choose_toolsets(assume_yes=defaults)
    )
    selected_products = _selected_products(args, assume_defaults=quick_start)
    secret_files, secret_values, plain_env = _docker_product_inputs(
        selected_products, assume_yes=args.yes
    )
    access_profile = _choose_access_profile(args, assume_yes=defaults)
    args.access_profile = access_profile
    product_access = _product_access(args, selected_products)
    platform_gates = _choose_platform_gates(
        access_profile, toolsets, selected_products, assume_yes=defaults
    )
    client_hostname = _resolve_client_hostname(host_ip, assume_yes=args.yes)
    rag_image = _choose_rag_image(assume_yes=defaults)
    rag_backend = _choose_rag_backend(assume_yes=args.yes) if rag_image else None

    steps: list[Step] = [_docker_token_step(args)]
    all_secrets = list(secret_files)
    if not args.skip_credentials:
        creds_step, captured = _write_credentials(
            DOCKER_CREDENTIALS_PATH,
            force=args.force,
            assume_yes=args.yes,
            split_secrets=True,
        )
        steps.append(creds_step)
        for secret in (CENTRAL_SECRET, GLP_SECRET):
            if secret.env_var in captured:
                all_secrets.append(secret)
                secret_values[secret.env_var] = captured[secret.env_var]
            elif secret.path.exists():
                # Kept-credentials rerun: the file is already there, so keep
                # wiring it into the overlay without touching its bytes.
                all_secrets.append(secret)
    for secret in all_secrets:
        if secret.env_var in secret_values:
            steps.append(
                _write_prompted_secret(
                    secret, secret_values[secret.env_var], force=args.force
                )
            )

    manifest = DockerManifest(
        port=args.port,
        host_ip=host_ip,
        client_hostname=client_hostname,
        rag=rag_image,
        backend=rag_backend,
        products=selected_products,
        access_profile=access_profile,
        token_path=BEARER_TOKEN_PATH,
        creds_path=DOCKER_CREDENTIALS_PATH,
        toolsets=toolsets,
        secret_files=tuple(all_secrets),
        plain_env=tuple(sorted(plain_env.items())),
        platform_gates=tuple(sorted(platform_gates.items())),
    )
    _finalize_docker_deployment(manifest)
    steps.extend(
        _docker_env_steps(args, manifest, product_access=product_access, force=args.force)
    )
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
        "--toolsets",
        default="",
        help=(
            "comma-separated toolsets the router loads (default: "
            f"{DEFAULT_TOOLSETS}); accepted values: "
            + ", ".join(sorted(VALID_TOOLSETS))
        ),
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
        creds_step, _ = _write_credentials(
            ROOT / "config" / "credentials.yaml",
            force=args.force,
            assume_yes=args.yes,
        )
        steps.append(creds_step)

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
