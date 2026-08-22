"""UTF-8 manifest IO contract: locale-independent reads, skip-don't-kill discovery.

Regression coverage for the find_tool cluster: bare ``Path.read_text()`` decoded
manifests with the ambient Windows locale (cp1252), so non-ASCII bytes in a
packaged manifest raised UnicodeDecodeError out of ``_generated_records()`` and
out of ``load_manifest()`` during catalog builds. Manifest files are repo
artifacts stored as UTF-8; reads/writes pin that explicitly, and the router
skips an undecodable or corrupt manifest instead of losing all discovery.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hpe_networking_mcp.mcp_servers import tool_router
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod
from hpe_networking_mcp.mcp_servers.openapi_gen.naming import digest

GOOD_MANIFEST = {
    "operations": [
        {
            "name": "op_alpha",
            "method": "get",
            "path": "/alpha",
            "operation_id": "getOpAlpha",
            "description": "café – naïve “quoted”",
        }
    ]
}


@pytest.fixture
def manifest_dir(tmp_path, monkeypatch):
    """Point MANIFEST_DIR (and the router cache) at an isolated directory."""
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(tool_router, "_generated_tool_records", None)
    return tmp_path


def _write_good_manifest(directory: Path) -> None:
    (directory / "good.json").write_bytes(
        json.dumps(GOOD_MANIFEST, ensure_ascii=False).encode("utf-8")
    )


def test_load_manifest_parses_non_ascii_utf8(manifest_dir):
    _write_good_manifest(manifest_dir)
    assert manifest_mod.load_manifest("good") == GOOD_MANIFEST


def test_write_manifest_emits_utf8_and_roundtrips(manifest_dir):
    written = manifest_mod.write_manifest("good", GOOD_MANIFEST)
    # Bytes must be UTF-8 regardless of ambient locale...
    assert json.loads(written.read_bytes().decode("utf-8")) == GOOD_MANIFEST
    # ...and the reader must agree with the writer.
    assert manifest_mod.load_manifest("good") == GOOD_MANIFEST


def test_generated_records_skips_corrupt_and_undecodable_manifests(manifest_dir):
    _write_good_manifest(manifest_dir)
    # Invalid UTF-8 bytes: propagated out of _generated_records() before the
    # encoding/except fix and killed semantic discovery entirely.
    (manifest_dir / "bad_bytes.json").write_bytes(b'{"operations": [\x80\x81]}')
    # Valid UTF-8 but invalid JSON: skipped pre-fix too, pinned here.
    (manifest_dir / "bad_json.json").write_text('{"operations": [', encoding="utf-8")

    records = tool_router._generated_records()

    alias = f"op_alpha_g{digest('get', '/alpha')}"
    assert set(records) == {"op_alpha", alias}
    assert records["op_alpha"]["operation_id"] == "getOpAlpha"
    assert records["op_alpha"]["manifest_platform"] == "good"


def test_load_manifest_parses_non_ascii_regardless_of_ambient_locale(manifest_dir):
    """Subprocess contract: with PYTHONUTF8 stripped the ambient locale decides,
    and parsing must survive it (this is the shape that reproduced the bug)."""
    _write_good_manifest(manifest_dir)

    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as m\n"
        "m.MANIFEST_DIR = Path(sys.argv[1])\n"
        "doc = m.load_manifest('good')\n"
        "assert doc['operations'][0]['description'] == "
        "'caf\\u00e9 \\u2013 na\\u00efve \\u201cquoted\\u201d', doc\n"
        "print('ok')\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    proc = subprocess.run(
        [sys.executable, "-c", script, str(manifest_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")
