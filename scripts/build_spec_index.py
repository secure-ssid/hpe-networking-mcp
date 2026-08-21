"""Build the SQLite/FTS5 spec index from the committed OpenAPI corpus.

No network, no scrape, no embeddings: ``vendor/openapi`` ships every document
the index needs, so a fresh clone can answer ``lookup_api`` immediately.

The indexing itself is deliberately not reimplemented here.
``specs_index.build`` already writes every table the query layer reads --
``endpoints``, ``schemas``, ``fields``, ``responses`` and the ``fts`` virtual
table that ``search``/``lookup`` MATCH against -- resolves each spec's
platform, version and source URL from
``ingestion/openapi_registry_manifest.json``, and swaps the finished database
into place atomically. A second builder beside it would have to track those
tables and that metadata forever, and an index missing ``fts`` returns nothing
for every search while its row counts look healthy. This is a front door onto
the real builder, not a copy of it.

Build:  python scripts/build_spec_index.py [output.sqlite]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hpe_networking_mcp.pipeline.clients.specs_index import build

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "vendor" / "openapi"
DEFAULT_OUTPUT = ROOT / "data" / "specs.sqlite"

# The contract Task 5 and Task 6 consume. ``specs_index.build`` also reports
# ``responses`` and ``skipped``; both are real and both stay out of the
# contract -- ``skipped`` counts the corpus's own ``MANIFEST.json``, which
# holds no OpenAPI paths.
_CONTRACT_KEYS = ("specs", "endpoints", "schemas", "fields")


def build_spec_index(vendor_dir: Path, output: Path) -> dict[str, int]:
    """Index the vendored OpenAPI corpus at ``vendor_dir`` into ``output``.

    Returns the indexed record counts keyed by ``specs``, ``endpoints``,
    ``schemas`` and ``fields``.

    Raises:
        FileNotFoundError: ``vendor_dir`` holds no ``MANIFEST.json``, so it is
            not a vendored corpus. Checked before any work, so a mistyped path
            fails immediately rather than after producing an empty database.
        RuntimeError: the corpus parsed but yielded no records.
    """
    vendor_dir = Path(vendor_dir)
    output = Path(output)
    manifest_path = vendor_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"{manifest_path} — corpus not vendored")
    counts = build(specs_dir=vendor_dir, db_path=output)
    return {key: counts[key] for key in _CONTRACT_KEYS}


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    print(json.dumps(build_spec_index(VENDOR_DIR, output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
