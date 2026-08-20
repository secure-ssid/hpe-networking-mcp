"""Deterministic scrape-status reports for RAG source collectors.

A refresh must never look current when pages failed to parse or the upstream
inventory could not be fully materialized. Collectors write one JSON report
per source under ``outputs/scrape-reports/`` so later gates can fail closed
on ``complete=false`` without re-reading scraper stdout.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "outputs" / "scrape-reports"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_scrape_report(
    source: str,
    *,
    inventory_path: Path | None = None,
    document_count: int = 0,
    ok: int = 0,
    skipped: int = 0,
    errors: list[str] | None = None,
    parser_errors: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    report_dir: Path = REPORT_DIR,
) -> Path:
    """Write ``{source}.json`` and return the report path.

    ``complete`` is true only when there are no fetch/parser errors. A partial
    scrape still records counts so operators can resume, but it must not be
    treated as a current corpus.
    """
    error_list = list(errors or [])
    parser_list = list(parser_errors or [])
    payload: dict[str, Any] = {
        "source": source,
        "generated_at": utc_now(),
        "inventory_path": (
            str(inventory_path.relative_to(ROOT))
            if inventory_path is not None and inventory_path.is_relative_to(ROOT)
            else (str(inventory_path) if inventory_path is not None else None)
        ),
        "inventory_sha256": sha256_file(inventory_path),
        "document_count": int(document_count),
        "ok": int(ok),
        "skipped": int(skipped),
        "error_count": len(error_list),
        "parser_error_count": len(parser_list),
        "errors": error_list[:50],
        "parser_errors": parser_list[:50],
        "complete": not error_list and not parser_list,
    }
    if extra:
        payload["extra"] = extra
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{source}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
