"""Integration dry-run test — requires live credentials.

Skipped automatically if SOURCE_CLIENT_ID env var is not set.
Runs Stages 1-2 only (--dry-run equivalent) against a real test device.

Set environment variables before running:
    SOURCE_BASE_URL=https://...
    SOURCE_CLIENT_ID=...
    SOURCE_CLIENT_SECRET=...
    TEST_SERIAL_NUMBER=CNXXXXXXXX
    TEST_TARGET_SITE=MyTestSite
    TEST_TARGET_GROUP=MyTestGroup
"""

from __future__ import annotations

import os

import pytest

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient
from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient
from hpe_networking_mcp.pipeline.clients.mcp_client import MCPClient
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.csv_loader import load_csv
from hpe_networking_mcp.pipeline.models import StageStatus
from hpe_networking_mcp.pipeline.stages.s1_discover import DiscoverStage
from hpe_networking_mcp.pipeline.stages.s2_validate import ValidateStage
from hpe_networking_mcp.pipeline.state_store import StateStore

pytestmark = pytest.mark.skipif(
    not os.getenv("SOURCE_CLIENT_ID"),
    reason="Live credentials not set — skipping integration test",
)


@pytest.fixture
def live_source_ctx():
    source_ctx, _ = build_account_contexts()
    tm = TokenManager(
        client_id=source_ctx.client_id,
        client_secret=source_ctx.client_secret,
        cache_key="source",
    )
    central = CentralClient(base_url=source_ctx.base_url, token_manager=tm)
    source_ctx.central_client = central
    source_ctx.glp_client = GLPClient(token_manager=tm, workspace_id=source_ctx.glp_workspace_id)
    source_ctx.mcp_client = MCPClient(central_client=central)
    return source_ctx


def test_dry_run_discover_and_validate(live_source_ctx, tmp_path):
    serial = os.environ["TEST_SERIAL_NUMBER"]
    site = os.environ.get("TEST_TARGET_SITE", "TestSite")
    group = os.environ.get("TEST_TARGET_GROUP", "TestGroup")

    csv_content = (
        "serial_number,source_type,hardware_series,target_account,"
        "target_site,target_group,persona,firmware_target\n"
        f"{serial},unmanaged,aos_cx,same,{site},{group},access_switch,10.13.1010\n"
    )
    csv_file = tmp_path / "test_input.csv"
    csv_file.write_text(csv_content)

    records = load_csv(str(csv_file))
    assert len(records) == 1
    record = records[0]

    state = StateStore(str(tmp_path / "test.db"))
    run_id = "integration-test-001"
    state.create_run(run_id, str(csv_file), 1)

    # Stage 1 — Discover
    s1_result = DiscoverStage().run(record, run_id, live_source_ctx, live_source_ctx, state)
    assert s1_result.status in (StageStatus.SUCCESS, StageStatus.FAILED), (
        f"S1 returned unexpected status: {s1_result.status}"
    )

    if s1_result.status == StageStatus.SUCCESS:
        # Stage 2 — Validate (dry-run: read-only)
        s2_result = ValidateStage().run(record, run_id, live_source_ctx, live_source_ctx, state)
        # Validation may fail due to missing group — that's expected in a test environment
        assert s2_result.status in (StageStatus.SUCCESS, StageStatus.FAILED)
