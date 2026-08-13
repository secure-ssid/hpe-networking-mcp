#!/usr/bin/env python3
"""Download and unpack the latest prebuilt hpe-networking-mcp RAG/OpenAPI indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://github.com/secure-ssid/hpe-networking-mcp/releases/latest/download/"
    "hpe-networking-mcp-rag-index-latest.tar.gz"
)
PINNED_MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksum(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        checksum = stripped.split()[0].lower()
        if len(checksum) == 64 and all(char in "0123456789abcdef" for char in checksum):
            return checksum
        raise ValueError(f"Invalid checksum line: {line!r}")
    raise ValueError("Checksum file is empty")


def _validate_sha256(value: str, *, label: str) -> str:
    checksum = value.strip().lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError(f"{label} must be a 64-character SHA-256 digest")
    return checksum


def _load_pinned_manifest(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read pinned index manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"Pinned index manifest {path} must contain a JSON object")
    if document.get("schema_version") != PINNED_MANIFEST_SCHEMA_VERSION:
        raise SystemExit(
            f"Pinned index manifest {path} schema_version "
            f"{document.get('schema_version')!r} != {PINNED_MANIFEST_SCHEMA_VERSION}"
        )

    required = ("release", "archive", "url", "sha256")
    missing = [key for key in required if not document.get(key)]
    if missing:
        raise SystemExit(
            f"Pinned index manifest {path} is missing: {', '.join(missing)}"
        )

    url = str(document["url"])
    checksum_url = str(document.get("checksum_url") or f"{url}.sha256")
    if not url.startswith("https://") or not checksum_url.startswith("https://"):
        raise SystemExit("Pinned index manifest URLs must use https://")
    archive = Path(str(document["archive"]))
    if archive.name != str(document["archive"]) or archive.suffixes[-2:] != [".tar", ".gz"]:
        raise SystemExit("Pinned index manifest archive must be a plain .tar.gz filename")
    if PurePosixPath(urlparse(url).path).name != archive.name:
        raise SystemExit("Pinned index manifest URL filename must match archive")
    try:
        digest = _validate_sha256(str(document["sha256"]), label="manifest sha256")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return {
        "release": str(document["release"]),
        "archive": archive.name,
        "url": url,
        "checksum_url": checksum_url,
        "sha256": digest,
    }


def _verify_checksum(archive: Path, checksum_file: Path) -> None:
    expected = _parse_checksum(checksum_file.read_text())
    actual = _sha256(archive)
    if actual != expected:
        raise SystemExit(
            f"Checksum mismatch for {archive}: expected {expected}, got {actual}"
        )


def _verify_expected_checksum(archive: Path, expected: str) -> None:
    expected = _validate_sha256(expected, label="expected sha256")
    actual = _sha256(archive)
    if actual != expected:
        raise SystemExit(
            f"Pinned checksum mismatch for {archive}: expected {expected}, got {actual}"
        )


def _member_target(member: tarfile.TarInfo, output_dir: Path) -> Path:
    name = PurePosixPath(member.name)
    parts = name.parts
    if name.is_absolute() or not parts or ".." in parts or parts[0] != "data":
        raise SystemExit(f"Unsafe archive member path: {member.name!r}")

    output_root = output_dir.resolve(strict=False)
    target = output_root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(output_root)
    except ValueError as exc:
        raise SystemExit(f"Unsafe archive member path: {member.name!r}") from exc
    return target


def _extract_data_archive(tar: tarfile.TarFile, output_dir: Path) -> None:
    for member in tar:
        target = _member_target(member, output_dir)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise SystemExit(f"Unsafe archive member type: {member.name!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            raise SystemExit(f"Could not read archive member: {member.name!r}")
        with source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _swap_into_place(staging_data: Path, data_dir: Path) -> None:
    """Move each extracted artifact over its live counterpart via renames.

    Extracting straight into the live ``data/`` interleaved old and new
    files: an interrupted run corrupted the Lance tables, and stale local
    files not present in the archive (e.g. higher-numbered Lance version
    manifests from local ingests) survived the extract — so a "successful"
    download could keep serving the old index. Renaming whole artifacts
    replaces them completely, and the old copy is only deleted after the new
    one is in place.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(staging_data.iterdir()):
        live = data_dir / entry.name
        old = data_dir / f"{entry.name}.old-tmp"
        _remove(old)
        moved_aside = False
        if live.exists() or live.is_symlink():
            # Same-directory rename — always same-filesystem as live.
            os.rename(live, old)
            moved_aside = True
        try:
            # shutil.move copies when staging and data/ are on different
            # filesystems (os.rename would raise EXDEV — and an exception
            # here used to cascade into deleting BOTH the new copy, via the
            # staging cleanup, and the .old-tmp backup on the next run).
            shutil.move(str(entry), str(live))
        except BaseException:
            if moved_aside and not (live.exists() or live.is_symlink()):
                os.rename(old, live)
            raise
        _remove(old)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "Repository-pinned JSON manifest containing release, archive, URL, "
            "checksum URL, and SHA-256. The pinned digest is always verified."
        ),
    )
    parser.add_argument("--url", default=None, help="Release asset URL")
    parser.add_argument(
        "--checksum-url",
        default=None,
        help="Checksum URL. Defaults to <url>.sha256 unless --skip-checksum is set.",
    )
    parser.add_argument(
        "--checksum-file",
        type=Path,
        default=None,
        help="Where to store the downloaded checksum file",
    )
    parser.add_argument(
        "--skip-checksum",
        action="store_true",
        help=(
            "Skip the downloaded .sha256 sidecar. A digest supplied by "
            "--manifest or --expected-sha256 is still verified."
        ),
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="Pinned archive SHA-256 (verified independently of any downloaded sidecar)",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Where to store the downloaded archive",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT,
        help="Directory where the archive is unpacked",
    )
    args = parser.parse_args()

    pinned = _load_pinned_manifest(args.manifest) if args.manifest else None
    if pinned and args.url:
        raise SystemExit("--manifest cannot be combined with --url")
    if pinned and args.expected_sha256:
        raise SystemExit("--manifest cannot be combined with --expected-sha256")

    url = pinned["url"] if pinned else (args.url or DEFAULT_URL)
    expected_sha256 = pinned["sha256"] if pinned else args.expected_sha256
    archive = args.archive or (
        ROOT / "dist" / (pinned["archive"] if pinned else Path(url).name)
    )
    checksum_url = args.checksum_url or (
        pinned["checksum_url"] if pinned else f"{url}.sha256"
    )

    archive.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, archive)

    if expected_sha256:
        print(f"Verifying repository-pinned digest for {archive}")
        _verify_expected_checksum(archive, expected_sha256)

    if not args.skip_checksum:
        checksum_file = args.checksum_file or archive.with_suffix(
            archive.suffix + ".sha256"
        )
        checksum_file.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {checksum_url}")
        urllib.request.urlretrieve(checksum_url, checksum_file)
        print(f"Verifying {archive}")
        _verify_checksum(archive, checksum_file)

    print(f"Unpacking {archive} into {args.output_dir}")
    # Extract into a staging dir on the same filesystem, then swap artifacts
    # into the live data/ via renames — never write into live data/ directly,
    # so an interrupted unpack leaves the previous good index in place.
    staging_root = args.output_dir / ".index-download-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _extract_data_archive(tar, staging_root)
        staging_data = staging_root / "data"
        if not staging_data.is_dir():
            raise SystemExit("Archive contained no data/ directory")
        _swap_into_place(staging_data, args.output_dir / "data")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    print("Indexes restored under data/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
