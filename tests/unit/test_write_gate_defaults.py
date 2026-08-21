"""Every platform write gate must be deny-by-default.

Regression lock for the pre-0.9.1 posture where Central alone defaulted to
enabled, so `import hpe_networking_mcp` with no environment produced a server
that would dispatch Central writes and reboots.
"""
import pytest

from hpe_networking_mcp.mcp_servers.shared import (
    PLATFORM_WRITE_GATE_NAMES,
    platform_writes_allowed,
)

_GATE_ENV_VARS = [
    "HPE_MCP_ACCESS_PROFILE",
    "HPE_MCP_READONLY",
    "HPE_MCP_PRODUCT_ACCESS",
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_GLP_V2BETA1_WRITES",
    "HPE_MCP_AOS8_WRITES",
    "HPE_MCP_EDGECONNECT_WRITES",
    "HPE_MCP_APSTRA_WRITES",
    "HPE_MCP_MIST_WRITES",
    "HPE_MCP_CLEARPASS_WRITES",
    "HPE_MCP_UXI_WRITES",
    "HPE_MCP_AXIS_WRITES",
]


@pytest.fixture
def pristine_env(monkeypatch):
    """No gate variable set at all — the bare `import` case."""
    for name in _GATE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("platform", sorted(PLATFORM_WRITE_GATE_NAMES))
def test_writes_denied_when_no_environment_is_set(pristine_env, platform):
    assert platform_writes_allowed(platform) is False, (
        f"{platform} writes are enabled with no environment configured"
    )


def test_central_writes_still_enablable(pristine_env, monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    assert platform_writes_allowed("central") is True
