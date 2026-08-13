"""Sandboxed writes under outputs/diagrams/."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

REPO_ROOT = repo_root()
DIAGRAM_OUT = REPO_ROOT / "outputs" / "diagrams"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def diagram_output_dir() -> Path:
    DIAGRAM_OUT.mkdir(parents=True, exist_ok=True)
    return DIAGRAM_OUT


def safe_stem(name: str) -> str:
    stem = (name or "diagram").strip()
    stem = stem.replace(" ", "_")
    if not _SAFE_NAME.match(stem):
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
    if not stem:
        stem = "diagram"
    return stem


def write_text_artifact(stem: str, ext: str, content: str) -> dict[str, Any]:
    out_dir = diagram_output_dir()
    if not ext.startswith("."):
        ext = f".{ext}"
    path = out_dir / f"{safe_stem(stem)}{ext}"
    # ensure still under out_dir
    resolved = path.resolve()
    if not str(resolved).startswith(str(out_dir.resolve())):
        raise ValueError("refusing to write outside outputs/diagrams")
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "bytes": path.stat().st_size}


def write_bytes_artifact(stem: str, ext: str, content: bytes) -> dict[str, Any]:
    out_dir = diagram_output_dir()
    if not ext.startswith("."):
        ext = f".{ext}"
    path = out_dir / f"{safe_stem(stem)}{ext}"
    resolved = path.resolve()
    if not str(resolved).startswith(str(out_dir.resolve())):
        raise ValueError("refusing to write outside outputs/diagrams")
    path.write_bytes(content)
    return {"path": str(path), "bytes": path.stat().st_size}


def write_json_artifact(stem: str, ext: str, obj: Any) -> dict[str, Any]:
    return write_text_artifact(stem, ext, json.dumps(obj, indent=2) + "\n")
