"""Guard: importing serving-path modules must not write to stdout.

stdio MCP servers frame JSON-RPC responses on stdout. A stray module-level
``print()`` corrupts the stream before any tool runs, and nothing else in CI
catches that class. This test imports every serving-path module in a clean
subprocess (so already-cached ``sys.modules`` entries cannot mask import-time
output) with stdout captured, and fails naming any offender.

Modules whose third-party dependencies are not installed are reported as
unimportable rather than failing — the run under ``--all-extras`` covers them.
"""

import json
import subprocess
import sys
from pathlib import Path

import hpe_networking_mcp

# One interpreter, every module imported fresh in listing order: fast, and
# immune to pytest's own prior imports of these modules.
_CHILD = """\
import contextlib, importlib, io, json, sys
result = {"offenders": {}, "unimportable": []}
for name in sys.argv[1:]:
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            importlib.import_module(name)
    except ModuleNotFoundError:
        result["unimportable"].append(name)
        continue
    written = buffer.getvalue()
    if written:
        result["offenders"][name] = written
sys.stderr.write(json.dumps(result))
"""


def _serving_module_names() -> list[str]:
    """Discover the serving-path surface from disk, so new files are guarded."""
    package_root = Path(hpe_networking_mcp.__file__).resolve().parent
    names = [
        f"hpe_networking_mcp.mcp_servers.{path.stem}"
        for path in sorted((package_root / "mcp_servers").glob("*.py"))
        if path.stem != "__init__"
    ]
    names += [
        f"hpe_networking_mcp.pipeline.clients.{path.stem}"
        for path in sorted((package_root / "pipeline" / "clients").glob("*.py"))
        if path.stem != "__init__"
    ]
    names.append("hpe_networking_mcp.pipeline.config")
    return names


def test_serving_imports_write_nothing_to_stdout() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, *_serving_module_names()],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stderr)
    assert not result["offenders"], (
        f"{len(result['offenders'])} serving-path module(s) wrote to stdout at "
        f"import time, which corrupts stdio JSON-RPC framing:\n"
        f"{json.dumps(result['offenders'], indent=2)}"
    )
