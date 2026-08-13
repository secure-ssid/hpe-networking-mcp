"""Unit tests for hpe_networking_mcp.pipeline.release_restore -- safe restore and
smoke-validation of packaged release bundles.

Covers:
- Happy path: a real deterministic archive (built with
  ``hpe_networking_mcp.pipeline.release_packaging``) restores, validates, and cleans up.
- Checksum verification success and tamper rejection.
- Path-traversal / absolute-path / required-prefix-mismatch rejection.
- Non-regular-file member rejection (symlinks).
- Member-count / per-file-size / total-size bound rejection.
- Guarded-directory / repository-root extraction refusal.
- Manifest-driven schema validation: missing manifest, missing entry
  file, checksum/size mismatch, and schema-invalid payload all raise.
- ``smoke_test_bundle`` never leaves a temp directory behind, even on
  failure.

No network calls; every archive used here is built locally in ``tmp_path``.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import release_packaging as rp
from hpe_networking_mcp.pipeline import release_restore as rr

GENERATED_AT = "2026-07-25T12:00:00+00:00"


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, *, mtime: int = 0) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(data))


def _add_dir(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    tar.addfile(info)


def _make_raw_archive(path: Path, builder) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        builder(tar)
    return path


@pytest.fixture
def validation_matrix_payload():
    return {
        "generated_at": GENERATED_AT,
        "entries": [
            {
                "category": "central_monitoring",
                "classification": "offline_fixture",
                "detail": "offline evaluator fixture",
            }
        ],
    }


@pytest.fixture
def real_bundle(tmp_path, validation_matrix_payload):
    """A minimal, real, schema-valid extracted-bundle-shaped staging dir,
    packaged via hpe_networking_mcp.pipeline.release_packaging into a real archive+checksum,
    for exercising the full happy path."""
    staging = tmp_path / "staging"
    staging.mkdir()

    matrix_path = staging / "evidence" / "validation-matrix.json"
    matrix_path.parent.mkdir(parents=True)
    manifest_entry = contracts.write_artifact(
        matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
    )

    sbom_path = staging / "sbom.json"
    sbom_path.write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}),
        encoding="utf-8",
    )

    manifest_payload = {
        "release_version": "v0.0.0-test",
        "generated_at": GENERATED_AT,
        "entries": [manifest_entry],
    }
    manifest_path = staging / "release-manifest.json"
    contracts.write_artifact(manifest_path, contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload)

    provenance_path = staging / "provenance.json"
    provenance_path.write_text(
        json.dumps(
            rp.build_provenance_manifest(
                version="v0.0.0-test",
                subjects={
                    "validation-matrix.json": rp.sha256_file(matrix_path),
                    "sbom.json": rp.sha256_file(sbom_path),
                    "release-manifest.json": rp.sha256_file(manifest_path),
                },
                generated_at=GENERATED_AT,
            )
        ),
        encoding="utf-8",
    )

    rp.write_checksums_file(staging)

    archive_path = tmp_path / "bundle.tar.gz"
    digest = rp.build_deterministic_archive(staging, archive_path, arcname_prefix="bundle")
    checksum_path = tmp_path / "bundle.tar.gz.sha256"
    checksum_path.write_text(f"{digest}  bundle.tar.gz\n", encoding="utf-8")
    return archive_path, checksum_path


class TestHappyPath:
    def test_smoke_test_bundle_succeeds_and_validates_contract_files(self, real_bundle):
        archive_path, _checksum_path = real_bundle
        report = rr.smoke_test_bundle(archive_path)
        assert report.member_count > 0
        assert "validation-matrix.json" in report.validated_contract_files
        assert "sbom.json" in report.structural_checks

    def test_smoke_test_bundle_leaves_no_temp_directory_behind(self, real_bundle):
        archive_path, _checksum_path = real_bundle
        before = set(Path(tempfile.gettempdir()).glob("hpe-networking-mcp-restore-smoke-*"))
        rr.smoke_test_bundle(archive_path)
        after = set(Path(tempfile.gettempdir()).glob("hpe-networking-mcp-restore-smoke-*"))
        assert after == before

    def test_extract_archive_respects_required_prefix(self, real_bundle, tmp_path):
        archive_path, _checksum_path = real_bundle
        out_dir = tmp_path / "out"
        member_count, total_bytes = rr.extract_archive(
            archive_path, out_dir, required_prefix="bundle"
        )
        assert member_count > 0
        assert total_bytes > 0
        assert (out_dir / "bundle" / "release-manifest.json").is_file()


class TestChecksumVerification:
    def test_matching_checksum_passes(self, real_bundle):
        archive_path, checksum_path = real_bundle
        rr.verify_checksum(archive_path, checksum_path)  # must not raise

    def test_tampered_archive_is_rejected(self, real_bundle):
        archive_path, checksum_path = real_bundle
        with archive_path.open("ab") as handle:
            handle.write(b"\x00tamper")
        with pytest.raises(rr.RestoreError, match="checksum mismatch"):
            rr.verify_checksum(archive_path, checksum_path)

    def test_smoke_test_bundle_rejects_tampered_archive(self, real_bundle):
        archive_path, _checksum_path = real_bundle
        with archive_path.open("ab") as handle:
            handle.write(b"\x00tamper")
        with pytest.raises(rr.RestoreError, match="checksum mismatch"):
            rr.smoke_test_bundle(archive_path)

    def test_empty_checksum_file_rejected(self, tmp_path):
        with pytest.raises(rr.RestoreError, match="empty"):
            rr.parse_checksum_line("")

    def test_malformed_checksum_line_rejected(self):
        with pytest.raises(rr.RestoreError, match="invalid checksum line"):
            rr.parse_checksum_line("not-a-valid-hex-digest  file.txt")


class TestPathTraversalAndUnsafeMembers:
    def test_parent_traversal_member_rejected(self, tmp_path):
        archive = _make_raw_archive(
            tmp_path / "evil.tar.gz",
            lambda tar: _add_bytes(tar, "../../etc/passwd", b"pwned"),
        )
        with pytest.raises(rr.RestoreError, match="unsafe archive member path"):
            rr.extract_archive(archive, tmp_path / "out")

    def test_absolute_path_member_rejected(self, tmp_path):
        archive = _make_raw_archive(
            tmp_path / "evil.tar.gz",
            lambda tar: _add_bytes(tar, "/etc/passwd", b"pwned"),
        )
        with pytest.raises(rr.RestoreError, match="unsafe archive member path"):
            rr.extract_archive(archive, tmp_path / "out")

    def test_required_prefix_mismatch_rejected(self, tmp_path):
        archive = _make_raw_archive(
            tmp_path / "wrong-prefix.tar.gz",
            lambda tar: _add_bytes(tar, "other/file.json", b"{}"),
        )
        with pytest.raises(rr.RestoreError, match="does not start with required prefix"):
            rr.extract_archive(archive, tmp_path / "out", required_prefix="bundle")

    def test_symlink_member_rejected(self, tmp_path):
        def builder(tar):
            info = tarfile.TarInfo(name="link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)

        archive = _make_raw_archive(tmp_path / "evil-symlink.tar.gz", builder)
        with pytest.raises(rr.RestoreError, match="unsafe archive member type"):
            rr.extract_archive(archive, tmp_path / "out")

    def test_directory_members_are_created(self, tmp_path):
        def builder(tar):
            _add_dir(tar, "bundle")
            _add_bytes(tar, "bundle/a.json", b"{}")

        archive = _make_raw_archive(tmp_path / "with-dir.tar.gz", builder)
        out_dir = tmp_path / "out"
        rr.extract_archive(archive, out_dir)
        assert (out_dir / "bundle").is_dir()
        assert (out_dir / "bundle" / "a.json").is_file()


class TestBounds:
    def test_member_count_bound_rejected(self, tmp_path):
        def builder(tar):
            for i in range(5):
                _add_bytes(tar, f"file-{i}.json", b"{}")

        archive = _make_raw_archive(tmp_path / "many.tar.gz", builder)
        with pytest.raises(rr.RestoreError, match="member-count bound"):
            rr.extract_archive(
                archive, tmp_path / "out", bounds=rr.RestoreBounds(max_members=2)
            )

    def test_per_file_size_bound_rejected(self, tmp_path):
        archive = _make_raw_archive(
            tmp_path / "big-file.tar.gz",
            lambda tar: _add_bytes(tar, "big.bin", b"x" * 1000),
        )
        with pytest.raises(rr.RestoreError, match="per-file bound"):
            rr.extract_archive(
                archive, tmp_path / "out", bounds=rr.RestoreBounds(max_member_bytes=100)
            )

    def test_total_size_bound_rejected(self, tmp_path):
        def builder(tar):
            _add_bytes(tar, "a.bin", b"x" * 60)
            _add_bytes(tar, "b.bin", b"x" * 60)

        archive = _make_raw_archive(tmp_path / "total-big.tar.gz", builder)
        with pytest.raises(rr.RestoreError, match="bound$"):
            rr.extract_archive(
                archive,
                tmp_path / "out",
                bounds=rr.RestoreBounds(max_member_bytes=1000, max_total_bytes=100),
            )

    def test_bounds_checked_before_any_bytes_written(self, tmp_path):
        def builder(tar):
            _add_bytes(tar, "ok.json", b"{}")
            _add_bytes(tar, "too-big.bin", b"x" * 1000)

        archive = _make_raw_archive(tmp_path / "partial.tar.gz", builder)
        out_dir = tmp_path / "out"
        with pytest.raises(rr.RestoreError, match="per-file bound"):
            rr.extract_archive(
                archive, out_dir, bounds=rr.RestoreBounds(max_member_bytes=100)
            )
        # Bounds are validated for every member before any file is written.
        assert not (out_dir / "ok.json").exists()


class TestGuardedOutputDirectories:
    def test_refuses_repository_root(self):
        with pytest.raises(rr.RestoreError, match="repository root"):
            rr._assert_safe_output_dir(rr.REPO_ROOT)

    @pytest.mark.parametrize("guarded", sorted(rr._GUARDED_TOP_LEVEL_NAMES))
    def test_refuses_guarded_top_level_dirs(self, guarded):
        with pytest.raises(rr.RestoreError, match="guarded repository path"):
            rr._assert_safe_output_dir(rr.REPO_ROOT / guarded / "nested")

    def test_allows_directory_outside_repository(self, tmp_path):
        resolved = rr._assert_safe_output_dir(tmp_path / "restore-target")
        assert resolved == (tmp_path / "restore-target").resolve()


def _entry_dict(entry: contracts.ManifestEntry, **overrides) -> dict:
    payload = contracts.to_json_dict(entry)
    payload.update(overrides)
    return payload


class TestManifestDrivenValidation:
    def test_missing_manifest_rejected(self, tmp_path):
        root = tmp_path / "extracted"
        root.mkdir()
        (root / "sbom.json").write_text("{}", encoding="utf-8")
        with pytest.raises(rr.RestoreError, match="missing a top-level release-manifest.json"):
            rr.validate_extracted_bundle(root)

    def test_missing_entry_file_rejected(self, tmp_path, validation_matrix_payload):
        root = tmp_path / "extracted"
        root.mkdir()
        matrix_path = root / "validation-matrix.json"
        entry = contracts.write_artifact(
            matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
        )
        matrix_path.unlink()  # the manifest references a file that no longer exists
        manifest_payload = {
            "release_version": "v0.0.0-test",
            "generated_at": GENERATED_AT,
            "entries": [_entry_dict(entry, filename="missing.json")],
        }
        contracts.write_artifact(
            root / "release-manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload
        )
        with pytest.raises(rr.RestoreError, match="not found in extracted bundle"):
            rr.validate_extracted_bundle(root)

    def test_checksum_mismatch_rejected(self, tmp_path, validation_matrix_payload):
        root = tmp_path / "extracted"
        root.mkdir()
        matrix_path = root / "validation-matrix.json"
        entry = contracts.write_artifact(
            matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
        )
        manifest_payload = {
            "release_version": "v0.0.0-test",
            "generated_at": GENERATED_AT,
            "entries": [_entry_dict(entry, sha256="0" * 64)],  # deliberately wrong
        }
        contracts.write_artifact(
            root / "release-manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload
        )
        with pytest.raises(rr.RestoreError, match="sha256 mismatch"):
            rr.validate_extracted_bundle(root)

    def test_size_mismatch_rejected(self, tmp_path, validation_matrix_payload):
        root = tmp_path / "extracted"
        root.mkdir()
        matrix_path = root / "validation-matrix.json"
        entry = contracts.write_artifact(
            matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
        )
        manifest_payload = {
            "release_version": "v0.0.0-test",
            "generated_at": GENERATED_AT,
            "entries": [_entry_dict(entry, size_bytes=1)],  # deliberately wrong
        }
        contracts.write_artifact(
            root / "release-manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload
        )
        with pytest.raises(rr.RestoreError, match="size mismatch"):
            rr.validate_extracted_bundle(root)

    def test_schema_invalid_payload_rejected(self, tmp_path, validation_matrix_payload):
        root = tmp_path / "extracted"
        root.mkdir()
        matrix_path = root / "validation-matrix.json"
        entry = contracts.write_artifact(
            matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
        )
        # Overwrite the file in place with a schema-invalid payload, but keep
        # the manifest's original (now-stale) checksum/size out of the way by
        # recomputing them so we hit schema validation, not checksum mismatch.
        bad_payload = json.dumps({"not": "a valid payload"}).encode("utf-8")
        matrix_path.write_bytes(bad_payload)
        manifest_payload = {
            "release_version": "v0.0.0-test",
            "generated_at": GENERATED_AT,
            "entries": [
                _entry_dict(
                    entry,
                    sha256=rr.sha256_file(matrix_path),
                    size_bytes=matrix_path.stat().st_size,
                )
            ],
        }
        contracts.write_artifact(
            root / "release-manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload
        )
        with pytest.raises(rr.RestoreError, match="failed schema validation"):
            rr.validate_extracted_bundle(root)

    def test_sbom_structural_check_rejects_non_cyclonedx(self, tmp_path, validation_matrix_payload):
        root = tmp_path / "extracted"
        root.mkdir()
        matrix_path = root / "validation-matrix.json"
        entry = contracts.write_artifact(
            matrix_path, contracts.VALIDATION_MATRIX_RESULT, validation_matrix_payload
        )
        manifest_payload = {
            "release_version": "v0.0.0-test",
            "generated_at": GENERATED_AT,
            "entries": [_entry_dict(entry)],
        }
        contracts.write_artifact(
            root / "release-manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload
        )
        (root / "sbom.json").write_text(json.dumps({"not": "cyclonedx"}), encoding="utf-8")
        with pytest.raises(rr.RestoreError, match="sbom.json failed structural sanity check"):
            rr.validate_extracted_bundle(root)
