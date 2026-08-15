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
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from hpe_networking_mcp.cli_client.banner import package_version
from hpe_networking_mcp.cli_client.commands import (
    cmd_tools_list,
    ensure_connected,
    parse_args_json,
)
from hpe_networking_mcp.cli_client.config import ClientConfig
from hpe_networking_mcp.cli_client.output import tool_result_to_text
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager

HELP_TEXT = """\
# hpe-mcp shortcuts

| Command | What it does |
|---|---|
| `ask <question>` | RAG / docs Q&A |
| `api <query>` | OpenAPI lookup |
| `find <query>` | Find tools |
| `tools` | List router tools |
| `docs list\\|add\\|search` | Personal documents |
| `skills list\\|show` | Local SKILL.md |
| `connect [profile]` | (Re)connect MCP |
| `clear` | Clear the log |
| `help` | This help |
| `exit` / `quit` | Leave the TUI |

**Keys:** `↑`/`↓` input history · `Ctrl+L` clear · `Ctrl+C` cancel busy · `q` quit
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
    width: 28;
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
        Binding("q", "quit_app", "Quit", show=True),
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
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.safety = safety
        self.profile_name = profile or cfg.default_profile
        self.mgr: SessionManager | None = None
        self._history: list[str] = []
        self._hist_idx: int = 0
        self._busy = False
        self._cancel_event = asyncio.Event()
        self._exit_code = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")
        with Horizontal(id="main"):
            yield RichLog(id="log", highlight=True, markup=True, wrap=True, auto_scroll=True)
            yield Static(
                "[bold]shortcuts[/]\n"
                "ask <q>\n"
                "api <q>\n"
                "find <q>\n"
                "tools\n"
                "docs list\n"
                "skills list\n"
                "connect\n"
                "clear · help\n"
                "exit / q",
                id="side",
            )
        yield Static("", id="busy")
        with Vertical(id="input-row"):
            yield Input(
                placeholder="ask <question>  |  api <query>  |  find <tool>  |  help",
                id="cmd",
            )
        yield Footer()

    async def on_mount(self) -> None:
        status = self.query_one("#status", StatusBar)
        status.set_status(connected=False, profile=self.profile_name, message="starting…")
        log = self.query_one("#log", RichLog)
        log.write(
            Panel(
                Text.from_markup(
                    "[bold cyan]HPE NETWORKING MCP[/]\n"
                    "[dim]TUI shell · read-only default · dry-run before writes[/]"
                ),
                border_style="green",
                title="hpe-networking-mcp",
            )
        )
        self.query_one("#cmd", Input).focus()
        self.connect_session()

    @work(exclusive=True)
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
            status.set_status(
                connected=True,
                profile=prof.name,
                transport=prof.transport,
                tools=len(self.mgr.tools),
            )
            log.write(
                Text.from_markup(
                    f"[green]connected[/] {prof.name} ({prof.transport}) · "
                    f"{len(self.mgr.tools)} tools"
                )
            )
        except Exception as exc:  # noqa: BLE001
            status.set_status(connected=False, profile=self.profile_name, message="error")
            log.write(Text.from_markup(f"[red]connect failed:[/] {exc}"))
        finally:
            self._set_busy(None)

    def _set_busy(self, message: str | None) -> None:
        self._busy = bool(message)
        busy = self.query_one("#busy", Static)
        if message:
            busy.update(f"… {message}")
            busy.add_class("visible")
        else:
            busy.update("")
            busy.remove_class("visible")

    def action_quit_app(self) -> None:
        self.exit(self._exit_code)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_show_help(self) -> None:
        self.query_one("#log", RichLog).write(Markdown(HELP_TEXT))

    def action_cancel(self) -> None:
        if self._busy:
            self._cancel_event.set()
            self.query_one("#log", RichLog).write(
                Text.from_markup("[yellow]cancel requested…[/]")
            )
        else:
            # empty input cancel → quit confirm style: just note
            self.query_one("#log", RichLog).write(
                Text.from_markup("[dim]nothing to cancel · press q to quit[/]")
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
        if not self._history or self._history[-1] != line:
            self._history.append(line)
        self._hist_idx = len(self._history)

        log = self.query_one("#log", RichLog)
        log.write(Text.from_markup(f"[bold green]hpe-mcp>[/] {line}"))

        if line in {"exit", "quit", ":q"}:
            self.exit(0)
            return
        if line in {"clear", "cls"}:
            log.clear()
            return
        if line in {"help", "?"}:
            log.write(Markdown(HELP_TEXT))
            return

        if self._busy:
            log.write(Text.from_markup("[yellow]busy — wait or Ctrl+C to cancel[/]"))
            return

        self.run_command(line)

    @work(exclusive=True)
    async def run_command(self, line: str) -> None:
        import shlex

        log = self.query_one("#log", RichLog)
        self._cancel_event.clear()
        self._set_busy(line[:60])
        try:
            try:
                argv = shlex.split(line)
            except ValueError as exc:
                log.write(Text.from_markup(f"[red]parse error:[/] {exc}"))
                return

            if not argv:
                return
            if self.mgr is None:
                log.write(Text.from_markup("[red]not connected[/] — try: connect"))
                return

            head = argv[0]
            if head == "connect":
                name = argv[1] if len(argv) > 1 else None
                if name:
                    self.profile_name = name
                await self._reconnect(log)
                return

            text = await self._dispatch(argv)
            if self._cancel_event.is_set():
                log.write(Text.from_markup("[yellow]cancelled[/]"))
                return
            if text:
                self._write_result(log, text)
        except Exception as exc:  # noqa: BLE001
            log.write(Text.from_markup(f"[red]{type(exc).__name__}:[/] {exc}"))
        finally:
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
            )
            log.write(
                Text.from_markup(
                    f"[green]connected[/] {prof.name} · {len(self.mgr.tools)} tools"
                )
            )
        except Exception as exc:  # noqa: BLE001
            status.set_status(connected=False, profile=self.profile_name)
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

        # shortcuts
        if head in {"ask", "q"} and len(argv) >= 2:
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
            q = argv[2] if len(argv) > 2 and argv[1] == "list" else (
                argv[1] if len(argv) > 1 and argv[1] != "list" else None
            )
            # Build a simple text table
            from hpe_networking_mcp.cli_client.safety import tool_is_read_only

            lines = ["## Tools", ""]
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
                coll = argv[2] if len(argv) > 2 else None
                docs = store.list(coll)
                if not docs:
                    return "(no documents)"
                lines = ["## Personal documents", ""]
                for d in docs:
                    lines.append(f"- `{d.id}` **{d.title}** _{d.collection}_")
                    lines.append(f"  {d.source_uri}")
                return "\n".join(lines)
            if len(argv) >= 3 and argv[1] == "add":
                src = argv[2]
                if src.startswith("http://") or src.startswith("https://"):
                    rec = store.add_uri_record(src)
                else:
                    rec = store.add_file(src)
                return f"added `{rec.id}` → {rec.collection} ({rec.title})"
            if len(argv) >= 3 and argv[1] == "search":
                hits = store.search(" ".join(argv[2:]))
                if not hits:
                    return "(no matches)"
                lines = ["## Document matches", ""]
                for d in hits:
                    lines.append(f"- `{d.id}` **{d.title}**")
                return "\n".join(lines)
            return "usage: docs list|add <path>|search <q>"

        if head == "profiles":
            lines = ["## Profiles", ""]
            for n, p in sorted(self.cfg.profiles.items()):
                mark = "*" if n == self.cfg.default_profile else " "
                lines.append(f"{mark} **{n}** `{p.transport}` — {p.description}")
            return "\n".join(lines)

        return (
            f"unknown command: `{head}`\n\n"
            "Try: ask · api · find · tools · docs · skills · help"
        )

    def _write_result(self, log: RichLog, text: str) -> None:
        stripped = text.lstrip()
        if (
            stripped.startswith("#")
            or "\n### " in text
            or "\n## " in text
            or "\n### Sources" in text
            or (len(text) > 160 and not stripped.startswith("{"))
        ):
            log.write(Markdown(text))
        elif stripped.startswith("{") or stripped.startswith("["):
            log.write(text)
        else:
            log.write(text)

    async def on_unmount(self) -> None:
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
) -> int:
    """Run the Textual app; returns process exit code."""
    app = HpeMcpApp(cfg, safety, profile=profile)
    result = app.run()
    if isinstance(result, int):
        return result
    return 0
