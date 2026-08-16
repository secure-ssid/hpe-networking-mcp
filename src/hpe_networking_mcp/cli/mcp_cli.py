"""argparse entry point for the standalone hpe-networking-mcp client.

Usage examples:

  hpe-mcp tools list
  hpe-mcp tools find "wireless client"
  hpe-mcp rag ask "How do I configure WPA3?" --source mist_docs
  hpe-mcp api lookup "create WLAN"
  hpe-mcp invoke-read ask_docs --args '{"question":"..."}'
  hpe-mcp skills list
  hpe-mcp docs add ./notes.md --collection personal
  hpe-mcp shell
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
from typing import Sequence

from hpe_networking_mcp.cli_client.banner import print_banner
from hpe_networking_mcp.cli_client.commands import (
    cmd_ai_reason,
    cmd_api_lookup,
    cmd_architect_plan,
    cmd_diagram,
    cmd_docs_add,
    cmd_docs_ingest,
    cmd_docs_list,
    cmd_docs_remove,
    cmd_docs_search,
    cmd_docs_search_content,
    cmd_docs_search_internal,
    cmd_find_tool_server,
    cmd_invoke,
    cmd_migrate_plan,
    cmd_rag_ask,
    cmd_skills_list,
    cmd_skills_show,
    cmd_status,
    cmd_tool_explore,
    cmd_tools_list,
    cmd_troubleshoot,
    ensure_connected,
    parse_args_json,
)
from hpe_networking_mcp.cli_client.config import load_client_config
from hpe_networking_mcp.cli_client.output import console, print_error
from hpe_networking_mcp.cli_client.repl_input import configure_readline, read_line
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hpe-mcp",
        description=(
            "Standalone client for hpe-networking-mcp (and other MCP servers). "
            "Run with no arguments to open the interactive shell."
        ),
    )
    p.add_argument(
        "--profile",
        "-p",
        default=None,
        help="Named server profile (default: local-router)",
    )
    p.add_argument("--config", default=None, help="Path to client config JSON")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress banner")
    p.add_argument(
        "--repl",
        action="store_true",
        help="Use plain readline shell instead of the Textual TUI",
    )
    p.add_argument(
        "--tui",
        action="store_true",
        help="Force Textual TUI (default for interactive shell)",
    )
    p.add_argument(
        "--allow-writes",
        action="store_true",
        help="Permit write-capable tools (still requires --yes unless interactive)",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm write operations non-interactively",
    )

    # Not required: bare `hpe-mcp` opens the interactive shell.
    sub = p.add_subparsers(dest="command", required=False)

    # tools
    tools = sub.add_parser("tools", help="List or search tools")
    tools_sub = tools.add_subparsers(dest="tools_cmd", required=True)
    t_list = tools_sub.add_parser("list", help="List connected tools")
    t_list.add_argument("query", nargs="?", default=None)
    t_find = tools_sub.add_parser("find", help="Find tools (router find_tool or local filter)")
    t_find.add_argument("query")

    # invoke / invoke-read
    inv = sub.add_parser("invoke", help="Call a tool (respects write policy)")
    inv.add_argument("tool")
    inv.add_argument("--args", default="{}", help="JSON object of arguments")

    invr = sub.add_parser("invoke-read", help="Call a tool; refuse if not read-only")
    invr.add_argument("tool")
    invr.add_argument("--args", default="{}", help="JSON object of arguments")

    # rag
    rag = sub.add_parser("rag", help="RAG helpers")
    rag_sub = rag.add_subparsers(dest="rag_cmd", required=True)
    rag_ask = rag_sub.add_parser("ask", help="ask_docs")
    rag_ask.add_argument("question", nargs="+")
    rag_ask.add_argument("--source", default=None)

    # api
    api = sub.add_parser("api", help="API catalog helpers")
    api_sub = api.add_subparsers(dest="api_cmd", required=True)
    api_lookup = api_sub.add_parser("lookup", help="lookup_api")
    api_lookup.add_argument("query", nargs="+")

    # skills
    skills = sub.add_parser("skills", help="Local SKILL.md workflows")
    skills_sub = skills.add_subparsers(dest="skills_cmd", required=True)
    skills_sub.add_parser("list")
    sk_show = skills_sub.add_parser("show")
    sk_show.add_argument("name")

    # docs
    docs = sub.add_parser("docs", help="Personal document collections")
    docs_sub = docs.add_subparsers(dest="docs_cmd", required=True)
    d_list = docs_sub.add_parser("list")
    d_list.add_argument("--collection", default=None)
    d_add = docs_sub.add_parser("add")
    d_add.add_argument("source")
    d_add.add_argument("--collection", default="personal")
    d_add.add_argument("--title", default=None)
    d_remove = docs_sub.add_parser("remove", help="Remove a stored document by id")
    d_remove.add_argument("doc_id")
    d_remove.add_argument(
        "--keep-file",
        action="store_true",
        help="Drop the index entry but keep the stored file on disk",
    )
    d_search = docs_sub.add_parser("search")
    d_search.add_argument("query", nargs="+")
    d_search.add_argument("--collection", default=None)
    d_content = docs_sub.add_parser("search-content")
    d_content.add_argument("query", nargs="+")
    d_content.add_argument("--collection", default=None)
    d_ingest = docs_sub.add_parser(
        "ingest", help="Extract/chunk/embed a folder into the local personal index"
    )
    d_ingest.add_argument("folder")
    d_ingest.add_argument("--collection", default="internal")
    d_search_internal = docs_sub.add_parser(
        "search-internal", help="Hybrid search over the local personal index"
    )
    d_search_internal.add_argument("query", nargs="+")
    d_search_internal.add_argument("--collection", default="internal")

    # diagram
    diag = sub.add_parser("diagram", help="Generate network design diagrams (Draw.io/Graphviz/NeXt)")
    diag.add_argument("prompt", nargs="*", default=[], help="Diagram description or title")
    diag.add_argument("--format", "-f", choices=["drawio", "graphviz", "nextui"], default=None)
    diag.add_argument("--vendor", "-v", default=None)
    diag.add_argument("--title", "-t", default=None)

    # ai / reason
    ai_p = sub.add_parser("ai", help="AI multi-turn reasoning and tool dispatch")
    ai_p.add_argument("prompt", nargs="+", help="Goal or prompt for AI expert")
    ai_p.add_argument("--provider", choices=["heuristic", "openai", "anthropic", "ollama"], default="heuristic")
    ai_p.add_argument("--model", default=None)

    # troubleshoot
    tb_p = sub.add_parser("troubleshoot", help="Automated root cause diagnostic reasoning")
    tb_p.add_argument("query", nargs="+", help="Symptom, client MAC, or error description")
    tb_p.add_argument("--site-id", default=None)

    # migrate
    mg_p = sub.add_parser("migrate", help="Multi-vendor migration blueprint & syntax translation")
    mg_p.add_argument("source_vendor", nargs="?", default="aos-s", help="Source vendor (cisco, aos-s, procurve)")

    # architect / design
    ar_p = sub.add_parser("architect", help="Campus / DC Fabric architecture design & BOM synthesis")
    ar_p.add_argument("environment", nargs="?", default="campus", choices=["campus", "datacenter", "branch", "fabric"])
    ar_p.add_argument("--ports", type=int, default=200)
    ar_p.add_argument("--aps", type=int, default=50)
    ar_p.add_argument("--evpn", action="store_true")

    # tool / explore
    tool_p = sub.add_parser("tool", help="Inspect tool schema and parameters")
    tool_p.add_argument("tool_name", help="Name of tool to inspect")

    # status
    sub.add_parser("status", help="Show connection status and profile details")

    # profiles
    prof = sub.add_parser("profiles", help="Show configured server profiles")
    prof.add_argument("profiles_cmd", nargs="?", default="list", choices=["list"])

    # TUI / shell
    sub.add_parser("shell", help="Interactive Textual TUI (or --repl fallback)")
    sub.add_parser("version", help="Print version and exit")

    return p


def _safety_from_args(args: argparse.Namespace) -> SafetyPolicy:
    return SafetyPolicy(
        read_only_default=not bool(getattr(args, "allow_writes", False)),
        allow_writes=bool(getattr(args, "allow_writes", False)),
        confirmed=bool(getattr(args, "yes", False)),
    )


async def _run_connected(args: argparse.Namespace) -> int:
    cfg = load_client_config(config_path=args.config, profile_override=args.profile)
    safety = _safety_from_args(args)
    json_mode = bool(args.json)

    if args.command == "version":
        from hpe_networking_mcp.cli_client.banner import package_version

        print(package_version())
        return 0

    if args.command == "profiles":
        rows = []
        for name, prof in sorted(cfg.profiles.items()):
            rows.append(
                {
                    "name": name,
                    "transport": prof.transport,
                    "command": prof.command,
                    "url": prof.url,
                    "description": prof.description,
                    "default": name == cfg.default_profile,
                }
            )
        if json_mode:
            from hpe_networking_mcp.cli_client.output import print_ok

            print_ok(
                {
                    "profiles": rows,
                    "config_path": (
                        str(cfg.config_path) if cfg.config_path else None
                    ),
                },
                json_mode=True,
            )
        else:
            from rich.table import Table

            table = Table(title="Server profiles")
            table.add_column("Name", style="cyan")
            table.add_column("Transport")
            table.add_column("Target")
            table.add_column("Default")
            table.add_column("Description")
            for r in rows:
                target = r["url"] or " ".join(filter(None, [r["command"]]))
                table.add_row(
                    r["name"],
                    r["transport"],
                    str(target)[:60],
                    "yes" if r["default"] else "",
                    r["description"][:50],
                )
            console.print(table)
        return 0

    # Offline commands (no MCP session)
    if args.command == "skills":
        if args.skills_cmd == "list":
            return cmd_skills_list(json_mode=json_mode)
        return cmd_skills_show(args.name, json_mode=json_mode)

    if args.command == "status":
        return cmd_status(None, cfg, current_profile=args.profile, json_mode=json_mode)

    if args.command == "troubleshoot":
        return cmd_troubleshoot(" ".join(args.query), site_id=args.site_id, json_mode=json_mode)

    if args.command == "migrate":
        return cmd_migrate_plan(args.source_vendor, json_mode=json_mode)

    if args.command == "architect":
        return cmd_architect_plan(
            environment=args.environment,
            ports=args.ports,
            aps=args.aps,
            evpn=args.evpn,
            json_mode=json_mode,
        )

    if args.command == "docs":
        if args.docs_cmd == "list":
            return cmd_docs_list(collection=args.collection, json_mode=json_mode)
        if args.docs_cmd == "add":
            return cmd_docs_add(
                args.source,
                collection=args.collection,
                title=args.title,
                json_mode=json_mode,
            )
        if args.docs_cmd == "remove":
            return cmd_docs_remove(
                args.doc_id,
                keep_file=args.keep_file,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search":
            return cmd_docs_search(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search-content":
            return cmd_docs_search_content(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "ingest":
            return cmd_docs_ingest(
                args.folder,
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search-internal":
            return cmd_docs_search_internal(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )

    if args.command == "shell":
        # TUI is started from main() on the main thread. This path is the
        # plain readline fallback (--repl) only.
        return await run_repl(cfg, safety, quiet=args.quiet, json_default=json_mode)

    # Connected commands
    print_banner(
        console,
        profile=args.profile or cfg.default_profile,
        mode=args.command,
        quiet=args.quiet or json_mode,
    )

    async with SessionManager.create(namespace=True) as mgr:
        try:
            prof = await ensure_connected(mgr, cfg, profile=args.profile)
        except Exception as exc:  # noqa: BLE001
            print_error(f"connect failed: {exc}", json_mode=json_mode, code="connect_failed")
            return 1

        if not json_mode and not args.quiet:
            console.print(
                "[dim]connected[/] "
                f"profile={prof.name} transport={prof.transport} "
                f"tools={len(mgr.tools)}"
            )

        if args.command == "diagram":
            return await cmd_diagram(
                mgr,
                safety,
                prompt=" ".join(args.prompt),
                format=args.format,
                vendor=args.vendor,
                title=args.title,
                json_mode=json_mode,
            )

        if args.command == "ai":
            return await cmd_ai_reason(
                mgr,
                safety,
                prompt=" ".join(args.prompt),
                provider=args.provider,
                model=args.model,
                json_mode=json_mode,
            )

        if args.command == "tool":
            return await cmd_tool_explore(
                mgr,
                safety,
                tool_name=args.tool_name,
                json_mode=json_mode,
            )

        if args.command == "tools":
            if args.tools_cmd == "list":
                return await cmd_tools_list(mgr, query=args.query, json_mode=json_mode)
            return await cmd_find_tool_server(mgr, safety, query=args.query, json_mode=json_mode)

        if args.command == "invoke":
            try:
                tool_args = parse_args_json(args.args)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print_error(f"bad --args: {exc}", json_mode=json_mode)
                return 2
            return await cmd_invoke(
                mgr,
                safety,
                tool=args.tool,
                args=tool_args,
                allow_writes=args.allow_writes,
                force_write=args.allow_writes and args.yes,
                json_mode=json_mode,
            )

        if args.command == "invoke-read":
            # Temporarily force read-only policy
            ro_safety = SafetyPolicy(read_only_default=True, allow_writes=False, confirmed=False)
            try:
                tool_args = parse_args_json(args.args)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print_error(f"bad --args: {exc}", json_mode=json_mode)
                return 2
            return await cmd_invoke(
                mgr,
                ro_safety,
                tool=args.tool,
                args=tool_args,
                json_mode=json_mode,
            )

        if args.command == "rag" and args.rag_cmd == "ask":
            return await cmd_rag_ask(
                mgr,
                safety,
                question=" ".join(args.question),
                source=args.source,
                json_mode=json_mode,
            )

        if args.command == "api" and args.api_cmd == "lookup":
            return await cmd_api_lookup(
                mgr,
                safety,
                query=" ".join(args.query),
                json_mode=json_mode,
            )

        print_error(f"unhandled command {args.command}", json_mode=json_mode)
        return 2


async def run_repl(
    cfg,
    safety: SafetyPolicy,
    *,
    quiet: bool = False,
    json_default: bool = False,
) -> int:
    """Minimal interactive shell over a live session."""
    # Load readline/libedit BEFORE first prompt so ↑/↓ history works.
    has_hist = configure_readline()
    print_banner(
        console,
        profile=cfg.default_profile,
        mode="shell",
        quiet=quiet,
    )
    console.print(
        "[dim]shortcuts:[/] ask <q> · api <q> · find <q> · tools · help · exit\n"
        "[dim]full:[/] rag ask · tools list|find · api lookup · invoke-read · "
        "skills · docs · profiles · connect"
    )
    if has_hist:
        console.print("[dim]history:[/] ↑/↓ recall · Ctrl-R search (libedit/GNU) · Ctrl-C cancel")
    else:
        console.print("[dim yellow]history unavailable (no readline)[/]")

    async with SessionManager.create(namespace=True) as mgr:
        try:
            prof = await ensure_connected(mgr, cfg)
            console.print(
                f"[green]connected[/] {prof.name} ({prof.transport}) · {len(mgr.tools)} tools"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]not connected yet:[/] {exc}")
            console.print("[dim]use: connect [profile][/]")

        while True:
            try:
                line = await asyncio.to_thread(read_line, "hpe-mcp> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            line = line.strip()
            if not line:
                continue
            if line in {"exit", "quit", ":q"}:
                break
            if line in {"help", "?"}:
                console.print(
                    "[bold]shortcuts[/]\n"
                    "  ask <question> [--source mist_docs]   RAG / docs Q&A\n"
                    "  api <query>                           OpenAPI lookup\n"
                    "  find <query>                          find tools\n"
                    "  tools                                 list tools\n"
                    "[bold]also[/]\n"
                    "  rag ask | tools list|find | api lookup | invoke-read\n"
                    "  skills list|show | docs list|add|remove|search\n"
                    "  connect [profile] | profiles | exit"
                )
                continue

            try:
                argv = shlex.split(line)
            except ValueError as exc:
                print_error(str(exc))
                continue

            head = argv[0].lstrip("/")
            if not head:
                continue
            json_mode = json_default or "--json" in argv
            argv = [a for a in argv if a != "--json"]

            try:
                if head in {"exit", "quit", ":q", "q"} and len(argv) == 1:
                    break
                if head in {"help", "?"} and len(argv) == 1:
                    console.print(
                        "[bold]shortcuts[/]\n"
                        "  ask <question> [--source mist_docs]   RAG / docs Q&A\n"
                        "  diagram <description>                 Network design diagram\n"
                        "  tool <name>                           Inspect tool schema\n"
                        "  api <query>                           OpenAPI lookup\n"
                        "  find <query>                          find tools\n"
                        "  tools [list|find]                     list tools\n"
                        "[bold]also[/]\n"
                        "  rag ask | tools list|find | api lookup | invoke-read\n"
                        "  skills [list|show] | docs [list|add|remove|search]\n"
                        "  status | connect [profile] | profiles | exit\n"
                        "[dim]Note: Any plain question automatically queries RAG docs.[/]"
                    )
                    continue
                if head == "connect":
                    name = argv[1] if len(argv) > 1 else None
                    prof = await ensure_connected(mgr, cfg, profile=name)
                    console.print(f"[green]connected[/] {prof.name} · {len(mgr.tools)} tools")
                    continue
                if head == "profiles":
                    for n, p in sorted(cfg.profiles.items()):
                        mark = "*" if n == cfg.default_profile else " "
                        console.print(f"{mark} {n:16} {p.transport:16} {p.description}")
                    continue
                if head == "status":
                    cmd_status(mgr, cfg, current_profile=None, json_mode=json_mode)
                    continue
                if head == "diagram":
                    await cmd_diagram(
                        mgr,
                        safety,
                        prompt=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head in {"ai", "reason"}:
                    await cmd_ai_reason(
                        mgr,
                        safety,
                        prompt=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head in {"troubleshoot", "tb"}:
                    cmd_troubleshoot(" ".join(argv[1:]), json_mode=json_mode)
                    continue
                if head in {"migrate", "migration"}:
                    vendor = argv[1] if len(argv) > 1 else "aos-s"
                    cmd_migrate_plan(vendor, json_mode=json_mode)
                    continue
                if head in {"architect", "design"}:
                    env = argv[1] if len(argv) > 1 else "campus"
                    cmd_architect_plan(env, json_mode=json_mode)
                    continue
                if head in {"tool", "explore"} and len(argv) >= 2:
                    await cmd_tool_explore(
                        mgr,
                        safety,
                        tool_name=argv[1],
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and len(argv) >= 2 and argv[1] == "list":
                    await cmd_tools_list(
                        mgr,
                        query=argv[2] if len(argv) > 2 else None,
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and len(argv) >= 3 and argv[1] == "find":
                    await cmd_find_tool_server(
                        mgr,
                        safety,
                        query=" ".join(argv[2:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "rag" and len(argv) >= 3 and argv[1] == "ask":
                    source = None
                    rest = argv[2:]
                    if "--source" in rest:
                        i = rest.index("--source")
                        source = rest[i + 1]
                        rest = rest[:i] + rest[i + 2 :]
                    await cmd_rag_ask(
                        mgr,
                        safety,
                        question=" ".join(rest),
                        source=source,
                        json_mode=json_mode,
                    )
                    continue
                if head == "api" and len(argv) >= 3 and argv[1] == "lookup":
                    await cmd_api_lookup(
                        mgr,
                        safety,
                        query=" ".join(argv[2:]),
                        json_mode=json_mode,
                    )
                    continue
                # Short aliases for common actions
                if head in {"ask", "search_docs"} and len(argv) >= 2:
                    source = None
                    rest = argv[1:]
                    if "--source" in rest:
                        i = rest.index("--source")
                        source = rest[i + 1]
                        rest = rest[:i] + rest[i + 2 :]
                    await cmd_rag_ask(
                        mgr,
                        safety,
                        question=" ".join(rest),
                        source=source,
                        json_mode=json_mode,
                    )
                    continue
                if head == "api" and len(argv) >= 2 and argv[1] != "lookup":
                    await cmd_api_lookup(
                        mgr,
                        safety,
                        query=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "find" and len(argv) >= 2:
                    await cmd_find_tool_server(
                        mgr,
                        safety,
                        query=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and (len(argv) == 1 or (len(argv) == 2 and argv[1] in {"list"})):
                    await cmd_tools_list(mgr, json_mode=json_mode)
                    continue
                if head == "invoke-read" and len(argv) >= 2:
                    raw_args = argv[2] if len(argv) > 2 else "{}"
                    await cmd_invoke(
                        mgr,
                        SafetyPolicy(read_only_default=True),
                        tool=argv[1],
                        args=parse_args_json(raw_args),
                        json_mode=json_mode,
                    )
                    continue
                if head == "skills":
                    if len(argv) == 1 or (len(argv) >= 2 and argv[1] == "list"):
                        cmd_skills_list(json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "show":
                        cmd_skills_show(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) == 2:
                        cmd_skills_show(argv[1], json_mode=json_mode)
                        continue
                if head == "docs":
                    if len(argv) == 1 or (len(argv) >= 2 and argv[1] == "list"):
                        coll = argv[2] if len(argv) > 2 else None
                        cmd_docs_list(collection=coll, json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "add":
                        cmd_docs_add(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "remove":
                        cmd_docs_remove(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "search":
                        cmd_docs_search(" ".join(argv[2:]), json_mode=json_mode)
                        continue
                    if len(argv) >= 2:
                        cmd_docs_search(" ".join(argv[1:]), json_mode=json_mode)
                        continue
                # If the user typed an arbitrary networking question, fall back to RAG
                await cmd_rag_ask(
                    mgr,
                    safety,
                    question=line,
                    json_mode=json_mode,
                )
            except Exception as exc:  # noqa: BLE001
                print_error(f"{type(exc).__name__}: {exc}", json_mode=json_mode)

    return 0


_COMMANDS = frozenset(
    {
        "tools",
        "tool",
        "diagram",
        "ai",
        "troubleshoot",
        "migrate",
        "architect",
        "status",
        "invoke",
        "invoke-read",
        "rag",
        "api",
        "skills",
        "docs",
        "profiles",
        "shell",
        "version",
    }
)


def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    """Default bare / flag-only invocations to the interactive shell."""
    import sys

    raw = list(argv) if argv is not None else sys.argv[1:]
    if any(a in {"-h", "--help"} for a in raw):
        return raw
    # Already has a subcommand somewhere after optional global flags.
    if any(a in _COMMANDS for a in raw):
        return raw
    # No args, or only global flags → shell.
    return [*raw, "shell"]


def main(argv: Sequence[str] | None = None) -> int:
    raw = _normalize_argv(argv)
    parser = build_parser()
    args = parser.parse_args(raw)
    if not getattr(args, "command", None):
        args.command = "shell"

    # Textual TUI must own the main thread / event loop.
    if args.command == "shell" and not getattr(args, "repl", False):
        cfg = load_client_config(
            config_path=args.config, profile_override=args.profile
        )
        safety = _safety_from_args(args)
        try:
            from hpe_networking_mcp.cli_client.tui import run_tui
        except ImportError:
            console.print(
                "[yellow]textual not installed — falling back to readline shell[/]"
            )
            try:
                return asyncio.run(
                    run_repl(cfg, safety, quiet=args.quiet, json_default=args.json)
                )
            except KeyboardInterrupt:
                return 130
        try:
            return run_tui(cfg, safety, profile=args.profile)
        except KeyboardInterrupt:
            return 130

    try:
        return asyncio.run(_run_connected(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
