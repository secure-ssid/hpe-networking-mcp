#!/usr/bin/env python3
"""Build the local SKU/part-number catalog from its reviewed seed snapshot.

The resulting ``data/hardware_catalog.sqlite`` is intentionally ignored like
the other local indexes. Query-time MCP tools never make vendor web calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hpe_networking_mcp.pipeline.clients.hardware_catalog import DB_PATH, SEED_PATH, build


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DB_PATH
    print(json.dumps(build(seed_path=SEED_PATH, db_path=output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
