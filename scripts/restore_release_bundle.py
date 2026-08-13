#!/usr/bin/env python3
"""Restore and smoke-validate one packaged release-artifacts bundle.

Thin CLI wrapper around ``hpe_networking_mcp.pipeline.release_restore.smoke_test_bundle``.
Extracts the archive into a throwaway temporary directory (never into the
repository tree), verifies its checksum, rejects any unsafe/traversal
member, enforces file-count/size bounds, schema-validates every
contract-typed JSON file listed in the bundle's own release manifest, and
always cleans up -- regardless of pass or fail.

Exit code is non-zero if the archive fails checksum verification, contains
an unsafe member, exceeds a safety bound, or fails schema/structural
validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hpe_networking_mcp.pipeline import release_restore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Path to the .tar.gz release bundle")
    parser.add_argument(
        "--checksum-file",
        type=Path,
        default=None,
        help="Path to the .sha256 checksum file (defaults to '<archive>.sha256' if present)",
    )
    parser.add_argument(
        "--required-prefix",
        default=None,
        help="Require every archive member to start with this top-level path component",
    )
    args = parser.parse_args(argv)

    if not args.archive.is_file():
        print(f"error: archive not found: {args.archive}", file=sys.stderr)
        return 1

    try:
        report = release_restore.smoke_test_bundle(
            args.archive,
            checksum_file=args.checksum_file,
            required_prefix=args.required_prefix,
        )
    except release_restore.RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Archive:                {report.archive}")
    print(f"Members extracted:      {report.member_count}")
    print(f"Total bytes:            {report.total_bytes}")
    print(f"Validated contract files ({len(report.validated_contract_files)}):")
    for name in report.validated_contract_files:
        print(f"  - {name}")
    print(f"Structural checks ({len(report.structural_checks)}):")
    for name in report.structural_checks:
        print(f"  - {name}")
    print("Restore smoke test passed; temporary extraction directory cleaned up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
