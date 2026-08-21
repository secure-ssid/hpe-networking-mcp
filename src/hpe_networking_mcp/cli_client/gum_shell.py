"""Gum-enhanced UX layer for the hpe-mcp REPL shell.

Wraps the key interaction points — prompt, spinner, header, confirm — with
`gum` (https://github.com/charmbracelet/gum) when it is installed.  Falls
back to plain Rich / readline when gum is not on $PATH.

Usage::

    from hpe_networking_mcp.cli_client.gum_shell import GumShell
    shell = GumShell()          # auto-detects gum availability
    shell.header("v0.8.0", profile="local-router")
    line = shell.prompt()       # blocking, raises EOFError on Ctrl-D
    with shell.spin("connecting…"):
        ...
    if shell.confirm("Apply changes?"):
        ...
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import Generator
from typing import Any


def _gum() -> str | None:
    """Return the path to gum if available, else None."""
    return shutil.which("gum")


def _run(*args: str, capture: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=capture,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Styled header
# ---------------------------------------------------------------------------

_BANNER = "HPE NETWORKING MCP"

_TAGLINE = "standalone MCP client · read-only default"


def _print_gum_header(version: str, profile: str, tools: int = 0) -> None:
    gum = _gum()
    if not gum:
        return
    meta = f"v{version}  ·  profile={profile}"
    if tools:
        meta += f"  ·  tools={tools}"
    meta += f"  ·  {_TAGLINE}"
    # Title block
    _run(
        gum, "style",
        "--foreground", "6",  # cyan
        "--bold",
        "--border", "rounded",
        "--border-foreground", "2",  # green
        "--padding", "0 2",
        "--margin", "0 0",
        _BANNER,
        capture=False,
    )
    # Meta line
    _run(
        gum, "style",
        "--foreground", "8",  # dim
        "--padding", "0 2",
        meta,
        capture=False,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _gum_input(placeholder: str = "Ask a networking question…  (/ for commands)") -> str:
    """Read one line with gum input. Raises EOFError on Ctrl-D / cancel."""
    gum = _gum()
    if not gum:
        raise RuntimeError("gum not available")
    # stdin must be inherited so gum can read keystrokes from the TTY.
    # stdout is captured to retrieve the entered text.
    result = subprocess.run(
        [
            gum, "input",
            "--placeholder", placeholder,
            "--prompt", "› ",
            "--prompt.foreground", "2",   # green
            "--cursor.foreground", "6",   # cyan
            "--width", "0",               # fill terminal width
        ],
        stdin=None,          # inherit parent TTY stdin
        stdout=subprocess.PIPE,
        stderr=None,         # inherit so gum can draw to terminal
        text=True,
    )
    if result.returncode != 0:
        raise EOFError
    line = result.stdout.rstrip("\n")
    if not line:
        # Empty Enter — return empty string; caller will continue the loop.
        return ""
    return line


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _gum_spin(title: str) -> Generator[None, None, None]:
    """Context manager that shows a gum spinner while the body runs.

    Runs gum spin as a side process writing directly to stderr (the TTY),
    so it doesn't interfere with stdout or stdin used by gum input / rich.
    """
    gum = _gum()
    if not gum:
        yield
        return

    proc = subprocess.Popen(
        [gum, "spin", "--spinner", "dot", "--title", f" {title}", "--", "sleep", "3600"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,  # inherit TTY so spinner renders
    )
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

def _gum_confirm(prompt: str, default: bool = False) -> bool:
    gum = _gum()
    if not gum:
        raise RuntimeError("gum not available")
    args = [gum, "confirm", prompt]
    if not default:
        args += ["--default=No"]
    result = _run(*args, capture=False)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Write confirmation (for safety gate)
# ---------------------------------------------------------------------------

def _gum_write_confirm(tool_name: str, args_preview: str) -> bool:
    """Show tool + args and ask the user to confirm a write operation."""
    gum = _gum()
    if not gum:
        raise RuntimeError("gum not available")
    # Show the tool/args panel
    _run(
        gum, "style",
        "--border", "rounded",
        "--border-foreground", "3",  # yellow
        "--padding", "0 2",
        f"⚠  Write operation: {tool_name}\n\n{args_preview}",
        capture=False,
    )
    return _gum_confirm("Proceed?", default=False)


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------

class GumShell:
    """Facade that picks gum or Rich fallback at construction time."""

    def __init__(self) -> None:
        self._has_gum: bool = bool(_gum())
        # Import Rich lazily to avoid circular imports.
        from hpe_networking_mcp.cli_client.output import console as _console  # noqa: PLC0415
        self._console = _console

    @property
    def has_gum(self) -> bool:
        return self._has_gum

    # -- Header --

    def header(self, version: str, *, profile: str = "local-router", tools: int = 0) -> None:
        if self._has_gum:
            _print_gum_header(version, profile, tools)
        else:
            from hpe_networking_mcp.cli_client.banner import print_banner  # noqa: PLC0415
            print_banner(self._console, profile=profile, mode="shell")

    # -- Prompt --

    async def prompt(
        self,
        placeholder: str = "Ask a networking question…  (/ for commands)",
    ) -> str:
        """Async-friendly prompt. Raises EOFError on Ctrl-D / cancel."""
        if self._has_gum:
            return await asyncio.to_thread(_gum_input, placeholder)
        from hpe_networking_mcp.cli_client.repl_input import read_line  # noqa: PLC0415
        return await asyncio.to_thread(read_line, "hpe-mcp> ")

    # -- Spinner --

    @contextlib.contextmanager
    def spin(self, title: str) -> Generator[None, None, None]:
        if self._has_gum:
            with _gum_spin(title):
                yield
        else:
            self._console.print(f"[dim]… {title}[/]")
            yield

    # -- Confirm --

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        if self._has_gum:
            try:
                return _gum_confirm(prompt, default=default)
            except Exception:  # noqa: BLE001
                pass
        # Rich fallback: inline y/n
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def write_confirm(self, tool_name: str, args_preview: str) -> bool:
        if self._has_gum:
            try:
                return _gum_write_confirm(tool_name, args_preview)
            except Exception:  # noqa: BLE001
                pass
        return self.confirm(f"Write via {tool_name}?")

    # -- Status line helpers --

    def connected(self, profile: str, transport: str, tools: int) -> None:
        if self._has_gum:
            gum = _gum()
            msg = f"✓ connected  {profile}  ({transport})  ·  {tools} tools"
            _run(
                gum, "style",  # type: ignore[arg-type]
                "--foreground", "2",
                msg,
                capture=False,
            )
        else:
            self._console.print(
                f"[green]connected[/] {profile} ({transport}) · {tools} tools"
            )

    def error(self, msg: str) -> None:
        if self._has_gum:
            gum = _gum()
            _run(
                gum, "style",  # type: ignore[arg-type]
                "--foreground", "1",
                f"✗ {msg}",
                capture=False,
            )
        else:
            self._console.print(f"[red]✗[/] {msg}")

    def info(self, msg: str) -> None:
        if self._has_gum:
            gum = _gum()
            _run(
                gum, "style",  # type: ignore[arg-type]
                "--foreground", "8",
                msg,
                capture=False,
            )
        else:
            self._console.print(f"[dim]{msg}[/]")

    def print_shortcuts(self) -> None:
        if self._has_gum:
            gum = _gum()
            text = (
                "shortcuts  ask · api · find · tools · diagram · help · exit\n"
                "           rag ask · tools list|find · api lookup · invoke-read\n"
                "           skills · docs · profiles · connect\n"
                "history    ↑/↓ recall · Ctrl-C cancel · Ctrl-D exit"
            )
            _run(
                gum, "style",
                "--border", "normal",
                "--border-foreground", "8",
                "--foreground", "8",
                "--padding", "0 1",
                text,
                capture=False,
            )
        else:
            self._console.print(
                "[dim]shortcuts:[/] ask <q> · api <q> · find <q> · tools · help · exit\n"
                "[dim]history:[/] ↑/↓ recall · Ctrl-C cancel"
            )
