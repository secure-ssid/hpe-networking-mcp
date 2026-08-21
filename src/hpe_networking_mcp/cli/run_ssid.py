"""Underlay SSID builder — CLI entry point.

Builds one or more underlay SSIDs in Aruba New Central by:
  1. Discovering the org-level global scope-id (or using a supplied scope-id).
  2. POSTing the SSID to /network-config/v1/wlan-ssids/{essid_name}.
  3. Scope-mapping the SSID to the CAMPUS_AP persona at the chosen scope.

Usage examples:

  # Single SSID — global scope, VLAN 1000, open auth:
  python run_ssid.py --ssid "Corp-WiFi" --vlans 1000

  # Multiple VLANs, WPA3-SAE:
  python run_ssid.py --ssid "Secure-Corp" --vlans 1000,1001 --opmode WPA3_SAE

  # Target a specific device-group scope instead of global:
  python run_ssid.py --ssid "Lobby-Guest" --vlans 200 --scope-id 79244358948933632

  # Dry-run (no writes):
  python run_ssid.py --ssid "Test-SSID" --vlans 500 --dry-run

  # Batch from CSV (columns: ssid_name, vlans, [scope_id], [opmode]):
  python run_ssid.py --input ssids.csv

CSV format (header required, scope_id and opmode columns are optional):
  ssid_name,vlans,scope_id,opmode
  Corp-WiFi,1000,,
  Guest-WiFi,200,79244358948933632,ENHANCED_OPEN

Options:
  --creds FILE        Credentials YAML (default: config/credentials.yaml)
  --ssid NAME         SSID name (single-SSID mode)
  --vlans IDS         Comma-separated VLAN IDs, e.g. 1000 or 1000,1001
  --scope-id ID       Scope-id to map SSID to (default: auto-discovered global)
  --opmode MODE       ENHANCED_OPEN | WPA3_SAE | WPA2_PERSONAL (default: ENHANCED_OPEN)
                      WPA2_PSK is accepted as a deprecated alias for
                      WPA2_PERSONAL and is normalized automatically.
  --rf-band BAND      24GHZ_5GHZ | 24GHZ | 5GHZ | 6GHZ (default: 24GHZ_5GHZ)
  --hide-ssid         Suppress broadcast of SSID name
  --max-clients N     Max clients per AP radio (default: 1024)
  --input FILE        CSV file for batch mode
  --dry-run           Log actions without making any API writes
  --log-level LEVEL   DEBUG | INFO | WARNING (default: INFO)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys

from rich.console import Console
from rich.table import Table

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.create_ssid import build_underlay_ssid
from hpe_networking_mcp.pipeline.stages.s6_configure import _fetch_global_scope_id

console = Console()


def _parse_vlans(vlan_str: str) -> list[str]:
    """Convert '1000' or '1000,1001' → ['1000', '1001']."""
    return [v.strip() for v in vlan_str.split(",") if v.strip()]


def _load_csv(path: str) -> list[dict]:
    """Load SSID definitions from a CSV file.

    Required column: ssid_name, vlans
    Optional columns: scope_id, opmode
    """
    rows = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ssid_name = row.get("ssid_name", "").strip()
            vlans_raw = row.get("vlans", "").strip()
            if not ssid_name or not vlans_raw:
                console.print(f"[yellow]Skipping row with missing ssid_name or vlans: {row}[/yellow]")
                continue
            rows.append({
                "ssid_name": ssid_name,
                "vlans": _parse_vlans(vlans_raw),
                "scope_id": row.get("scope_id", "").strip() or None,
                "opmode": row.get("opmode", "").strip() or "ENHANCED_OPEN",
            })
    return rows


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aruba New Central — Underlay SSID Builder")
    parser.add_argument("--creds", default="config/credentials.yaml", help="Credentials YAML file")
    parser.add_argument("--ssid", metavar="NAME", help="SSID name (single-SSID mode)")
    parser.add_argument("--vlans", metavar="IDS", help="Comma-separated VLAN IDs (e.g. 1000 or 1000,1001)")
    parser.add_argument("--scope-id", metavar="ID", help="Scope-id to map SSID to (default: global)")
    parser.add_argument("--opmode", default="ENHANCED_OPEN",
                        choices=["ENHANCED_OPEN", "WPA3_SAE", "WPA2_PERSONAL", "WPA2_PSK"],
                        help="Security/auth mode (default: ENHANCED_OPEN). "
                             "WPA2_PSK is a deprecated alias for WPA2_PERSONAL.")
    parser.add_argument("--rf-band", default="24GHZ_5GHZ",
                        choices=["24GHZ_5GHZ", "24GHZ", "5GHZ", "6GHZ"],
                        help="RF band (default: 24GHZ_5GHZ). Values match the Central "
                             "Aruba802dot11_Wlan802dot11.rf-band enum on the wire.")
    parser.add_argument("--passphrase", metavar="KEY",
                        help="WPA pre-shared key (required for WPA3_SAE or WPA2_PERSONAL)")
    parser.add_argument("--hide-ssid", action="store_true", help="Suppress SSID broadcast")
    parser.add_argument("--max-clients", type=int, default=1024, metavar="N",
                        help="Max clients per AP radio (default: 1024)")
    parser.add_argument("--input", metavar="FILE", help="CSV file for batch mode")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without writing to API")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate: must have either --ssid+--vlans or --input
    if not args.input and not (args.ssid and args.vlans):
        parser.error("Provide either --ssid + --vlans, or --input FILE")

    # Build SSID definitions list
    if args.input:
        try:
            ssid_defs = _load_csv(args.input)
        except (FileNotFoundError, KeyError) as exc:
            console.print(f"[red]Error loading CSV:[/red] {exc}")
            sys.exit(1)
    else:
        ssid_defs = [
            {
                "ssid_name": args.ssid,
                "vlans": _parse_vlans(args.vlans),
                "scope_id": args.scope_id,
                "opmode": args.opmode,
            }
        ]

    # WPA2_PSK is a deprecated alias for WPA2_PERSONAL (see
    # hpe_networking_mcp.pipeline.create_ssid.DEPRECATED_OPMODE_ALIASES); warn once up front so
    # 0.4.0 scripts/CSVs that still pass it keep working but are visibly
    # nudged to update.
    if any(defn["opmode"] == "WPA2_PSK" for defn in ssid_defs):
        console.print(
            "[yellow]Warning:[/yellow] --opmode WPA2_PSK is deprecated — "
            "use WPA2_PERSONAL instead. Normalizing automatically for this run."
        )

    # Build credentials + client
    try:
        _, target_ctx = build_account_contexts(args.creds)
    except Exception as exc:
        console.print(f"[red]Error loading credentials:[/red] {exc}")
        sys.exit(1)

    tm = TokenManager(
        client_id=target_ctx.client_id,
        client_secret=target_ctx.client_secret,
        cache_key="target",
    )
    central = CentralClient(base_url=target_ctx.base_url, token_manager=tm)

    # Discover global scope-id once (used as fallback when no per-SSID scope_id is given)
    global_scope_id: str | None = None
    if not all(d["scope_id"] for d in ssid_defs):
        if args.dry_run:
            global_scope_id = "DRY_RUN_SCOPE"
            console.print("[yellow][dry-run] Using placeholder global scope-id[/yellow]")
        else:
            try:
                global_scope_id = _fetch_global_scope_id(central)
                console.print(f"Discovered global scope-id: [cyan]{global_scope_id}[/cyan]")
            except Exception as exc:
                console.print(f"[red]Failed to discover global scope-id:[/red] {exc}")
                sys.exit(1)

    mode = "dry-run" if args.dry_run else "live"
    console.print(
        f"\n[bold]Underlay SSID Builder[/bold] — mode=[cyan]{mode}[/cyan]  "
        f"SSIDs: {len(ssid_defs)}\n"
    )

    results = []
    for defn in ssid_defs:
        scope_id = defn["scope_id"] or global_scope_id
        console.print(
            f"  [bold]{defn['ssid_name']}[/bold] "
            f"vlans={defn['vlans']}  opmode={defn['opmode']}  scope={scope_id}"
        )
        result = build_underlay_ssid(
            central,
            ssid_name=defn["ssid_name"],
            vlan_ids=defn["vlans"],
            scope_id=scope_id,
            opmode=defn["opmode"],
            rf_band=args.rf_band,
            hide_ssid=args.hide_ssid,
            max_clients=args.max_clients,
            wpa_passphrase=args.passphrase,
            dry_run=args.dry_run,
        )
        results.append(result)

        if result["errors"]:
            for err in result["errors"]:
                console.print(f"    [red]✗[/red] {err}")
        else:
            console.print(
                f"    [green]✓[/green] "
                f"created={result['created']}  scope_mapped={result['scope_mapped']}"
            )

    # Summary table
    table = Table(title="SSID Build Summary")
    table.add_column("SSID")
    table.add_column("VLANs")
    table.add_column("Scope-ID")
    table.add_column("Created", justify="center")
    table.add_column("Scope-Mapped", justify="center")
    table.add_column("Errors")

    for r in results:
        created = "[green]✓[/green]" if r["created"] else "[red]✗[/red]"
        mapped = "[green]✓[/green]" if r["scope_mapped"] else "[red]✗[/red]"
        errors = "; ".join(r["errors"]) if r["errors"] else ""
        table.add_row(
            r["ssid_name"],
            ", ".join(r["vlan_ids"]),
            r["scope_id"],
            created,
            mapped,
            errors,
        )

    console.print(table)

    failed = sum(1 for r in results if r["errors"])
    if failed:
        console.print(f"\n[red]{failed} SSID(s) had errors.[/red]")
        sys.exit(1)
    else:
        console.print(f"\n[green]All {len(results)} SSID(s) built successfully.[/green]")


if __name__ == "__main__":
    main()
