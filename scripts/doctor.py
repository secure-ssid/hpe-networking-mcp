#!/usr/bin/env python3
"""Source-checkout wrapper around ``hpe-mcp-doctor``.

The implementation lives in :mod:`hpe_networking_mcp.cli.doctor` so it ships
with the package and backs the ``hpe-mcp-doctor`` console script. This
wrapper only exists so ``python scripts/doctor.py`` keeps working from a raw
checkout that has not been installed yet.

Prefer ``uv run hpe-mcp-doctor``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hpe_networking_mcp.cli.doctor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
