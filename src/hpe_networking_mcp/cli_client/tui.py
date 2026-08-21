"""Textual TUI shell for hpe-mcp.

Layout:
  ┌ header / status ─────────────────────────────────────────┐
  │ scrollable answer log                                      │
  │                                                            │
  ├ shortcuts ─┬ command input ────────────────────────────────┤
  └ footer (key bindings) ─────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Header, Input, RichLog, Static
from textual.worker import Worker

from hpe_networking_mcp.cli_client.banner import package_version
from hpe_networking_mcp.cli_client.commands import (
    cmd_tools_list,
    create_reasoning_service,
    ensure_connected,
    parse_args_json,
)
from hpe_networking_mcp.cli_client.config import ClientConfig
from hpe_networking_mcp.cli_client.diagram_workflow import (
    execute_diagram_export,
    parse_diagram_intent,
)
from hpe_networking_mcp.cli_client.output import (
    format_duration,
    format_tool_schema,
    format_usage,
    redact_sensitive_text,
    tool_result_to_text,
)
from hpe_networking_mcp.cli_client.repl_input import history_path
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager

COMMAND_SUGGESTIONS = (
    "/help",
    "/examples",
    "/diagram ",
    "/tool ",
    "/ai ",
    "/troubleshoot ",
    "/migrate ",
    "/architect ",
    "/api ",
    "/find ",
    "/tools",
    "/docs list",
    "/docs add ",
    "/docs remove ",
    "/docs search ",
    "/docs search-content ",
    "/skills list",
    "/skills show ",
    "/profiles",
    "/profile ",
    "/products",
    "/usage",
    "/status",
    "/connect",
    "/clear",
    "/quit",
)

_LEGACY_PREFIX_COMMANDS = frozenset(
    {
        "ask",
        "chat",
        "api",
        "find",
        "rag",
        "invoke-read",
        "connect",
        "profile",
        "products",
        "product",
        "toolsets",
        "toolset",
        "usage",
        "diagram",
        "tool",
        "explore",
        "ai",
        "reason",
        "troubleshoot",
        "tb",
        "migrate",
        "migration",
        "architect",
        "design",
    }
)
_LEGACY_EXACT_COMMANDS = frozenset(
    {"profiles", "status", "examples", "clear", "cls", "help", "?", "exit", "quit", ":q"}
)
_SLASH_ALIASES = {
    "?": "help",
    "h": "help",
    "q": "exit",
    "quit": "exit",
}
_SENSITIVE_HISTORY_RE = re.compile(
    r"(?i)(?:--(?:password|passphrase|token|secret|api[-_]?key)\b|"
    r"(?:password|passphrase|token|secret|client_secret|api[-_]?key)\s*[:=]\s*\S+)"
)


def normalize_tui_input(line: str) -> list[str]:
    """Map user input to a command.

    Plain text is a networking question. Expert controls use slash commands.
    Diagram requests ("draw a diagram", "topology map") route to diagram wizard.
    Old bare commands remain compatible while people transition.
    """
    value = line.strip()
    if not value:
        return []
    if value == "/":
        return ["help"]
    if value.startswith("/"):
        argv = shlex.split(value[1:])
        if not argv:
            return ["help"]
        argv[0] = _SLASH_ALIASES.get(argv[0].lower(), argv[0].lower())
        return argv

    # Natural language diagram routing
    low = value.lower()
    if (
        low.startswith("diagram")
        or low.startswith("draw diagram")
        or low.startswith("draw a diagram")
        or low.startswith("build diagram")
        or low.startswith("build me a diagram")
        or "draw a network diagram" in low
        or "export network diagram" in low
        or "create network topology diagram" in low
    ):
        return ["diagram", value]

    argv = shlex.split(value)
    if not argv:
        return []
    head = argv[0].lower()
    if head in _LEGACY_PREFIX_COMMANDS:
        argv[0] = head
        return argv
    if head in _LEGACY_EXACT_COMMANDS and len(argv) == 1:
        argv[0] = head
        return argv
    if head == "tools" and (len(argv) == 1 or argv[1] in {"list", "find"}):
        argv[0] = head
        return argv
    _docs_subcmds = {"list", "add", "remove", "search", "search-content"}
    if head == "docs" and len(argv) >= 2 and argv[1] in _docs_subcmds:
        argv[0] = head
        return argv
    if head == "skills" and len(argv) >= 2 and argv[1] in {"list", "show"}:
        argv[0] = head
        return argv
    return ["ask", value]


def history_line_is_safe(line: str) -> bool:
    """Do not persist commands that appear to contain credentials."""
    return not _SENSITIVE_HISTORY_RE.search(line)


HELP_TEXT = """\
# hpe-mcp

Just type a networking question and press **Enter**.

Use `/` commands for expert controls:

| Command | What it does |
|---|---|
| `/diagram <query>` | Generate Draw.io/Graphviz/NeXt design diagrams |
| `/tool <name>` | Inspect tool parameter schema and options |
| `/api <query>` | Find exact API endpoints and schemas |
| `/find <query>` | Search thousands of backend operations |
| `/tools` | Show the small router/discovery layer |
| `/docs list\\|add\\|remove\\|search` | Manage personal documents (`--collection name` to scope) |
| `/skills list\\|show` | Show guided workflows |
| `/profiles` | Show available MCP connections |
| `/profile [name]` | View or switch active connection profile |
| `/products` | View enabled product/toolset configuration |
| `/usage` | View cumulative AI token usage |
| `/status` | View connection state, profile, and tools |
| `/connect [profile]` | (Re)connect MCP |
| `/examples` | Show useful starter questions |
| `/clear` | Clear the results pane |
| `/quit` | Leave the TUI |

**Keys:** `↑`/`↓` history · `Ctrl+L` clear · `Ctrl+C` cancel · `Ctrl+Q` quit

Legacy commands such as `ask`, `api`, `find`, and `diagram` still work.
"""

EXAMPLES_TEXT = """\
# Try one of these

- `What is the best way to design a three-tier campus network?`
- `How should I design redundant VSX aggregation?`
- `/diagram three-tier campus network with VSX agg and Aruba APs`
- `/tool list_devices`
- `/api create a Mist WLAN`
- `/find troubleshoot wireless client`
- `/docs add ./my-network-notes.md`
- `/docs search-content EX4400`
- `/docs remove <id>`

Personal documents are stored locally and searchable across metadata and text.
"""

CSS = """
Screen {
    layout: vertical;
}

#status {
    height: 3;
    padding: 0 1;
    background: $surface;
    color: $text;
    border: solid $success;
}

#main {
    height: 1fr;
}

#log {
    height: 1fr;
    border: solid $primary;
    background: $background;
    padding: 0 1;
}

#side {
    width: 32;
    min-width: 24;
    max-width: 38;
    border: solid $accent;
    padding: 0 1;
    background: $surface;
    color: $text-muted;
}

#input-row {
    height: auto;
    dock: bottom;
}

#cmd {
    width: 1fr;
    border: tall $success;
    margin: 0 1 1 1;
}

#busy {
    dock: bottom;
    height: 1;
    padding: 0 1;
    color: $warning;
    display: none;
}

#busy.visible {
    display: block;
}
"""


class StatusBar(Static):
    """Connection / profile status strip."""

    def set_status(
        self,
        *,
        connected: bool,
        profile: str,
        transport: str = "",
        tools: int = 0,
        products: str = "",
        usage: str = "",
        message: str = "",
    ) -> None:
        ver = package_version()
        state = "[green]connected[/]" if connected else "[yellow]disconnected[/]"
        bits = [
            f"[bold cyan]hpe-networking-mcp[/] v{ver}",
            state,
            f"profile={profile}",
        ]
        if transport:
            bits.append(f"transport={transport}")
        if products:
            bits.append(f"products={products}")
        if usage:
            bits.append(f"tokens={usage}")
        if tools:
            bits.append(f"tools={tools}")
        bits.append("[dim]read-only default[/]")
        if message:
            bits.append(message)
        self.update(" · ".join(bits))


class HpeMcpApp(App[int]):
    """Full-screen TUI client."""

    TITLE = "hpe-networking-mcp"
    SUB_TITLE = "standalone MCP client"
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit", show=True),
        Binding("ctrl+c", "cancel", "Cancel", show=True),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("up", "history_prev", "Hist↑", show=False, priority=True),
        Binding("down", "history_next", "Hist↓", show=False, priority=True),
    ]

    def __init__(
        self,
        cfg: ClientConfig,
        safety: SafetyPolicy,
        *,
        profile: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.safety = safety
        self.profile_name = profile or cfg.default_profile
        self.provider = provider or cfg.ai_provider
        self.model = model or cfg.ai_model
        self.mgr: SessionManager | None = None
        self._reasoning_service = None
        self._history: list[str] = []
        self._hist_idx: int = 0
        self._busy = False
        self._busy_message = ""
        self._busy_started: float | None = None
        self._command_worker: Worker[None] | None = None
        self._exit_code = 0
        self._session_usage: dict[str, int] = {}

    def _get_active_products(self) -> str:
        prods = os.environ.get("HPE_MCP_PRODUCTS", "").strip()
        toolsets = os.environ.get("HPE_MCP_TOOLSETS", "").strip()
        parts = []
        if prods:
            parts.append(prods)
        if toolsets:
            parts.append(f"toolsets:{toolsets}")
        return ",".join(parts) if parts else "default"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")
        with Horizontal(id="main"):
            yield RichLog(id="log", highlight=True, markup=True, wrap=True, auto_scroll=True)
            yield Static(
                "[bold cyan]START HERE[/]\n\n"
                "Type a networking\n"
                "question normally.\n\n"
                "[bold]Expert commands[/]\n"
                "/diagram <query>\n"
                "/tool <name>\n"
                "/api <query>\n"
                "/find <query>\n"
                "/tools\n"
                "/docs list|search\n"
                "/skills list\n"
                "/status\n"
                "/examples\n"
                "/help\n"
                "/quit",
                id="side",
            )
        yield Static("", id="busy")
        with Vertical(id="input-row"):
            yield Input(
                placeholder="Ask a networking question…  (type / for commands)",
                suggester=SuggestFromList(COMMAND_SUGGESTIONS, case_sensitive=False),
                id="cmd",
            )
        yield Footer()

    async def on_mount(self) -> None:
        self._load_history()
        self.set_interval(0.25, self._refresh_busy)
        status = self.query_one("#status", StatusBar)
        status.set_status(connected=False, profile=self.profile_name, message="starting…")
        log = self.query_one("#log", RichLog)
        log.write(
            Panel(
                Markdown(
                    "# Ask about your network\n\n"
                    "Type a question normally. Use `/` only when you want a "
                    "diagram, API lookup, tool search, documents, or settings.\n\n"
                    "**Examples:**\n"
                    "- Best way to design a three-tier network\n"
                    "- How do I troubleshoot a roaming client?\n"
                    "- `/diagram three-tier campus with VSX agg`\n"
                    "- `/api create a Mist WLAN`\n"
                    "- `/tool list_devices`\n"
                    "- `/find switch port health`"
                ),
                border_style="green",
                title="Welcome to hpe-networking-mcp",
            )
        )
        self.query_one("#cmd", Input).focus()
        self.connect_session()

    def _load_history(self) -> None:
        path = history_path()
        try:
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and history_line_is_safe(line)
            ]
        except OSError:
            lines = []
        self._history = lines[-500:]
        self._hist_idx = len(self._history)

    def _save_history(self) -> None:
        path = history_path()
        safe_lines = [line for line in self._history[-500:] if history_line_is_safe(line)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(safe_lines) + ("\n" if safe_lines else ""), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _record_history(self, line: str) -> None:
        if not history_line_is_safe(line):
            return
        if not self._history or self._history[-1] != line:
            self._history.append(line)
        self._history = self._history[-500:]
        self._hist_idx = len(self._history)
        self._save_history()

    @work(exclusive=True, group="connection")
    async def connect_session(self) -> None:
        status = self.query_one("#status", StatusBar)
        log = self.query_one("#log", RichLog)
        self._set_busy("connecting…")
        try:
            if self.mgr is not None:
                await self.mgr.__aexit__(None, None, None)
            self.mgr = SessionManager.create(namespace=True)
            await self.mgr.__aenter__()
            prof = await ensure_connected(self.mgr, self.cfg, profile=self.profile_name)
            if self._reasoning_service is not None:
                self._reasoning_service.session_manager = self.mgr
                self._reasoning_service.safety_policy = self.safety
            status.set_status(
                connected=True,
                profile=prof.name,
                transport=prof.transport,
                tools=len(self.mgr.tools),
                products=self._get_active_products(),
                usage=format_usage(self._session_usage) if self._session_usage else "",
            )
            log.write(
                Panel(
                    Markdown(
                        f"**Connected:** `{prof.name}` over `{prof.transport}`\n\n"
                        f"The client sees **{len(self.mgr.tools)} router tools**. "
                        "Those are the discovery/dispatch layer—not the full catalog. "
                        "Use `/find <what you need>` to search backend operations."
                    ),
                    title="Ready",
                    border_style="green",
                )
            )
        except Exception as exc:  # noqa: BLE001
            status.set_status(
                connected=False,
                profile=self.profile_name,
                products=self._get_active_products(),
                usage=format_usage(self._session_usage) if self._session_usage else "",
                message="error",
            )
            log.write(
                Panel(
                    Markdown(
                        f"**Could not connect:** `{exc}`\n\n"
                        "Try `/connect`, check `/profiles`, or run "
                        "`hpe-mcp-doctor` in another terminal."
                    ),
                    title="Connection problem",
                    border_style="red",
                )
            )
        finally:
            self._set_busy(None)

    def _set_busy(self, message: str | None) -> None:
        self._busy = bool(message)
        self._busy_message = message or ""
        self._busy_started = time.monotonic() if message else None
        busy = self.query_one("#busy", Static)
        if message:
            busy.update(f"… {message}")
            busy.add_class("visible")
        else:
            busy.update("")
            busy.remove_class("visible")

    def _refresh_busy(self) -> None:
        if not self._busy or self._busy_started is None:
            return
        elapsed = time.monotonic() - self._busy_started
        self.query_one("#busy", Static).update(
            f"… {self._busy_message}  ·  {elapsed:.1f}s  ·  Ctrl+C cancels"
        )

    def action_quit_app(self) -> None:
        self.exit(self._exit_code)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_show_help(self) -> None:
        self.query_one("#log", RichLog).write(Markdown(HELP_TEXT))

    def action_cancel(self) -> None:
        worker = self._command_worker
        if worker is not None and not worker.is_finished:
            worker.cancel()
            self.query_one("#log", RichLog).write(
                Text.from_markup("[yellow]cancelled the active request[/]")
            )
            self._command_worker = None
            self._set_busy(None)
        else:
            self.query_one("#log", RichLog).write(
                Text.from_markup("[dim]nothing to cancel · Ctrl+Q quits[/]")
            )

    def action_history_prev(self) -> None:
        if not self._history:
            return
        cmd = self.query_one("#cmd", Input)
        # Only hijack up when input is focused
        if not cmd.has_focus:
            return
        self._hist_idx = max(0, self._hist_idx - 1)
        cmd.value = self._history[self._hist_idx]
        cmd.cursor_position = len(cmd.value)

    def action_history_next(self) -> None:
        if not self._history:
            return
        cmd = self.query_one("#cmd", Input)
        if not cmd.has_focus:
            return
        if self._hist_idx >= len(self._history) - 1:
            self._hist_idx = len(self._history)
            cmd.value = ""
            return
        self._hist_idx += 1
        cmd.value = self._history[self._hist_idx]
        cmd.cursor_position = len(cmd.value)

    @on(Input.Submitted, "#cmd")
    async def on_command(self, event: Input.Submitted) -> None:
        line = (event.value or "").strip()
        event.input.value = ""
        if not line:
            return
        self._record_history(line)

        log = self.query_one("#log", RichLog)
        log.write(Text.from_markup(f"[bold green]you ›[/] {line}"))

        try:
            argv = normalize_tui_input(line)
        except ValueError as exc:
            log.write(Text.from_markup(f"[red]Could not parse input:[/] {exc}"))
            return
        if not argv:
            return

        head = argv[0]
        if head in {"exit", "quit", ":q"}:
            self.exit(0)
            return
        if head in {"clear", "cls"}:
            log.clear()
            return
        if head in {"help", "?"}:
            log.write(Markdown(HELP_TEXT))
            return
        if head == "examples":
            log.write(Markdown(EXAMPLES_TEXT))
            return

        if self._busy:
            log.write(Text.from_markup("[yellow]busy — wait or Ctrl+C to cancel[/]"))
            return

        self._command_worker = self.run_command(line)

    @work(exclusive=True, group="command")
    async def run_command(self, line: str) -> None:
        log = self.query_one("#log", RichLog)
        self._set_busy(line[:60])
        started = time.monotonic()
        try:
            argv = normalize_tui_input(line)
            if not argv:
                return
            # ``normalize_tui_input`` keeps the old return shape for callers,
            # but plain natural language is now model-backed chat. Explicit
            # slash commands can still request legacy `ask`/RAG behavior.
            if not line.lstrip().startswith("/") and argv[0] == "ask":
                argv = ["chat", line]

            head = argv[0]
            if head == "connect":
                if self.mgr is None:
                    self.connect_session()
                else:
                    name = argv[1] if len(argv) > 1 else None
                    if name:
                        self.profile_name = name
                    await self._reconnect(log)
                return

            if self.mgr is None:
                log.write(
                    Panel(
                        Markdown(
                            "The MCP router is not connected. Try `/connect` or "
                            "run `hpe-mcp-doctor` in another terminal."
                        ),
                        title="Not connected",
                        border_style="red",
                    )
                )
                return

            text = await self._dispatch(argv)
            if text:
                duration = time.monotonic() - started
                self._write_result(log, text, command=argv[0], duration=duration)
        except asyncio.CancelledError:
            log.write(Text.from_markup("[yellow]request cancelled[/]"))
            raise
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - started
            log.write(
                Panel(
                    Markdown(
                        f"**{type(exc).__name__}:** `{exc}`\n\n"
                        "Check `/status`, retry, or run `hpe-mcp-doctor`."
                    ),
                    title=f"Request failed · {duration:.1f}s",
                    border_style="red",
                )
            )
        finally:
            self._command_worker = None
            self._set_busy(None)

    async def _reconnect(self, log: RichLog) -> None:
        status = self.query_one("#status", StatusBar)
        try:
            assert self.mgr is not None
            prof = await ensure_connected(self.mgr, self.cfg, profile=self.profile_name)
            status.set_status(
                connected=True,
                profile=prof.name,
                transport=prof.transport,
                tools=len(self.mgr.tools),
                products=self._get_active_products(),
                usage=format_usage(self._session_usage) if self._session_usage else "",
            )
            log.write(
                Text.from_markup(f"[green]connected[/] {prof.name} · {len(self.mgr.tools)} tools")
            )
        except Exception as exc:  # noqa: BLE001
            status.set_status(
                connected=False,
                profile=self.profile_name,
                products=self._get_active_products(),
                usage=format_usage(self._session_usage) if self._session_usage else "",
            )
            log.write(Text.from_markup(f"[red]connect failed:[/] {exc}"))

    async def _dispatch(self, argv: list[str]) -> str:
        assert self.mgr is not None
        head = argv[0]
        mgr = self.mgr
        safety = self.safety

        async def _invoke_text(tool: str, args: dict[str, Any] | None = None) -> str:
            try:
                resolved = mgr.resolve_tool_name(tool)
            except KeyError as exc:
                return f"error: {exc}"
            tool_obj = mgr.tools[resolved]
            decision = safety.check(tool_obj)
            if not decision.allowed:
                return f"blocked: {decision.reason}"
            result = await mgr.call_tool(resolved, args or {})
            return tool_result_to_text(result)

        # diagram workflow
        if head == "diagram":
            prompt_str = " ".join(argv[1:])
            pref = parse_diagram_intent(prompt_str)
            res = await execute_diagram_export(mgr, safety, pref)
            if res.get("ok"):
                return str(res.get("text", "Diagram generated"))
            return f"error: {res.get('error', 'Diagram export failed')}"

        # ai multi-turn reasoning
        if head in {"ai", "reason"} and len(argv) >= 2:
            head = "chat"

        if head == "chat" and len(argv) >= 2:
            if self._reasoning_service is None:
                self._reasoning_service = create_reasoning_service(
                    mgr,
                    safety,
                    provider=self.provider,
                    model=self.model,
                )
            result = await self._reasoning_service.complete(" ".join(argv[1:]))
            if result.usage:
                for key, value in result.usage.items():
                    self._session_usage[key] = self._session_usage.get(key, 0) + value
                status = self.query_one("#status", StatusBar)
                prof = self.cfg.profiles.get(self.profile_name)
                status.set_status(
                    connected=True,
                    profile=self.profile_name,
                    transport=prof.transport if prof else "",
                    tools=len(mgr.tools),
                    products=self._get_active_products(),
                    usage=format_usage(self._session_usage),
                )
            return result.content

        # troubleshoot
        if head in {"troubleshoot", "tb"} and len(argv) >= 2:
            from hpe_networking_mcp.pipeline.reasoning import (
                create_troubleshooting_plan,
                format_troubleshooting_report,
            )

            plan = create_troubleshooting_plan(" ".join(argv[1:]))
            return format_troubleshooting_report(plan)

        # migrate
        if head in {"migrate", "migration"}:
            from hpe_networking_mcp.pipeline.reasoning import (
                format_migration_plan_markdown,
                plan_migration,
            )

            vendor = argv[1] if len(argv) > 1 else "aos-s"
            plan_dict = plan_migration(vendor)
            return format_migration_plan_markdown(plan_dict)

        # architect
        if head in {"architect", "design"}:
            from hpe_networking_mcp.pipeline.reasoning import (
                format_architecture_recommendation_markdown,
                synthesize_architecture,
            )

            env = argv[1] if len(argv) > 1 else "campus"
            rec = synthesize_architecture(environment=env)
            return format_architecture_recommendation_markdown(rec)

        # tool / explore
        if head in {"tool", "explore"} and len(argv) >= 2:
            tool_name = argv[1]
            try:
                resolved = mgr.resolve_tool_name(tool_name)
                return format_tool_schema(resolved, mgr.tools[resolved])
            except KeyError:
                # Fall back to finding tool schema in backend catalog
                if any(n.endswith("find_tool") or n == "find_tool" for n in mgr.tools):
                    try:
                        res = await mgr.call_tool(
                            "find_tool",
                            {"query": tool_name, "include_schema": True, "top_k": 5},
                        )
                        structured = getattr(res, "structuredContent", None) or getattr(
                            res, "structured_content", None
                        )
                        hits = structured if isinstance(structured, list) else []
                        if not hits and getattr(res, "content", None):
                            for b in res.content:
                                t = getattr(b, "text", None)
                                if t and (t.startswith("[") or t.startswith("{")):
                                    try:
                                        parsed = json.loads(t)
                                        if isinstance(parsed, list):
                                            hits = parsed
                                    except Exception:
                                        pass
                        for h in hits:
                            if isinstance(h, dict) and (
                                h.get("name") == tool_name or tool_name in h.get("name", "")
                            ):
                                return format_tool_schema(h.get("name", tool_name), h)
                        if hits and isinstance(hits[0], dict):
                            return format_tool_schema(hits[0].get("name", tool_name), hits[0])
                    except Exception as exc:
                        return f"error: {exc}"
                return f"error: unknown tool '{tool_name}'"

        # shortcuts
        if head == "ask" and len(argv) >= 2:
            source = None
            rest = argv[1:]
            if "--source" in rest:
                i = rest.index("--source")
                source = rest[i + 1]
                rest = rest[:i] + rest[i + 2 :]
            args: dict[str, Any] = {"question": " ".join(rest)}
            if source:
                args["source"] = source
            # router path
            if any(n.endswith("invoke_read_tool") or n == "invoke_read_tool" for n in mgr.tools):
                return await _invoke_text(
                    "invoke_read_tool",
                    {"name": "ask_docs", "arguments": args},
                )
            return await _invoke_text("ask_docs", args)

        if head == "api" and len(argv) >= 2:
            q = " ".join(argv[1:] if argv[1] != "lookup" else argv[2:])
            if any(n.endswith("invoke_read_tool") or n == "invoke_read_tool" for n in mgr.tools):
                return await _invoke_text(
                    "invoke_read_tool",
                    {"name": "lookup_api", "arguments": {"query": q}},
                )
            return await _invoke_text("lookup_api", {"query": q})

        if head == "find" and len(argv) >= 2:
            q = " ".join(argv[1:])
            if any(n.endswith("find_tool") or n == "find_tool" for n in mgr.tools):
                return await _invoke_text("find_tool", {"query": q})
            # local filter fallback
            import contextlib
            from io import StringIO

            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                await cmd_tools_list(mgr, query=q, json_mode=False)
            return buf.getvalue() or "(no tools matched)"

        if head == "tools":
            if len(argv) >= 3 and argv[1] == "find":
                return await self._dispatch(["find", *argv[2:]])
            q = (
                argv[2]
                if len(argv) > 2 and argv[1] == "list"
                else (argv[1] if len(argv) > 1 and argv[1] != "list" else None)
            )
            # Build a simple text table
            from hpe_networking_mcp.cli_client.safety import tool_is_read_only

            lines = [
                "## Router tools",
                "",
                "These are the small discovery/dispatch layer. Use "
                "`/find <what you need>` to search the backend catalog.",
                "",
            ]
            query = (q or "").lower()
            for name in sorted(mgr.tools):
                tool = mgr.tools[name]
                desc = (getattr(tool, "description", None) or "")[:100]
                if query and query not in name.lower() and query not in desc.lower():
                    continue
                ro = "read" if tool_is_read_only(tool) else "write"
                lines.append(f"- **{name}** _{ro}_")
                if desc:
                    lines.append(f"  {desc}")
            return "\n".join(lines) if len(lines) > 2 else "(no tools matched)"

        if head == "rag" and len(argv) >= 3 and argv[1] == "ask":
            return await self._dispatch(["ask", *argv[2:]])

        if head == "invoke-read" and len(argv) >= 2:
            raw = argv[2] if len(argv) > 2 else "{}"
            return await _invoke_text(argv[1], parse_args_json(raw))

        if head == "skills":
            if len(argv) >= 2 and argv[1] == "list":
                from hpe_networking_mcp.cli_client.skills import discover_skills

                skills = discover_skills()
                lines = ["## Skills", ""]
                for s in skills:
                    lines.append(f"- **{s.name}** — {s.description[:100]}")
                return "\n".join(lines)
            if len(argv) >= 3 and argv[1] == "show":
                from hpe_networking_mcp.cli_client.skills import get_skill

                s = get_skill(argv[2])
                return f"# {s.name}\n\n{s.description}\n\n{s.body}"
            return "usage: skills list | skills show <name>"

        if head == "docs":
            from hpe_networking_mcp.cli_client.documents import DocumentStore

            store = DocumentStore()
            if len(argv) >= 2 and argv[1] == "list":
                coll = None
                if "--collection" in argv:
                    i = argv.index("--collection")
                    if i + 1 < len(argv):
                        coll = argv[i + 1]
                elif len(argv) > 2:
                    coll = argv[2]
                docs = store.list(coll)
                if not docs:
                    return "(no documents)"
                lines = [
                    "## Personal documents",
                    "",
                    "_Stored/bookmarked locally; searchable with `/docs search` "
                    "and `/docs search-content`._",
                    "",
                ]
                for d in docs:
                    lines.append(f"- `{d.id}` **{d.title}** _{d.collection}_")
                    lines.append(f"  {d.source_uri}")
                return "\n".join(lines)
            if len(argv) >= 3 and argv[1] == "add":
                src = argv[2]
                collection = "personal"
                title = None
                if "--collection" in argv:
                    i = argv.index("--collection")
                    if i + 1 < len(argv):
                        collection = argv[i + 1]
                if "--title" in argv:
                    i = argv.index("--title")
                    if i + 1 < len(argv):
                        title = argv[i + 1]
                if src.startswith("http://") or src.startswith("https://"):
                    rec = store.add_uri_record(
                        src,
                        collection=collection,
                        title=title,
                    )
                else:
                    rec = store.add_file(
                        src,
                        collection=collection,
                        title=title,
                    )
                return (
                    f"Stored `{rec.id}` → {rec.collection} ({rec.title}).\n\n"
                    "_Available to `/docs search` and `/docs search-content`._"
                )
            if len(argv) >= 3 and argv[1] == "remove":
                doc_id = argv[2]
                keep_file = "--keep-file" in argv
                removed = store.remove(doc_id, delete_file=not keep_file)
                if not removed:
                    return f"No document found with id `{doc_id}`."
                return f"Removed `{doc_id}` from the personal document index."
            if len(argv) >= 3 and argv[1] == "search-content":
                terms = argv[2:]
                collection = None
                if "--collection" in terms:
                    i = terms.index("--collection")
                    if i + 1 < len(terms):
                        collection = terms[i + 1]
                    terms = terms[:i]
                hits = store.search_content(" ".join(terms), collection=collection)
                if not hits:
                    return "(no text matches found in stored documents)"
                lines = [
                    "## Document text matches",
                    "",
                ]
                for d in hits:
                    lines.append(f"- `{d['id']}` **{d['title']}** _{d['collection']}_")
                    lines.append(f"  {d['snippet']}")
                return "\n".join(lines)
            if len(argv) >= 3 and argv[1] == "search":
                terms = argv[2:]
                collection = None
                if "--collection" in terms:
                    i = terms.index("--collection")
                    if i + 1 < len(terms):
                        collection = terms[i + 1]
                    terms = terms[:i]
                hits = store.search(" ".join(terms), collection=collection)
                if not hits:
                    return "(no matches)"
                lines = [
                    "## Document metadata matches",
                    "",
                    "_Local title/path/tag search._",
                    "",
                ]
                for d in hits:
                    lines.append(f"- `{d.id}` **{d.title}**")
                return "\n".join(lines)
            return (
                "usage: docs list|add <path>|remove <id>|search <q>|"
                "search-content <q> [--collection name]"
            )

        if head in {"profiles", "profile"}:
            if head == "profile" and len(argv) > 1:
                self.profile_name = argv[1]
                log = self.query_one("#log", RichLog)
                await self._reconnect(log)
                return f"Switched profile to `{self.profile_name}`."
            lines = ["## Profiles", ""]
            for n, p in sorted(self.cfg.profiles.items()):
                mark = "*" if n == self.profile_name else " "
                lines.append(f"{mark} **{n}** `{p.transport}` — {p.description}")
            return "\n".join(lines)

        if head in {"products", "product", "toolsets", "toolset"}:
            if len(argv) > 1:
                val = argv[1] if argv[1] != "set" else (argv[2] if len(argv) > 2 else "")
                if head in {"toolsets", "toolset"}:
                    os.environ["HPE_MCP_TOOLSETS"] = val
                else:
                    os.environ["HPE_MCP_PRODUCTS"] = val
                log = self.query_one("#log", RichLog)
                await self._reconnect(log)
                prods = os.environ.get("HPE_MCP_PRODUCTS", "(default)")
                toolsets = os.environ.get("HPE_MCP_TOOLSETS", "(default)")
                return (
                    f"Updated configuration & reconnected.\n\n"
                    f"- **HPE_MCP_PRODUCTS:** `{prods}`\n"
                    f"- **HPE_MCP_TOOLSETS:** `{toolsets}`"
                )

            prods = os.environ.get("HPE_MCP_PRODUCTS", "(default)")
            toolsets = os.environ.get("HPE_MCP_TOOLSETS", "(default)")
            mode = os.environ.get("HPE_MCP_ROUTER_MODE", "minimal")
            lines = [
                "## Active Product Configuration",
                "",
                f"- **HPE_MCP_PRODUCTS:** `{prods}`",
                f"- **HPE_MCP_TOOLSETS:** `{toolsets}`",
                f"- **HPE_MCP_ROUTER_MODE:** `{mode}`",
                "",
                "_Tip: Use `/products <names>` or `/toolsets <names>` to switch dynamically._",
            ]
            return "\n".join(lines)

        if head == "usage":
            if not self._session_usage:
                return "No AI token usage recorded in this session."
            return f"## Cumulative Session Token Usage\n\n`{format_usage(self._session_usage)}`"

        if head == "status":
            state_str = self.mgr.state.value if self.mgr else "disconnected"
            profile = self.cfg.profiles.get(self.profile_name)
            lines = [
                "## Client status\n",
                f"- MCP connection state: **`{state_str}`**",
                f"- Active profile: `{self.profile_name}`",
            ]
            if profile is not None:
                target = profile.command or profile.url or "(unset)"
                target_redacted = redact_sensitive_text(target)
                lines.append(f"- Transport: `{profile.transport}` → `{target_redacted}`")
                if profile.description:
                    lines.append(f"- Profile description: {profile.description}")
            rec = self.mgr.connected.get(self.profile_name) if self.mgr else None
            if rec is not None:
                lines.append(f"- Connected server: **{rec.server_name}**")
                lines.append(f"- Connected for: {format_duration(time.time() - rec.connected_at)}")
            lines.append(f"- Visible router tools: **{len(mgr.tools)}**")
            by_server: dict[str, int] = {}
            for tool_name in mgr.tools:
                prefix = tool_name.split(".", 1)[0] if "." in tool_name else "(unnamespaced)"
                by_server[prefix] = by_server.get(prefix, 0) + 1
            if len(by_server) > 1:
                for prefix, count in sorted(by_server.items(), key=lambda kv: -kv[1]):
                    lines.append(f"  - `{prefix}`: {count}")
            lines.append("- Safety policy: **read-only default**")
            lines.append(
                f"- AI backend: **`{self.provider}`**"
                + (f" / `{self.model}`" if self.model else "")
            )
            lines.append("- Personal docs: **local metadata + content search enabled**")
            if self.mgr and self.mgr.last_error:
                lines.append(f"- Last error: `{self.mgr.last_error}`")
            return "\n".join(lines)

        return (
            f"Unknown command: `/{head}`\n\n"
            "Type a question normally, or use `/help` to see controls."
        )

    def _write_result(
        self,
        log: RichLog,
        text: str,
        *,
        command: str,
        duration: float,
    ) -> None:
        text = redact_sensitive_text(text)
        stripped = text.lstrip()
        border = "cyan"
        title = {
            "ask": "Answer",
            "chat": "AI Chat",
            "api": "API lookup",
            "find": "Tool search",
            "tools": "Router tools",
            "tool": "Tool Schema",
            "explore": "Tool Explorer",
            "diagram": "Network Diagram",
            "ai": "AI Reasoning",
            "reason": "AI Reasoning",
            "troubleshoot": "Diagnostic Plan",
            "tb": "Diagnostic Plan",
            "migrate": "Migration Plan",
            "migration": "Migration Plan",
            "architect": "Architecture Blueprint",
            "design": "Architecture Blueprint",
            "docs": "Documents",
            "skills": "Skills",
            "profiles": "Profiles",
            "profile": "Profile",
            "products": "Products",
            "product": "Products",
            "usage": "Usage",
            "status": "Status",
        }.get(command, command)
        if stripped.lower().startswith(("error:", "blocked:")):
            border = "red"
        if (
            stripped.startswith("#")
            or "\n### " in text
            or "\n## " in text
            or "\n### Sources" in text
            or (len(text) > 160 and not stripped.startswith("{"))
        ):
            body: Any = Markdown(text)
        elif stripped.startswith("{") or stripped.startswith("["):
            body = Text(text)
        else:
            body = Text(text)
        log.write(
            Panel(
                body,
                title=f"{title} · {duration:.1f}s",
                border_style=border,
            )
        )

    async def on_unmount(self) -> None:
        self._save_history()
        if self._command_worker is not None and not self._command_worker.is_finished:
            self._command_worker.cancel()
        if self.mgr is not None:
            try:
                await self.mgr.__aexit__(None, None, None)
            except Exception:
                pass
            self.mgr = None


def run_tui(
    cfg: ClientConfig,
    safety: SafetyPolicy,
    *,
    profile: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Run the Textual app; returns process exit code."""
    app = HpeMcpApp(
        cfg,
        safety,
        profile=profile,
        provider=provider,
        model=model,
    )
    result = app.run()
    if isinstance(result, int):
        return result
    return 0
