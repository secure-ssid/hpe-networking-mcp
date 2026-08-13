from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import download_indexes

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_parse_checksum_accepts_sha256_file_with_filename():
    checksum = download_indexes._parse_checksum(
        "dcfca1d7c9cd3957d047cef0092c5500a6dc9ed885667f8ea9e4b5fcecce32c9  "
        "dist/hpe-networking-mcp-rag-index-latest.tar.gz\n"
    )

    assert checksum == "dcfca1d7c9cd3957d047cef0092c5500a6dc9ed885667f8ea9e4b5fcecce32c9"


def test_verify_checksum_rejects_mismatch(tmp_path):
    archive = tmp_path / "index.tar.gz"
    archive.write_text("not really a tar")
    checksum = tmp_path / "index.tar.gz.sha256"
    checksum.write_text("0" * 64 + "  index.tar.gz\n")

    with pytest.raises(SystemExit, match="Checksum mismatch"):
        download_indexes._verify_checksum(archive, checksum)


def test_load_pinned_manifest_validates_https_and_digest(tmp_path):
    path = tmp_path / "index-bundle.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": "indexes-v0.8.0",
                "archive": "hpe-networking-mcp-rag-index-v0.8.0.tar.gz",
                "url": (
                    "https://github.com/example/repo/releases/download/indexes-v0.8.0/"
                    "hpe-networking-mcp-rag-index-v0.8.0.tar.gz"
                ),
                "sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    manifest = download_indexes._load_pinned_manifest(path)

    assert manifest["release"] == "indexes-v0.8.0"
    assert manifest["sha256"] == "a" * 64
    assert manifest["checksum_url"].endswith(".tar.gz.sha256")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("url", "http://example.invalid/index.tar.gz", "https"),
        ("sha256", "not-a-digest", "64-character"),
        ("archive", "../index.tar.gz", "plain .tar.gz filename"),
        (
            "url",
            "https://example.invalid/different.tar.gz",
            "filename must match archive",
        ),
    ],
)
def test_load_pinned_manifest_rejects_unsafe_values(tmp_path, field, value, match):
    document = {
        "schema_version": 1,
        "release": "indexes-v0.8.0",
        "archive": "index.tar.gz",
        "url": "https://example.invalid/index.tar.gz",
        "sha256": "a" * 64,
    }
    document[field] = value
    path = tmp_path / "index-bundle.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SystemExit, match=match):
        download_indexes._load_pinned_manifest(path)


def test_verify_expected_checksum_rejects_mismatch(tmp_path):
    archive = tmp_path / "index.tar.gz"
    archive.write_bytes(b"archive")

    with pytest.raises(SystemExit, match="Pinned checksum mismatch"):
        download_indexes._verify_expected_checksum(archive, "0" * 64)


def test_tracked_pinned_manifest_matches_project_version():
    facts = json.loads((REPO_ROOT / "docs" / "project-facts.json").read_text())
    version = facts["package"]["version"]
    manifest = download_indexes._load_pinned_manifest(
        REPO_ROOT / ".github" / "index-bundle.json"
    )

    assert manifest["release"] == f"indexes-v{version}"
    assert manifest["archive"] == f"hpe-networking-mcp-rag-index-v{version}.tar.gz"
    assert f"/{manifest['release']}/{manifest['archive']}" in manifest["url"]


def test_main_downloads_checksum_next_to_archive_by_default(tmp_path, monkeypatch):
    source_data = tmp_path / "source" / "data"
    source_data.mkdir(parents=True)
    (source_data / "INDEX-MANIFEST.json").write_text("{}\n")
    source_archive = tmp_path / "source-index.tar.gz"
    with tarfile.open(source_archive, "w:gz") as tar:
        tar.add(source_data, arcname="data")
    source_checksum = source_archive.with_suffix(source_archive.suffix + ".sha256")
    checksum = download_indexes._sha256(source_archive)
    source_checksum.write_text(
        f"{checksum}  dist/hpe-networking-mcp-rag-index-latest.tar.gz\n"
    )

    def fake_urlretrieve(url: str, destination):
        source = source_checksum if url.endswith(".sha256") else source_archive
        shutil.copyfile(source, destination)

    def fail_extractall(*args, **kwargs):
        raise AssertionError("download_indexes should use the compatibility extractor")

    archive = tmp_path / "dist" / "hpe-networking-mcp-rag-index-latest.tar.gz"
    output_dir = tmp_path / "restore"
    monkeypatch.setattr(download_indexes.urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(tarfile.TarFile, "extractall", fail_extractall)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_indexes.py",
            "--url",
            "https://example.invalid/hpe-networking-mcp-rag-index-latest.tar.gz",
            "--archive",
            str(archive),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert download_indexes.main() == 0

    assert archive.exists()
    assert archive.with_suffix(archive.suffix + ".sha256").exists()
    assert (output_dir / "data" / "INDEX-MANIFEST.json").exists()


def test_main_uses_pinned_manifest_and_verifies_digest(tmp_path, monkeypatch):
    source_data = tmp_path / "source" / "data"
    source_data.mkdir(parents=True)
    (source_data / "INDEX-MANIFEST.json").write_text("{}\n")
    source_archive = tmp_path / "source-index.tar.gz"
    with tarfile.open(source_archive, "w:gz") as tar:
        tar.add(source_data, arcname="data")
    digest = download_indexes._sha256(source_archive)
    source_checksum = source_archive.with_suffix(source_archive.suffix + ".sha256")
    source_checksum.write_text(f"{digest}  source-index.tar.gz\n")
    manifest = tmp_path / "index-bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": "indexes-v0.8.0",
                "archive": "pinned-index.tar.gz",
                "url": "https://example.invalid/pinned-index.tar.gz",
                "checksum_url": "https://example.invalid/pinned-index.tar.gz.sha256",
                "sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    downloaded_archive = tmp_path / "downloaded" / "pinned-index.tar.gz"

    def fake_urlretrieve(url: str, destination):
        source = source_checksum if url.endswith(".sha256") else source_archive
        shutil.copyfile(source, destination)

    output_dir = tmp_path / "restore"
    monkeypatch.setattr(download_indexes.urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_indexes.py",
            "--manifest",
            str(manifest),
            "--archive",
            str(downloaded_archive),
            "--output-dir",
            str(output_dir),
        ],
    )
    assert download_indexes.main() == 0
    assert (output_dir / "data" / "INDEX-MANIFEST.json").exists()
    assert (output_dir / "data" / "INDEX-MANIFEST.json").exists()
    assert downloaded_archive.exists()
    assert downloaded_archive.with_suffix(downloaded_archive.suffix + ".sha256").exists()


def test_extract_data_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = b"unsafe"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))

    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(SystemExit, match="Unsafe archive member path"):
            download_indexes._extract_data_archive(tar, tmp_path / "restore")

    assert not (tmp_path / "escape.txt").exists()


def test_extract_data_archive_rejects_symlink_members(tmp_path):
    archive = tmp_path / "unsafe-link.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("data/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        tar.addfile(member)

    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(SystemExit, match="Unsafe archive member type"):
            download_indexes._extract_data_archive(tar, tmp_path / "restore")


def test_swap_into_place_replaces_artifact_entirely(tmp_path):
    """A whole artifact (e.g. docs.lance/) is replaced completely, so stale
    files inside the live copy that are absent from the new one do not
    survive."""
    data_dir = tmp_path / "data"
    live_artifact = data_dir / "docs.lance"
    live_artifact.mkdir(parents=True)
    (live_artifact / "stale.txt").write_text("old")

    staging = tmp_path / "staging" / "data"
    new_artifact = staging / "docs.lance"
    new_artifact.mkdir(parents=True)
    (new_artifact / "fresh.txt").write_text("new")

    download_indexes._swap_into_place(staging, data_dir)

    assert (data_dir / "docs.lance" / "fresh.txt").read_text() == "new"
    assert not (data_dir / "docs.lance" / "stale.txt").exists()
    assert not list(data_dir.glob("*.old-tmp"))


def test_swap_into_place_rolls_back_on_move_failure(tmp_path, monkeypatch):
    """If moving the new artifact fails, the previous live artifact is
    restored — never left deleted."""
    data_dir = tmp_path / "data"
    live_artifact = data_dir / "docs.lance"
    live_artifact.mkdir(parents=True)
    (live_artifact / "keep.txt").write_text("original")

    staging = tmp_path / "staging" / "data"
    new_artifact = staging / "docs.lance"
    new_artifact.mkdir(parents=True)
    (new_artifact / "fresh.txt").write_text("new")

    def _boom(src, dst):
        raise RuntimeError("simulated move failure")

    monkeypatch.setattr(download_indexes.shutil, "move", _boom)

    with pytest.raises(RuntimeError, match="simulated move failure"):
        download_indexes._swap_into_place(staging, data_dir)

    # Live artifact restored intact; no dangling .old-tmp backup.
    assert (data_dir / "docs.lance" / "keep.txt").read_text() == "original"
    assert not list(data_dir.glob("*.old-tmp"))
