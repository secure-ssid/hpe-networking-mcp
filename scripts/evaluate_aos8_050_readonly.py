"""Reproduce the AOS8 0.5.0 read-only evaluation.

Default mode is fully offline and fixture-backed: it uses the same FakeBackend /
`candidate()` / `security()` / `new_adapter()` / `classic_adapter()` contract as
`tests/unit/test_aos8_target_adapters.py`, without importing that test module,
and performs no network calls.

Optional `--live-new-central-readonly` swaps only the New Central surface to a
live, GET-only preview path. That mode never touches AOS8 or Classic Central
network surfaces, never attempts a write, never passes `confirmation=True`, and
only calls `hpe_networking_mcp.mcp_servers.monitoring.get_global_scope_id`,
`hpe_networking_mcp.mcp_servers.monitoring.list_scopes`, and
`hpe_networking_mcp.mcp_servers.aos8.aos8_preview_migration_run`. A GET-only guard is installed on
`hpe_networking_mcp.mcp_servers.shared.get_client()._request` before any live call so a non-GET
verb raises before transmission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from hpe_networking_mcp.mcp_servers import aos8 as aos8_tools
from hpe_networking_mcp.mcp_servers import monitoring as monitoring_tools
from hpe_networking_mcp.mcp_servers import shared as shared_tools
from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    ClassicCentralAdapter,
    ConflictPolicy,
    NewCentralAdapter,
    TargetContext,
    TargetType,
)
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager
from hpe_networking_mcp.pipeline.config import build_account_contexts

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state" / "aos8_migrations"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PLACEHOLDER_SECRET = "__runtime_secret_placeholder__"
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)

# Synthetic placeholders used only in fixture-backed previews. Reports never
# echo these raw literals; secret-scan results reference only their hashes.
OFFLINE_SECRET_LITERALS = {
    "wlan-passphrase": "placeholder-wpa-passphrase-050",
    "radius-shared-secret": "placeholder-radius-secret-050",
    "wpa3-personal": "placeholder-classic-wpa3-secret-050",
}


@dataclass(frozen=True)
class CandidateCase:
    surface: str
    family: str
    variant: str
    persona: str
    candidate: dict[str, Any]
    preview_candidates: tuple[dict[str, Any], ...] = ()
    selected: frozenset[str] | None = None
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    external_object_references: dict[str, dict[str, str]] = field(default_factory=dict)
    ap_group_target_map: dict[str, str] = field(default_factory=dict)
    ap_group_device_serials: dict[str, list[str]] = field(default_factory=dict)


# These helpers intentionally mirror the pure dict/fixture contract used by
# tests/unit/test_aos8_target_adapters.py. They are duplicated here (not
# imported from the test module) so this script stays runnable outside pytest.
def candidate(
    object_type: str,
    identifier: str,
    *,
    payload: dict[str, Any] | None = None,
    dependencies: list[str] | None = None,
    apply_order: int = 10,
    unsupported_fields: dict[str, Any] | None = None,
    requires_secret_input: bool = False,
    secret_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "identifier": identifier,
        "payload": payload or {},
        "dependencies": dependencies or [],
        "apply_order": apply_order,
        "unsupported_fields": unsupported_fields or {},
        "requires_secret_input": requires_secret_input,
        "secret_fields": secret_fields or [],
        "warnings": [],
    }


# Same as tests/unit/test_aos8_target_adapters.py helper.
def security(mode: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "mode": mode,
        "opmode": overrides.pop("opmode", mode),
        "ambiguous": False,
        "aaa_profile": None,
        "dot1x_auth_profile": None,
        "mac_auth_profile": None,
        "passphrase_present": False,
        "psk_hexkey_present": False,
        "wpa3_transition": False,
        "evidence": [],
    }
    base.update(overrides)
    return base


class FakeBackend:
    def __init__(self, reads: dict[str, Any] | None = None):
        self.reads = reads or {}
        self.read_calls: list[Any] = []
        self.write_calls: list[tuple[Any, bool]] = []

    def read(self, operation: Any) -> Any:
        self.read_calls.append(operation)
        value = self.reads.get(operation.name)
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, operation: Any, *, confirmation: bool) -> Any:
        self.write_calls.append((operation, confirmation))
        return {"ok": True, "name": operation.name}


# Same as tests/unit/test_aos8_target_adapters.py helper.
def resolve_scope(context: Any) -> tuple[str, str]:
    if context.scope_name == "bad":
        raise ValueError("unknown scope")
    return context.scope_id or "100", context.scope_name or "Branch"


# Same as tests/unit/test_aos8_target_adapters.py helper.
def validate_persona(context: Any) -> str:
    if context.persona not in {
        "CAMPUS_AP",
        "MICROBRANCH_AP",
        "MOBILITY_GW",
        "ACCESS_SWITCH",
    }:
        raise ValueError("invalid persona")
    return str(context.persona)


# Same as tests/unit/test_aos8_target_adapters.py helper.
def new_adapter(
    backend: FakeBackend,
    *,
    policy: ConflictPolicy = ConflictPolicy.FAIL,
    secrets: dict[str, dict[str, str]] | None = None,
    writes: bool = True,
    cluster: bool = False,
    persona: str = "CAMPUS_AP",
) -> NewCentralAdapter:
    return NewCentralAdapter(
        TargetContext(
            target_type=TargetType.NEW_CENTRAL,
            scope_id="100",
            scope_name="Branch",
            persona=persona,
            cluster_name="cluster-1" if cluster else None,
            cluster_scope_id="200" if cluster else None,
            conflict_policy=policy,
            secret_inputs=secrets or {},
        ),
        scope_resolver=resolve_scope,
        persona_validator=validate_persona,
        read_invoker=backend.read,
        write_invoker=backend.write,
        writes_enabled=lambda target: writes,
    )


# Same as tests/unit/test_aos8_target_adapters.py helper.
def classic_adapter(
    backend: FakeBackend,
    *,
    policy: ConflictPolicy = ConflictPolicy.FAIL,
    scope_name: str = "Branch Group",
    secrets: dict[str, dict[str, str]] | None = None,
    external_object_references: dict[str, dict[str, str]] | None = None,
    ap_group_target_map: dict[str, str] | None = None,
    ap_group_device_serials: dict[str, list[str]] | None = None,
    writes: bool = True,
) -> ClassicCentralAdapter:
    return ClassicCentralAdapter(
        TargetContext(
            target_type=TargetType.CLASSIC_CENTRAL,
            scope_id="classic-id",
            scope_name=scope_name,
            persona="CAMPUS_AP",
            conflict_policy=policy,
            secret_inputs=secrets or {},
            external_object_references=external_object_references or {},
            ap_group_target_map=ap_group_target_map or {},
            ap_group_device_serials=ap_group_device_serials or {},
        ),
        scope_resolver=resolve_scope,
        persona_validator=validate_persona,
        read_invoker=backend.read,
        write_invoker=backend.write,
        writes_enabled=lambda target: writes,
    )


def _candidate_key(candidate_payload: Mapping[str, Any]) -> str:
    return f"{candidate_payload['object_type']}:{candidate_payload['identifier']}"


def _wlan_candidate(
    name: str,
    mode: str,
    *,
    essid: str | None = None,
    vlan: int = 20,
    forward_mode: str = "bridge",
    opmode: str = "wpa2-aes",
    ambiguous: bool = False,
    requires_secret_input: bool = False,
    secret_fields: list[str] | None = None,
    **extra_security: Any,
) -> dict[str, Any]:
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": essid or name,
            "vlan": vlan,
            "aaa_profile": None,
            "security": security(
                mode,
                opmode=opmode,
                ambiguous=ambiguous,
                **extra_security,
            ),
        },
        unsupported_fields={
            "ssid_profile.opmode": opmode,
            "virtual_ap.forward_mode": forward_mode,
        },
        requires_secret_input=requires_secret_input,
        secret_fields=secret_fields,
    )


def _classic_wpa3_personal_candidate(name: str) -> dict[str, Any]:
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": name,
            "vlan": 30,
            "aaa_profile": None,
            "security": security(
                "wpa3_sae",
                opmode="wpa3-sae-aes",
                passphrase_present=True,
            ),
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa3-sae-aes",
            "virtual_ap.forward_mode": "bridge",
            "ssid_profile.wpa_passphrase": "<redacted:present>",
        },
        requires_secret_input=True,
        secret_fields=["payload.security.wpa_passphrase"],
    )


def _classic_wpa3_enterprise_candidate(name: str) -> dict[str, Any]:
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": name,
            "vlan": 40,
            "aaa_profile": "corp-aaa",
            "security": security(
                "enterprise_dot1x",
                opmode="wpa3-aes-ccm-128",
                dot1x_auth_profile="corp-dot1x",
            ),
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa3-aes-ccm-128",
            "virtual_ap.forward_mode": "bridge",
        },
    )


def _representative_candidates() -> list[CandidateCase]:
    rad1 = candidate(
        "auth_server",
        "radius:eval-rad1",
        payload={"name": "eval-rad1", "server_type": "radius", "host": "10.0.0.10"},
        unsupported_fields={"rad_authport": 1812, "rad_acctport": 1813},
        requires_secret_input=True,
    )
    server_group = candidate(
        "server_group",
        "eval-corp-sg",
        payload={
            "name": "eval-corp-sg",
            "auth_servers": ["eval-rad1"],
            "auth_server_entries": [{"name": "eval-rad1", "position": 1}],
            "fail_through": True,
            "load_balance": False,
        },
        dependencies=[_candidate_key(rad1)],
    )

    return [
        CandidateCase(
            surface="new_central",
            family="vlan",
            variant="basic",
            persona="CAMPUS_AP",
            candidate=candidate("vlan", "20", payload={"description": "eval-vlan-20"}),
        ),
        CandidateCase(
            surface="new_central",
            family="role",
            variant="allow-all",
            persona="CAMPUS_AP",
            candidate=candidate(
                "role",
                "eval-role-allowall",
                payload={"name": "eval-role-allowall", "vlan": 20, "policies": ["allowall"]},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="role",
            variant="custom-acl-unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "role",
                "eval-role-custom-acl",
                payload={"name": "eval-role-custom-acl", "policies": ["corp-acl"]},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="open",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate("eval-open-ssid", "open", opmode="open"),
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="wpa2-personal",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-wpa2-ssid",
                "wpa2_personal",
                opmode="wpa2-aes",
                requires_secret_input=True,
                secret_fields=["payload.security.wpa_passphrase"],
            ),
            secrets={
                "wlan:eval-wpa2-ssid": {
                    "wpa_passphrase": OFFLINE_SECRET_LITERALS["wlan-passphrase"]
                }
            },
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="wpa3-sae",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-wpa3-ssid",
                "wpa3_sae",
                opmode="wpa3-sae",
                requires_secret_input=True,
                secret_fields=["payload.security.wpa_passphrase"],
            ),
            secrets={
                "wlan:eval-wpa3-ssid": {
                    "wpa_passphrase": OFFLINE_SECRET_LITERALS["wlan-passphrase"]
                }
            },
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="enhanced-open",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-enhanced-open-ssid",
                "enhanced_open",
                opmode="enhanced-open",
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="wpa3-transition-blocked",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-transition-ssid",
                "wpa3_transition_personal",
                opmode="wpa3-sae-transition",
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="mac-auth-unsupported",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-macauth-ssid",
                "mac_auth_only",
                opmode="mac-auth",
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="wlan",
            variant="enterprise-unsupported",
            persona="CAMPUS_AP",
            candidate=_wlan_candidate(
                "eval-enterprise-ssid",
                "enterprise_dot1x",
                opmode="wpa2-aes-dot1x",
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="ap_group",
            variant="unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "ap_group",
                "eval-ap-group",
                payload={"name": "eval-ap-group"},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="policy",
            variant="unsupported",
            persona="CAMPUS_AP",
            candidate=candidate("policy", "eval-policy", payload={"name": "eval-policy"}),
        ),
        CandidateCase(
            surface="new_central",
            family="auth_server",
            variant="radius",
            persona="MOBILITY_GW",
            candidate=rad1,
            secrets={
                "auth_server:radius:eval-rad1": {
                    "shared_secret": OFFLINE_SECRET_LITERALS["radius-shared-secret"]
                }
            },
        ),
        CandidateCase(
            surface="new_central",
            family="auth_server",
            variant="ldap",
            persona="MOBILITY_GW",
            candidate=candidate(
                "auth_server",
                "ldap:eval-ldap1",
                payload={"name": "eval-ldap1", "server_type": "ldap", "host": "10.0.0.11"},
                unsupported_fields={
                    "ldap_admindn": "cn=admin,dc=example,dc=com",
                    "ldap_keyattribute": "uid",
                },
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="auth_server",
            variant="tacacs",
            persona="MOBILITY_GW",
            candidate=candidate(
                "auth_server",
                "tacacs:eval-tac1",
                payload={"name": "eval-tac1", "server_type": "tacacs", "host": "10.0.0.12"},
                unsupported_fields={"tacacs_tcpport": 49, "tacacs_timeout": 5},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="auth_server",
            variant="radsec",
            persona="MOBILITY_GW",
            candidate=candidate(
                "auth_server",
                "radsec:eval-radsec1",
                payload={"name": "eval-radsec1", "server_type": "radsec", "host": "10.0.0.13"},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="server_group",
            variant="basic",
            persona="MOBILITY_GW",
            candidate=server_group,
            preview_candidates=(server_group, rad1),
            selected=frozenset({_candidate_key(server_group)}),
            secrets={
                "auth_server:radius:eval-rad1": {
                    "shared_secret": OFFLINE_SECRET_LITERALS["radius-shared-secret"]
                }
            },
        ),
        CandidateCase(
            surface="new_central",
            family="aaa_profile",
            variant="simple",
            persona="MOBILITY_GW",
            candidate=candidate(
                "aaa_profile",
                "eval-guest-aaa",
                payload={
                    "name": "eval-guest-aaa",
                    "default_user_role": "guest",
                    "accounting_server_group": "acct-group",
                },
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="aaa_profile",
            variant="rich",
            persona="MOBILITY_GW",
            candidate=candidate(
                "aaa_profile",
                "eval-rich-aaa",
                payload={
                    "name": "eval-rich-aaa",
                    "default_user_role": "guest",
                    "dot1x_server_group": "corp-sg",
                },
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="dot1x_auth_profile",
            variant="bare",
            persona="MOBILITY_GW",
            candidate=candidate(
                "dot1x_auth_profile",
                "eval-dot1x",
                payload={"name": "eval-dot1x"},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="dot1x_auth_profile",
            variant="rich",
            persona="MOBILITY_GW",
            candidate=candidate(
                "dot1x_auth_profile",
                "eval-dot1x-rich",
                payload={"name": "eval-dot1x-rich"},
                unsupported_fields={"use_session_key": True, "reauthentication": True},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="mac_auth_profile",
            variant="bare",
            persona="MOBILITY_GW",
            candidate=candidate(
                "mac_auth_profile",
                "eval-macauth",
                payload={"name": "eval-macauth"},
            ),
        ),
        CandidateCase(
            surface="new_central",
            family="route",
            variant="unsupported",
            persona="MOBILITY_GW",
            candidate=candidate("route", "ipv4:0.0.0.0"),
        ),
        CandidateCase(
            surface="new_central",
            family="vrrp",
            variant="unsupported",
            persona="MOBILITY_GW",
            candidate=candidate("vrrp", "vrrp:1"),
        ),
        CandidateCase(
            surface="classic_central",
            family="wlan",
            variant="open-bridged-ready",
            persona="CAMPUS_AP",
            candidate=candidate(
                "wlan",
                "eval-classic-open",
                payload={
                    "name": "eval-classic-open",
                    "essid": "eval-classic-open",
                    "vlan": 20,
                    "aaa_profile": None,
                    "security": security("open"),
                },
                unsupported_fields={
                    "ssid_profile.opmode": "open",
                    "virtual_ap.forward_mode": "bridge",
                },
            ),
        ),
        CandidateCase(
            surface="classic_central",
            family="wlan",
            variant="wpa3-personal-missing-secret",
            persona="CAMPUS_AP",
            candidate=_classic_wpa3_personal_candidate("eval-classic-wpa3-missing"),
        ),
        CandidateCase(
            surface="classic_central",
            family="wlan",
            variant="wpa3-personal-with-secret",
            persona="CAMPUS_AP",
            candidate=_classic_wpa3_personal_candidate("eval-classic-wpa3-ready"),
            secrets={
                "wlan:eval-classic-wpa3-ready": {
                    "wpa_passphrase": OFFLINE_SECRET_LITERALS["wpa3-personal"]
                }
            },
        ),
        CandidateCase(
            surface="classic_central",
            family="wlan",
            variant="wpa3-enterprise-no-ref",
            persona="CAMPUS_AP",
            candidate=_classic_wpa3_enterprise_candidate("eval-classic-enterprise-no-ref"),
        ),
        CandidateCase(
            surface="classic_central",
            family="wlan",
            variant="wpa3-enterprise-with-ref-dry-run-only",
            persona="CAMPUS_AP",
            candidate=_classic_wpa3_enterprise_candidate("eval-classic-enterprise-ready"),
            external_object_references={
                "wlan:eval-classic-enterprise-ready": {"auth_server1": "InternalServer"}
            },
        ),
        CandidateCase(
            surface="classic_central",
            family="ap_group",
            variant="no-mapping",
            persona="CAMPUS_AP",
            candidate=candidate(
                "ap_group",
                "eval-classic-ap-group",
                payload={"name": "eval-classic-ap-group"},
            ),
        ),
        CandidateCase(
            surface="classic_central",
            family="ap_group",
            variant="mapped-no-serials",
            persona="CAMPUS_AP",
            candidate=candidate(
                "ap_group",
                "eval-classic-ap-group",
                payload={"name": "eval-classic-ap-group"},
            ),
            ap_group_target_map={"eval-classic-ap-group": "mapped-group"},
        ),
        CandidateCase(
            surface="classic_central",
            family="auth_server",
            variant="ldap-unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "auth_server",
                "ldap:eval-classic-ldap1",
                payload={"name": "eval-classic-ldap1", "server_type": "ldap"},
            ),
        ),
        CandidateCase(
            surface="classic_central",
            family="route",
            variant="unsupported",
            persona="CAMPUS_AP",
            candidate=candidate("route", "ipv4:0.0.0.0"),
        ),
        CandidateCase(
            surface="classic_central",
            family="role",
            variant="custom-acl-unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "role", "eval-classic-custom-role", payload={"policies": ["corp-acl"]}
            ),
        ),
        CandidateCase(
            surface="classic_central",
            family="server_group",
            variant="unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "server_group", "eval-classic-sg", payload={"name": "eval-classic-sg"}
            ),
        ),
        CandidateCase(
            surface="classic_central",
            family="dot1x_auth_profile",
            variant="unsupported",
            persona="CAMPUS_AP",
            candidate=candidate(
                "dot1x_auth_profile",
                "eval-classic-dot1x",
                payload={"name": "eval-classic-dot1x"},
            ),
        ),
    ]


def _snapshot_state_dir() -> list[str]:
    if not STATE_DIR.exists():
        return []
    return sorted(
        str(path.relative_to(REPO_ROOT))
        for path in STATE_DIR.rglob("*")
        if path.is_file()
    )


def _git_commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def _repo_version() -> str | None:
    match = VERSION_RE.search((REPO_ROOT / "pyproject.toml").read_text())
    return match.group(1) if match else None


def _sanitize_identifier(raw: str) -> str:
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _secret_hashes(secret_literals: Iterable[str]) -> list[str]:
    return sorted({_sanitize_identifier(value) for value in secret_literals})


def _sanitize_text(text: str, *, identifiers: Iterable[str], secrets: Iterable[str]) -> str:
    sanitized = str(text)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "******")
    for identifier in sorted({item for item in identifiers if item}, key=len, reverse=True):
        sanitized = sanitized.replace(identifier, _sanitize_identifier(identifier))
    return sanitized


def _install_get_only_request_guard(client: Any, observed_verbs: list[str]) -> Any:
    if getattr(client, "_readonly_eval_guard_installed", False):
        return client._request
    original = client._request

    def guarded(method: str, *args: Any, **kwargs: Any) -> Any:
        verb = str(method or "").upper()
        observed_verbs.append(verb)
        if verb not in SAFE_METHODS:
            raise RuntimeError(
                f"Blocked non-read-only HTTP method before transmission: {verb}."
            )
        return original(method, *args, **kwargs)

    guarded._readonly_eval_original = original  # type: ignore[attr-defined]
    client._request = guarded
    client._readonly_eval_guard_installed = True
    return original


def _install_no_token_refresh_guard(client: Any) -> Any:
    token_manager = getattr(client, "token_manager", None)
    if token_manager is None or getattr(token_manager, "_readonly_eval_refresh_guard", False):
        return getattr(token_manager, "_refresh_token", None)
    original = token_manager._refresh_token

    def blocked_refresh() -> None:
        raise RuntimeError(
            "Blocked OAuth token refresh before transmission: warm the Central "
            "token cache first, then rerun -- live read-only mode permits no "
            "POST/PUT/PATCH/DELETE network traffic."
        )

    token_manager._refresh_token = blocked_refresh
    token_manager._readonly_eval_refresh_guard = True
    return original


def _live_get_only_client_ready() -> tuple[bool, str | None]:
    try:
        creds_path = os.environ.get("CREDS_PATH", "config/credentials.yaml")
        source_ctx, _ = build_account_contexts(creds_path)
        token_manager = TokenManager(
            client_id=source_ctx.client_id,
            client_secret=source_ctx.client_secret,
            cache_context=f"{source_ctx.base_url}|{source_ctx.glp_workspace_id}",
            cache_key="source",
        )
    except Exception as exc:
        return False, f"Central credentials/token cache preflight failed: {exc}"

    expires_at = getattr(token_manager, "token_expires_at", None)
    expiry_buffer = getattr(token_manager, "expiry_buffer", 0)
    has_valid_cache = bool(
        getattr(token_manager, "access_token", None)
        and expires_at
        and time.time() < (float(expires_at) - float(expiry_buffer))
    )
    if has_valid_cache:
        return True, None
    return (
        False,
        "No valid cached Central token is available. Constructing the live "
        "client would require an OAuth POST refresh, so GET-only mode is "
        "blocked until the token cache is warmed out of band.",
    )


def _result_entry(
    case: CandidateCase,
    operation: Mapping[str, Any] | None,
    *,
    live_mode: bool,
) -> dict[str, Any]:
    operation = operation or {}
    return {
        "surface": case.surface,
        "family": case.family,
        "variant": case.variant,
        "persona": case.persona,
        "status": operation.get("status", "missing"),
        "dry_run_only": bool(operation.get("dry_run_only", False)),
        "conflict": operation.get("conflict", "not-checked"),
        "coverage": "live_get_only" if live_mode else "fixture_backed",
    }


def _preview_operation_for_case(
    preview: Mapping[str, Any], case: CandidateCase
) -> Mapping[str, Any] | None:
    target_key = _candidate_key(case.candidate)
    for operation in preview.get("operations", []):
        if isinstance(operation, Mapping) and operation.get("candidate") == target_key:
            return operation
    return None


def _run_offline() -> dict[str, Any]:
    cases = _representative_candidates()
    raw_previews: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    secret_literals = set(OFFLINE_SECRET_LITERALS.values())

    for case in cases:
        backend = FakeBackend()
        preview_candidates = list(case.preview_candidates or (case.candidate,))
        if case.surface == "new_central":
            adapter = new_adapter(
                backend,
                persona=case.persona,
                secrets=case.secrets or None,
            )
        else:
            adapter = classic_adapter(
                backend,
                secrets=case.secrets or None,
                external_object_references=case.external_object_references or None,
                ap_group_target_map=case.ap_group_target_map or None,
                ap_group_device_serials=case.ap_group_device_serials or None,
            )
        selected = set(case.selected) if case.selected else None
        preview = adapter.preview(preview_candidates, selected=selected)
        raw_previews.append(preview)
        operation = _preview_operation_for_case(preview, case)
        results.append(_result_entry(case, operation, live_mode=False))

    return {
        "results": results,
        "raw_previews": raw_previews,
        "secret_literals": sorted(secret_literals),
        "surface_modes": {
            "new_central": "fixture_backed",
            "classic_central": "fixture_backed",
            "aos8_source": "fixture_backed",
        },
        "blockers": [],
        "observed_http_verbs": [],
        "scope_resolution": {"mode": "offline", "scope_name": None, "scope_id": None},
    }


def _resolve_live_scope_name(
    global_scope: Mapping[str, Any] | None,
    scopes: Mapping[str, Any] | None,
) -> tuple[str, str, list[str]]:
    blockers: list[str] = []
    global_scope_id = None
    if isinstance(global_scope, Mapping):
        global_scope_id = global_scope.get("global_scope_id")
        blockers.extend(str(item) for item in global_scope.get("errors", []) or [])
    if global_scope_id:
        return str(global_scope_id), "Global", blockers

    items = scopes.get("items", []) if isinstance(scopes, Mapping) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        scope_id = item.get("scope_id") or item.get("scopeId") or item.get("id")
        scope_name = item.get("scope_name") or item.get("scopeName") or item.get("name")
        if scope_id and scope_name:
            return str(scope_id), str(scope_name), blockers
    raise RuntimeError(
        "; ".join(blockers) or "Could not resolve a live New Central scope."
    )


def _group_live_new_central_candidates(
    cases: Sequence[CandidateCase],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for case in cases:
        if case.surface != "new_central":
            continue
        grouped.setdefault(case.persona, {})
        grouped[case.persona][_candidate_key(case.candidate)] = case.candidate
        for extra in case.preview_candidates:
            grouped[case.persona][_candidate_key(extra)] = extra
    return {persona: list(entries.values()) for persona, entries in grouped.items()}


def _run_live_new_central() -> dict[str, Any]:
    cases = [case for case in _representative_candidates() if case.surface == "new_central"]
    raw_previews: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    observed_verbs: list[str] = []
    secret_literals = {PLACEHOLDER_SECRET}
    blockers: list[str] = []
    scope_resolution: dict[str, Any] = {
        "mode": "live_get_only",
        "scope_name": None,
        "scope_id": None,
    }

    try:
        ready, reason = _live_get_only_client_ready()
        if not ready:
            raise RuntimeError(reason or "Live GET-only preflight failed.")
        client = shared_tools.get_client()
        _install_get_only_request_guard(client, observed_verbs)
        _install_no_token_refresh_guard(client)
        global_scope = monitoring_tools.get_global_scope_id()
        scopes = monitoring_tools.list_scopes(full_list=True)
        resolved_scope_id, resolved_scope_name, scope_blockers = _resolve_live_scope_name(
            global_scope, scopes
        )
        blockers.extend(scope_blockers)
        scope_resolution = {
            "mode": "live_get_only",
            "scope_name": resolved_scope_name,
            "scope_id": resolved_scope_id,
        }
        for persona, candidates in _group_live_new_central_candidates(cases).items():
            preview = aos8_tools.aos8_preview_migration_run(
                target_type="new_central",
                candidates=candidates,
                scope_name=resolved_scope_name,
                persona=persona,
                conflict_policy="fail",
            )
            raw_previews.append(preview)
            if preview.get("status") == "blocked" and preview.get("error"):
                blockers.append(str(preview["error"]))
        previews_by_candidate = {
            operation.get("candidate"): operation
            for preview in raw_previews
            for operation in preview.get("operations", [])
            if isinstance(operation, Mapping) and operation.get("candidate")
        }
        for case in cases:
            results.append(
                _result_entry(
                    case,
                    previews_by_candidate.get(_candidate_key(case.candidate)),
                    live_mode=True,
                )
            )
    except Exception as exc:
        blockers.append(str(exc))

    return {
        "results": results,
        "raw_previews": raw_previews,
        "secret_literals": sorted(secret_literals),
        "surface_modes": {
            "new_central": "live_get_only" if results else "live_requested_blocked",
            "classic_central": "fixture_backed",
            "aos8_source": "fixture_backed",
        },
        "blockers": blockers,
        "observed_http_verbs": observed_verbs,
        "scope_resolution": scope_resolution,
    }


def _build_report(*, live_new_central_readonly: bool) -> dict[str, Any]:
    before_state = _snapshot_state_dir()
    offline = _run_offline()
    live = _run_live_new_central() if live_new_central_readonly else None
    after_state = _snapshot_state_dir()

    results = [item for item in offline["results"] if item["surface"] == "classic_central"]
    results.extend(
        live["results"] if live_new_central_readonly and live is not None else [
            item for item in offline["results"] if item["surface"] == "new_central"
        ]
    )

    raw_previews = list(offline["raw_previews"])
    if live is not None:
        raw_previews.extend(live["raw_previews"])

    secret_literals = set(offline["secret_literals"])
    if live is not None:
        secret_literals.update(live["secret_literals"])

    active_scope_resolution = (
        live["scope_resolution"] if live is not None else offline["scope_resolution"]
    )
    report_mode = (
        "live_new_central_readonly" if live_new_central_readonly else "offline_fixture_backed"
    )
    new_central_coverage = (
        (live or offline)["surface_modes"]["new_central"]
        if live_new_central_readonly
        else offline["surface_modes"]["new_central"]
    )
    report: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "commit_sha": _git_commit_sha(),
            "version": _repo_version(),
            "mode": report_mode,
        },
        "coverage_classification": {
            "new_central": new_central_coverage,
            "classic_central": "fixture_backed",
            "aos8_source": "fixture_backed",
        },
        "scope_resolution": {
            key: (_sanitize_identifier(value) if value else None)
            for key, value in active_scope_resolution.items()
            if key in {"scope_name", "scope_id"}
        } | {"mode": active_scope_resolution["mode"]},
        "results": results,
        "observed_http_verbs": sorted(set((live or offline)["observed_http_verbs"])),
        "state_aos8_migrations": {
            "before": before_state,
            "after": after_state,
            "unchanged": before_state == after_state,
        },
        "no_write_confirmation": {
            "write_attempted": False,
            "confirmation_true_passed": False,
            "called_aos8_create_migration_run": False,
            "called_aos8_apply_migration_run": False,
            "statement": "No write was attempted; confirmation=True was never passed.",
        },
        "blockers": [
            _sanitize_text(
                blocker,
                identifiers=[
                    str((live or offline)["scope_resolution"].get("scope_name") or ""),
                    str((live or offline)["scope_resolution"].get("scope_id") or ""),
                ],
                secrets=secret_literals,
            )
            for blocker in ((live["blockers"] if live is not None else []) + offline["blockers"])
        ],
    }

    serialized_report = json.dumps(report, sort_keys=True)
    serialized_previews = [json.dumps(item, sort_keys=True) for item in raw_previews]
    report_leaks = [
        _sanitize_identifier(secret)
        for secret in sorted(secret_literals)
        if secret and secret in serialized_report
    ]
    preview_leaks = sorted(
        {
            _sanitize_identifier(secret)
            for secret in secret_literals
            if secret and any(secret in payload for payload in serialized_previews)
        }
    )
    report["secret_leak_assertion"] = {
        "checked_literal_hashes": _secret_hashes(secret_literals),
        "report_leaks": report_leaks,
        "preview_leaks": preview_leaks,
        "passed": not report_leaks and not preview_leaks,
    }
    return report


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# AOS8 0.5.0 read-only evaluation reproduction",
        "",
        f"- Commit: `{report['metadata'].get('commit_sha') or 'unknown'}`",
        f"- Version: `{report['metadata'].get('version') or 'unknown'}`",
        f"- Mode: `{report['metadata'].get('mode')}`",
        "",
        "## Coverage classification",
        "",
        "| Surface | Coverage |",
        "|---|---|",
    ]
    for surface, coverage in report["coverage_classification"].items():
        lines.append(f"| {surface} | {coverage} |")

    lines.extend(
        [
            "",
            "## Scope resolution",
            "",
            f"- mode: `{report['scope_resolution'].get('mode')}`",
            f"- scope_name: `{report['scope_resolution'].get('scope_name') or 'n/a'}`",
            f"- scope_id: `{report['scope_resolution'].get('scope_id') or 'n/a'}`",
            "",
            "## Per-family / per-mode results",
            "",
            "| Surface | Family | Variant | Persona | Status | Dry-run-only | Coverage |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in report["results"]:
        lines.append(
            "| {surface} | {family} | {variant} | {persona} | {status} "
            "| {dry_run_only} | {coverage} |".format(**row)
        )

    verbs = report["observed_http_verbs"] or ["none"]
    leak_info = report["secret_leak_assertion"]
    write_info = report["no_write_confirmation"]
    lines.extend(
        [
            "",
            "## Observed HTTP verbs",
            "",
            f"- {', '.join(verbs)}",
            "",
            "## Secret leak assertion",
            "",
            f"- passed: `{leak_info['passed']}`",
            f"- checked literal hashes: `{', '.join(leak_info['checked_literal_hashes'])}`",
            f"- report leaks: `{', '.join(leak_info['report_leaks']) or 'none'}`",
            f"- preview leaks: `{', '.join(leak_info['preview_leaks']) or 'none'}`",
            "",
            "## No-write confirmation",
            "",
            f"- {write_info['statement']}",
            f"- create_run called: `{write_info['called_aos8_create_migration_run']}`",
            f"- apply_run called: `{write_info['called_aos8_apply_migration_run']}`",
            "",
            "## State persistence",
            "",
            f"- unchanged: `{report['state_aos8_migrations']['unchanged']}`",
            f"- before: `{report['state_aos8_migrations']['before']}`",
            f"- after: `{report['state_aos8_migrations']['after']}`",
            "",
            "## Blockers",
            "",
        ]
    )
    blockers = report.get("blockers") or ["none"]
    for blocker in blockers:
        lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def _write_output(text: str, output: str | None) -> None:
    if output is None:
        print(text, end="")
        return
    path = Path(output)
    path.write_text(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output")
    parser.add_argument("--live-new-central-readonly", action="store_true")
    args = parser.parse_args(argv)

    report = _build_report(live_new_central_readonly=args.live_new_central_readonly)
    if args.format == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        rendered = _render_markdown(report)
    _write_output(rendered, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
