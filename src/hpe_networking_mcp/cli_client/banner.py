"""Startup banner for the standalone hpe-networking-mcp CLI.

Rendered with Rich when available. Falls back to plain text so one-shot
scripts and non-TTY environments still get a clean header without color
noise.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

# Unicode box-drawing wordmark for "HPE NETWORKING MCP"
_BLOCK = r"""
╦ ╦╔═╗╔═╗  ┌┐┌┌─┐┌┬┐┬ ┬┌─┐┬─┐┬┌─┬┌┐┌┌─┐  ┌┬┐┌─┐┌─┐
╠═╣╠═╝║╣───│││├┤  │ ││││ │├┬┘├┴┐│││││ ┬───│││││  ├─┘
╩ ╩╩  ╚═╝   ┘└┘└─┘ ┴ └┴┘└─┘┴└─┴ ┴┴┘└┘└─┘  ─┴┘└─┘┴
""".strip(
    "\n"
)

# ASCII-only fallback intentionally omitted (E501); compact form used on narrow TTYs.
_COMPACT = "hpe-networking-mcp"


def package_version() -> str:
    try:
        return version("hpe-networking-mcp")
    except PackageNotFoundError:
        return "0.8.0"


def select_wordmark(*, width: int, style: str = "block") -> str:
    """Pick a wordmark that fits the terminal width."""
    if style == "compact" or width < 52:
        return _COMPACT
    if style == "ascii":
        return _COMPACT
    full_width = max(len(line) for line in _BLOCK.splitlines())
    return _BLOCK if width >= full_width + 2 else _COMPACT


def render_banner(
    *,
    width: int = 80,
    profile: str | None = None,
    transport: str | None = None,
    servers: int | None = None,
    mode: str = "shell",
    style: str = "block",
) -> str:
    """Return a plain-text banner block (no ANSI codes)."""
    wordmark = select_wordmark(width=width, style=style)
    ver = package_version()
    meta: list[str] = [f"hpe-networking-mcp  v{ver}", f"mode={mode}"]
    if profile:
        meta.append(f"profile={profile}")
    if transport:
        meta.append(f"transport={transport}")
    if servers is not None:
        meta.append(f"servers={servers}")
    meta.append("read-only default · dry-run before writes")
    bar = "─" * min(max(width - 2, 24), 72)
    return "\n".join([wordmark, bar, " · ".join(meta), bar])


def print_banner(
    console: Any | None = None,
    *,
    profile: str | None = None,
    transport: str | None = None,
    servers: int | None = None,
    mode: str = "shell",
    quiet: bool = False,
    style: str = "block",
) -> None:
    """Print the boot logo. No-op when ``quiet`` is set or stdout is not a TTY."""
    if quiet:
        return

    if console is None:
        try:
            from rich.console import Console

            console = Console(stderr=False)
        except Exception:
            console = None

    if console is not None:
        try:
            width = getattr(getattr(console, "size", None), "width", 80) or 80
            if hasattr(console, "is_terminal") and not console.is_terminal:
                return
            text = render_banner(
                width=width,
                profile=profile,
                transport=transport,
                servers=servers,
                mode=mode,
                style=style,
            )
            try:
                from rich.panel import Panel
                from rich.text import Text

                lines = text.splitlines()
                body = Text()
                bar_seen = 0
                for i, line in enumerate(lines):
                    suffix = "\n" if i < len(lines) - 1 else ""
                    if set(line) <= {"─"} and line:
                        bar_seen += 1
                        body.append(line + suffix, style="dim green")
                    elif bar_seen == 0:
                        body.append(line + suffix, style="bold cyan")
                    else:
                        body.append(line + suffix, style="dim")
                console.print(
                    Panel.fit(
                        body,
                        border_style="green",
                        title="[bold green]hpe-networking-mcp[/]",
                        subtitle="[dim]standalone MCP client[/]",
                    )
                )
                return
            except Exception:
                console.print(text)
                return
        except Exception:
            pass

    import sys

    if not sys.stdout.isatty():
        return
    print(
        render_banner(
            width=80,
            profile=profile,
            transport=transport,
            servers=servers,
            mode=mode,
            style=style,
        )
    )
