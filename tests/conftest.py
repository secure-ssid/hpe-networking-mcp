"""Shared fixtures for all tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.models import (
    AccountContext,
    DeviceRecord,
    FirmwareAction,
    HardwareSeries,
    Persona,
    SourceType,
    TargetAccount,
)
from hpe_networking_mcp.pipeline.state_store import StateStore


@pytest.fixture(autouse=True)
def _allow_placeholder_urls_in_tests(monkeypatch):
    """Let the suite keep using RFC 2606 ``*.example.com`` hosts as fixtures.

    ``pipeline.url_validation.validate_infra_url`` refuses unedited
    documentation placeholder hosts by default, which is exactly what every
    backend fixture here uses as its stand-in product host
    (``apstra.example.com``, ``orch.example.com``, ...). Opt those fixtures
    back in globally so the guard is exercised *deliberately* -- the tests
    that assert the guard fires (``tests/unit/test_placeholder_url_guard.py``)
    delete this variable first, the same way existing tests delete
    ``HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS`` before asserting the local-host rule.
    """
    monkeypatch.setenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", "1")
    yield


# ---------------------------------------------------------------------------
# DeviceRecord fixtures — one per source_type
# ---------------------------------------------------------------------------


@pytest.fixture
def record_unmanaged() -> DeviceRecord:
    return DeviceRecord(
        serial_number="CN00UNMANAGED",
        source_type=SourceType.UNMANAGED,
        hardware_series=HardwareSeries.AOS_CX,
        target_account=TargetAccount.SAME,
        target_site="HQ",
        target_group="Onboarding",
        persona=Persona.ACCESS_SWITCH,
        firmware_target="10.13.1010",
    )


@pytest.fixture
def record_classic_central() -> DeviceRecord:
    return DeviceRecord(
        serial_number="CN00CLASSIC01",
        source_type=SourceType.CLASSIC_CENTRAL,
        hardware_series=HardwareSeries.AOS_CX,
        target_account=TargetAccount.SAME,
        target_site="Building-A",
        target_group="CX-Switches",
        persona=Persona.CORE_SWITCH,
        firmware_target="10.13.1010",
    )


@pytest.fixture
def record_aos8() -> DeviceRecord:
    return DeviceRecord(
        serial_number="CN00AOS8DEV01",
        source_type=SourceType.AOS8,
        hardware_series=HardwareSeries.AOS_CX,
        target_account=TargetAccount.NEW,
        target_site="Campus-B",
        target_group="NewCentral-CX",
        persona=Persona.AGGREGATION_SWITCH,
        firmware_target="10.13.1010",
    )


@pytest.fixture
def record_aos_s() -> DeviceRecord:
    """AOS-S device — firmware upgrade should be skipped."""
    return DeviceRecord(
        serial_number="CN00AOSSDEV01",
        source_type=SourceType.CLASSIC_CENTRAL,
        hardware_series=HardwareSeries.AOS_S,
        target_account=TargetAccount.SAME,
        target_site="Warehouse",
        target_group="Legacy-Switches",
        persona=Persona.ACCESS_SWITCH,
        firmware_target="10.13.1010",
        firmware_action=FirmwareAction.SKIP,
    )


# ---------------------------------------------------------------------------
# AccountContext with mocked clients
# ---------------------------------------------------------------------------


def _mock_account_context(label: str) -> AccountContext:
    ctx = AccountContext(
        label=label,
        base_url="https://mock.api.central.arubanetworks.com",
        client_id="mock-client-id",
        client_secret="mock-secret",
        glp_workspace_id="mock-workspace",
    )
    ctx.central_client = MagicMock()
    ctx.glp_client = MagicMock()
    ctx.mcp_client = MagicMock()
    return ctx


@pytest.fixture
def source_ctx() -> AccountContext:
    return _mock_account_context("source")


@pytest.fixture
def target_ctx() -> AccountContext:
    return _mock_account_context("target")


# ---------------------------------------------------------------------------
# In-memory state store
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "test_pipeline.db"))


@pytest.fixture
def run_id() -> str:
    return "test-run-001"
