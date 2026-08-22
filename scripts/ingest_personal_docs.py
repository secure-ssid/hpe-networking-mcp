#!/usr/bin/env python3
"""Ingest local personal documents for the router's ``search_internal_docs`` tool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.optional_deps import MissingOptionalDependency  # noqa: E402
from hpe_networking_mcp.pipeline.personal_ingest import (  # noqa: E402
    IngestResult,
    ingest_folder,
)


def _payload(collection: str, result: IngestResult) -> dict[str, object]:
    return {
        "collection": collection,
        "files_seen": result.files_seen,
        "files_ingested": result.files_ingested,
        "files_skipped_unchanged": result.files_skipped_unchanged,
        "files_skipped_duplicate": result.files_skipped_duplicate,
        "files_skipped_unsupported": result.files_skipped_unsupported,
        "files_failed": result.files_failed,
        "chunks_written": result.chunks_written,
        "errors": result.errors or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest local documents into the private personal LanceDB index."
    )
    parser.add_argument("folder", type=Path, help="Folder containing documents to ingest")
    parser.add_argument(
        "--collection",
        default="internal",
        help="Collection name (lowercase letters, digits, and underscores)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    args = parser.parse_args()

    folder = args.folder.expanduser()
    if not folder.is_dir():
        print(f"not a directory: {folder}", file=sys.stderr)
        return 2

    def progress(message: str) -> None:
        if not args.json:
            print(message)

    try:
        result = ingest_folder(folder, collection=args.collection, progress=progress)
    except MissingOptionalDependency as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2

    payload = _payload(args.collection, result)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"ingested {result.files_ingested} file(s), "
            f"{result.chunks_written} chunk(s) -> collection '{args.collection}'"
        )
        print(
            f"seen={result.files_seen} unchanged={result.files_skipped_unchanged} "
            f"duplicate={result.files_skipped_duplicate} "
            f"unsupported={result.files_skipped_unsupported} "
            f"failed={result.files_failed}"
        )
        for path, error in (result.errors or {}).items():
            print(f"failed: {path}: {error}", file=sys.stderr)
    return 0 if result.files_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
