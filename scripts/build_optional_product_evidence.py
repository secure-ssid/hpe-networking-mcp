#!/usr/bin/env python3
"""Write one compatibility/evidence artifact per optional product backend.

Usage::

    uv run python scripts/build_optional_product_evidence.py
    uv run python scripts/build_optional_product_evidence.py --platform apstra
    uv run python scripts/build_optional_product_evidence.py \\
        --output-dir outputs/optional-product-evidence

Always builds an offline, git-history-anchored compatibility artifact for
each platform (see ``src/hpe_networking_mcp/pipeline/optional_product_evidence.py``). Additionally
builds a bounded, read-only live-evidence artifact for a platform only when
``HPE_MCP_LIVE_TEST_<PLATFORM>_READ=1`` is set *and* that platform's
credentials are configured (see ``src/hpe_networking_mcp/pipeline/live_test_config.py``) -- neither
is ever enabled by this script itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import optional_product_evidence as evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=sorted(evidence.OPTIONAL_PLATFORMS),
        help="build evidence for one platform (default: all six)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=evidence.DEFAULT_OUTPUT_DIR,
        help="destination directory for evidence artifacts",
    )
    args = parser.parse_args()

    platforms = [args.platform] if args.platform else list(evidence.OPTIONAL_PLATFORMS)
    for platform in platforms:
        entries = evidence.write_backend_evidence(platform, output_dir=args.output_dir)
        for entry in entries:
            print(f"Wrote {args.output_dir / entry.filename} ({entry.kind}, {entry.size_bytes}B).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
