"""Interactive input with up/down history (readline / libedit).

macOS ships libedit-backed ``readline``. Importing it before ``input()`` is
what makes arrow keys work instead of printing ``^[[A``.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from hpe_networking_mcp.cli_client.config import default_user_data_dir

_HISTORY_NAME = "shell_history"
_MAX_HISTORY = 2000
_configured = False


def history_path() -> Path:
    override = os.environ.get("HPE_MCP_SHELL_HISTORY")
    if override:
        return Path(override).expanduser()
    return default_user_data_dir() / _HISTORY_NAME


def configure_readline() -> bool:
    """Enable line editing + persistent history. Returns True if available."""
    global _configured
    if _configured:
        return True
    try:
        import readline
    except ImportError:
        return False

    # libedit (macOS) vs GNU readline bind syntax differs.
    try:
        doc = (getattr(readline, "__doc__", None) or "").lower()
        if "libedit" in doc:
            readline.parse_and_bind("bind ^I rl_complete")
            # Emacs-style history is default on libedit once module is loaded.
        else:
            readline.parse_and_bind("tab: complete")
            readline.parse_and_bind("set editing-mode emacs")
            readline.parse_and_bind("\\e[A: history-search-backward")
            readline.parse_and_bind("\\e[B: history-search-forward")
    except Exception:
        pass

    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            readline.read_history_file(str(path))
    except OSError:
        pass

    try:
        readline.set_history_length(_MAX_HISTORY)
    except Exception:
        pass

    def _save() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(path))
        except OSError:
            pass

    atexit.register(_save)
    _configured = True
    return True


def read_line(prompt: str = "hpe-mcp> ") -> str:
    """Blocking prompt with history. Raises EOFError on Ctrl-D."""
    configure_readline()
    return input(prompt)
