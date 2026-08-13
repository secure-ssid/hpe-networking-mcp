"""HPE Aruba Switch Migration Pipeline — CLI entry point.

Usage:
    python run_pipeline.py --input inputs/batch1.csv [options]

Options:
    --input FILE           Input CSV file path (required)
    --creds FILE           Credentials YAML (default: config/credentials.yaml)
    --resume RUN_ID        Resume a prior run (skips already-succeeded stages)
    --dry-run              Run Stages 1-2 only (no writes)
    --devices SN1,SN2      Limit execution to specific serial numbers
    --stage-from STAGE     Start from a specific stage name (e.g. s5_onboard)
    --stage-to STAGE       Stop after a specific stage name (e.g. s6_configure)
    --configure-only       Run S6 only — assign group/persona/site on already-provisioned devices
    --output-dir PATH      Output directory (default: outputs/)
    --log-level LEVEL      DEBUG | INFO | WARNING (default: INFO)
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient
from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient
from hpe_networking_mcp.pipeline.clients.mcp_client import MCPClient
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.csv_loader import CSVValidationError, load_csv
from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, StageStatus
from hpe_networking_mcp.pipeline.reporter import write_reports
from hpe_networking_mcp.pipeline.stages.s1_discover import DiscoverStage
from hpe_networking_mcp.pipeline.stages.s2_validate import ValidateStage
from hpe_networking_mcp.pipeline.stages.s3_offboard import OffboardStage
from hpe_networking_mcp.pipeline.stages.s4_transfer import TransferStage
from hpe_networking_mcp.pipeline.stages.s5_onboard import OnboardStage
from hpe_networking_mcp.pipeline.stages.s6_configure import ConfigureStage
from hpe_networking_mcp.pipeline.stages.s7_firmware import FirmwareStage
from hpe_networking_mcp.pipeline.stages.s8_verify import VerifyStage
from hpe_networking_mcp.pipeline.state_store import StateStore

console = Console()

ALL_STAGES = [
    DiscoverStage(),
    ValidateStage(),
    OffboardStage(),
    TransferStage(),
    OnboardStage(),
    ConfigureStage(),
    FirmwareStage(),
    VerifyStage(),
]

STAGE_NAMES = [s.name for s in ALL_STAGES]


def _build_clients(ctx: AccountContext, cache_key: str) -> None:
    """Instantiate and attach clients to an AccountContext."""
    if not ctx.client_id or not ctx.client_secret:
        return  # Skip — same-account target may not need separate creds

    central_tm = TokenManager(
        client_id=ctx.client_id,
        client_secret=ctx.client_secret,
        cache_context=f"{ctx.base_url}|{ctx.glp_workspace_id}",
        cache_key=cache_key,
    )
    glp_tm = TokenManager(
        client_id=ctx.client_id,
        client_secret=ctx.client_secret,
        token_url=ctx.glp_token_url,
        cache_context=f"{ctx.glp_base_url}|{ctx.glp_workspace_id}",
        cache_key=f"{cache_key}-glp",
        expiry_buffer=60,
    )
    central = CentralClient(base_url=ctx.base_url, token_manager=central_tm)
    glp = GLPClient(
        token_manager=glp_tm,
        workspace_id=ctx.glp_workspace_id,
        base_url=ctx.glp_base_url,
    )
    mcp = MCPClient(central_client=central)

    ctx.central_client = central
    ctx.glp_client = glp
    ctx.mcp_client = mcp


def _run_device(
    record: DeviceRecord,
    run_id: str,
    source_ctx: AccountContext,
    target_ctx: AccountContext,
    state: StateStore,
    stages: list,
    dry_run: bool,
) -> None:
    for stage in stages:
        result = stage.run(record, run_id, source_ctx, target_ctx, state, dry_run)
        if result.status == StageStatus.FAILED:
            console.print(
                f"  [red]✗[/red] [{stage.name}] {record.serial_number} — {result.error}"
            )
            break  # Stop processing this device on first failure
        elif result.status == StageStatus.SUCCESS:
            console.print(f"  [green]✓[/green] [{stage.name}] {record.serial_number}")
        else:
            console.print(
                f"  [yellow]⊘[/yellow] [{stage.name}] {record.serial_number} — "
                f"{result.data.get('reason', 'skipped')}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="HPE Aruba Switch Migration Pipeline")
    parser.add_argument("--input", required=True, help="Input CSV file path")
    parser.add_argument("--creds", default="config/credentials.yaml", help="Credentials YAML file")
    parser.add_argument("--resume", metavar="RUN_ID", help="Resume a prior run")
    parser.add_argument("--dry-run", action="store_true", help="Run S1+S2 only, no writes")
    parser.add_argument(
        "--devices",
        metavar="SN1,SN2",
        help="Comma-separated serial numbers to process",
    )
    parser.add_argument(
        "--stage-from",
        metavar="STAGE",
        choices=STAGE_NAMES,
        help="Start from this stage",
    )
    parser.add_argument(
        "--stage-to",
        metavar="STAGE",
        choices=STAGE_NAMES,
        help="Stop after this stage",
    )
    parser.add_argument(
        "--configure-only",
        action="store_true",
        help="Run S6 only (group/persona/site) on already-provisioned devices",
    )
    parser.add_argument("--output-dir", default="outputs", help="Output directory")
    parser.add_argument(
        "--report-formats",
        default="csv",
        help="Comma-separated report formats: csv,json,html (default: csv)",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load + validate CSV
    try:
        records = load_csv(args.input)
    except (FileNotFoundError, CSVValidationError) as exc:
        console.print(f"[red]Error loading CSV:[/red] {exc}")
        sys.exit(1)

    # Filter to specific devices if requested
    if args.devices:
        serials = {s.strip().upper() for s in args.devices.split(",")}
        records = [r for r in records if r.serial_number in serials]
        if not records:
            console.print("[red]No matching devices found for --devices filter.[/red]")
            sys.exit(1)

    # Determine active stages
    active_stages = ALL_STAGES
    if args.dry_run:
        active_stages = ALL_STAGES[:2]  # S1 + S2 only
    elif args.configure_only:
        active_stages = [s for s in ALL_STAGES if s.name == "s6_configure"]
    else:
        if args.stage_from:
            start_idx = STAGE_NAMES.index(args.stage_from)
            active_stages = ALL_STAGES[start_idx:]
        if args.stage_to:
            end_idx = STAGE_NAMES.index(args.stage_to)
            active_stages = [s for s in active_stages if STAGE_NAMES.index(s.name) <= end_idx]

    # Build credentials + clients
    try:
        source_ctx, target_ctx = build_account_contexts(args.creds)
    except Exception as exc:
        console.print(f"[red]Error loading credentials:[/red] {exc}")
        sys.exit(1)

    _build_clients(source_ctx, cache_key="source")

    # For same-account: target == source
    if target_ctx.client_id:
        _build_clients(target_ctx, cache_key="target")
    else:
        target_ctx.central_client = source_ctx.central_client
        target_ctx.glp_client = source_ctx.glp_client
        target_ctx.mcp_client = source_ctx.mcp_client

    # State store
    run_id = args.resume or (
        f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    db_path = f"state/pipeline_{run_id}.db"
    state = StateStore(db_path)
    state.create_run(run_id, args.input, len(records))

    mode = "configure-only" if args.configure_only else ("dry-run" if args.dry_run else "live")
    console.print(f"\n[bold]Migration Pipeline[/bold] — run_id=[cyan]{run_id}[/cyan]")
    console.print(f"Devices: {len(records)}  |  Mode: {mode}  |  Stages: {len(active_stages)}\n")

    started_at = datetime.now(tz=timezone.utc)

    for record in records:
        console.print(
            f"[bold]{record.serial_number}[/bold] "
            f"({record.source_type.value} → {record.target_account.value})"
        )
        _run_device(record, run_id, source_ctx, target_ctx, state, active_stages, args.dry_run)

    ended_at = datetime.now(tz=timezone.utc)
    state.complete_run(run_id)

    # Write report
    report_formats = tuple(
        item.strip()
        for item in args.report_formats.split(",")
        if item.strip()
    )
    report_paths = write_reports(
        records, run_id, state,
        output_dir=args.output_dir,
        started_at=started_at,
        ended_at=ended_at,
        formats=report_formats,
    )

    # Summary table
    table = Table(title="Migration Summary")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    done = sum(
        1 for r in records
        if state.get_stage_status(r.serial_number, run_id, "s8_verify") == StageStatus.SUCCESS
    )
    failed = sum(
        1 for r in records
        if any(
            state.get_stage_status(r.serial_number, run_id, s) == StageStatus.FAILED
            for s in STAGE_NAMES
        )
    )
    skipped = len(records) - done - failed

    table.add_row("[green]Done[/green]", str(done))
    table.add_row("[red]Failed[/red]", str(failed))
    table.add_row("[yellow]Partial/Skipped[/yellow]", str(skipped))
    console.print(table)
    for report_format, report_path in report_paths.items():
        console.print(
            f"\n{report_format.upper()} report: [cyan]{report_path}[/cyan]"
        )
    console.print(f"State DB: [cyan]{db_path}[/cyan]")
    console.print(
        "\nTo resume failed devices: "
        f"[bold]python run_pipeline.py --input {args.input} --resume {run_id}[/bold]\n"
    )


if __name__ == "__main__":
    main()
