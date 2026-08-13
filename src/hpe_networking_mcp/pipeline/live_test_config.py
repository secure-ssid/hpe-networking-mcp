"""Credential-gated live-test configuration shared by v0.7 evaluation harnesses.

Every v0.7 workstream that wants to run bounded, real requests against a
live platform (Central, GLP, AOS8, or an optional product backend) during
manual/lab evaluation should resolve its authorization through this module
instead of inventing its own environment-variable convention -- this is the
generalized, reusable form of the gating already hand-rolled in
``scripts/evaluate_aos8_060_lab.py`` (see its
``_explicit_central_write_gate_enabled``).

Hard safety rules enforced here (see ``tests/unit/test_live_test_config.py``):

- Every platform defaults to fully disabled: no live calls of any kind,
  regardless of whether platform credentials are configured in the
  environment. Credential *presence* is reported for diagnostics only and
  never implies authorization.
- Read-only live calls require an explicit, separate opt-in
  (``HPE_MCP_LIVE_TEST_<PLATFORM>_READ=1``).
- Disposable-write live calls (a bounded create/read-back/delete round trip
  against a lab-owned target) require *both* the read opt-in and a second,
  explicit write opt-in (``HPE_MCP_LIVE_TEST_<PLATFORM>_WRITE=1``).
  Write is never derived from read being enabled, and never derived from
  credentials merely existing.
- The status/validation API (:func:`live_test_status`) reports env var
  names and boolean/"configured" state only; it never reads back or echoes
  a credential value.

This module intentionally does not overlap with the existing
``hpe_networking_mcp.mcp_servers.shared`` per-platform *write-tool* gates
(``HPE_MCP_CENTRAL_WRITES``, ``HPE_MCP_GLP_V2BETA1_WRITES``,
``HPE_MCP_PRODUCT_ACCESS``, etc.), which control whether the always-on
MCP servers expose write tools to a client. Those govern production tool
exposure; this module governs opt-in, local, disposable *evaluation
harnesses* and is meant to be reused by future per-platform live-test
scripts (mirroring ``scripts/evaluate_aos8_060_lab.py``) rather than
duplicated per platform.
"""

from __future__ import annotations

import os

from hpe_networking_mcp.mcp_servers.shared import PLATFORM_WRITE_GATE_NAMES

_TRUTHY = {"1", "true", "yes", "on"}
_PLACEHOLDER_MARKERS = ("your_", "your-", "replace_me", "placeholder")

# Reuses the same canonical platform key set as the production write gates
# (hpe_networking_mcp.mcp_servers.shared.PLATFORM_WRITE_GATE_NAMES) so every v0.7 harness
# refers to platforms with one consistent name.
LIVE_TEST_PLATFORMS: tuple[str, ...] = PLATFORM_WRITE_GATE_NAMES

# Env var *names* required per platform -- never read into a returned
# value, log line, or exception message; only their presence/placeholder
# state is ever reported (see credentials_configured/live_test_status).
_CREDENTIAL_ENV_VARS: dict[str, tuple[str, ...]] = {
    "central": ("SOURCE_BASE_URL", "SOURCE_CLIENT_ID", "SOURCE_CLIENT_SECRET"),
    "glp": ("TARGET_BASE_URL", "TARGET_CLIENT_ID", "TARGET_CLIENT_SECRET"),
    "aos8": ("AOS8_BASE_URL", "AOS8_USERNAME", "AOS8_PASSWORD"),
    "edgeconnect": ("EDGECONNECT_BASE_URL", "EDGECONNECT_API_TOKEN"),
    "apstra": ("APSTRA_BASE_URL", "APSTRA_API_TOKEN"),
    "mist": ("MIST_HOST", "MIST_API_TOKEN"),
    "clearpass": ("CLEARPASS_BASE_URL", "CLEARPASS_API_TOKEN"),
    "uxi": ("UXI_CLIENT_ID", "UXI_CLIENT_SECRET"),
    "axis": ("AXIS_BASE_URL", "AXIS_API_TOKEN"),
}
assert set(_CREDENTIAL_ENV_VARS) == set(LIVE_TEST_PLATFORMS)  # keep the two lists in lockstep


def _validate_platform(platform: str) -> str:
    key = platform.strip().lower()
    if key not in _CREDENTIAL_ENV_VARS:
        raise ValueError(
            f"unknown live-test platform {platform!r}; expected one of {LIVE_TEST_PLATFORMS}"
        )
    return key


def credential_env_vars(platform: str) -> tuple[str, ...]:
    """Return the env var *names* (never values) required for ``platform``."""
    return _CREDENTIAL_ENV_VARS[_validate_platform(platform)]


def live_test_read_env_var(platform: str) -> str:
    """Return the read opt-in env var name for ``platform``."""
    return f"HPE_MCP_LIVE_TEST_{_validate_platform(platform).upper()}_READ"


def live_test_write_env_var(platform: str) -> str:
    """Return the disposable-write opt-in env var name for ``platform``."""
    return f"HPE_MCP_LIVE_TEST_{_validate_platform(platform).upper()}_WRITE"


def _flag_enabled(env_var: str) -> bool:
    return os.getenv(env_var, "").strip().lower() in _TRUTHY


def live_test_read_enabled(platform: str) -> bool:
    """Return whether bounded, read-only live calls are explicitly enabled.

    Defaults to disabled. Never inferred from credentials existing.
    """
    return _flag_enabled(live_test_read_env_var(platform))


def live_test_write_enabled(platform: str) -> bool:
    """Return whether disposable, lab-owned live writes are explicitly enabled.

    Requires *both* the read and write opt-ins -- never inferred from
    credentials existing, and never enabled by the write flag alone (a
    disposable-write probe always needs bounded reads to confirm cleanup).
    """
    key = _validate_platform(platform)
    return live_test_read_enabled(key) and _flag_enabled(live_test_write_env_var(key))


def _is_placeholder(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def credentials_configured(platform: str) -> bool:
    """Return whether every required credential env var looks populated.

    Reports presence only -- never reads a credential value into a return
    payload, log line, or exception message. A True result never implies
    read or write authorization by itself.
    """
    key = _validate_platform(platform)
    for name in _CREDENTIAL_ENV_VARS[key]:
        value = os.getenv(name, "").strip()
        if not value or _is_placeholder(value):
            return False
    return True


def live_test_status(platform: str) -> dict[str, object]:
    """Return a redacted status/validation summary for ``platform``.

    Never includes a credential value. Explicitly notes that credential
    presence does not grant read or write authorization.
    """
    key = _validate_platform(platform)
    read_env = live_test_read_env_var(key)
    write_env = live_test_write_env_var(key)
    return {
        "platform": key,
        "read_env_var": read_env,
        "write_env_var": write_env,
        "read_enabled": live_test_read_enabled(key),
        "write_enabled": live_test_write_enabled(key),
        "credential_env_vars": list(_CREDENTIAL_ENV_VARS[key]),
        "credentials_configured": credentials_configured(key),
        "note": (
            "Credential presence never grants read or write authorization; "
            f"set {read_env}=1 and/or {write_env}=1 explicitly."
        ),
    }
