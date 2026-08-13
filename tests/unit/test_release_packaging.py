"""Unit tests for hpe_networking_mcp.pipeline.release_packaging -- deterministic release-bundle
packaging primitives.

Covers:
- ``read_source_freshness_payload``: returns None when absent, returns a
  validated payload when present, and never fetches/regenerates anything.
- ``build_provenance_manifest``: shape, subject-count bound, and the
  explicit "not a signed attestation" note.
- ``write_checksums_file`` / ``build_deterministic_archive``: sha256sum-
  format output, sorted/deterministic member order, fixed archive
  metadata, and byte-identical output across two builds of the same
  staged directory.
- Missing-optional-input handling (no source-freshness snapshot present).

Full end-to-end bundle assembly (``scripts/build_release_bundle.py``,
which depends on ``scripts.*`` evidence generators) is covered separately
in ``tests/unit/test_build_release_bundle.py``.
"""

from __future__ import annotations

import json
import tarfile
import time

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import release_packaging as rp

GENERATED_AT = "2026-07-25T12:00:00+00:00"


class TestSourceFreshness:
    def test_missing_snapshot_returns_none(self, tmp_path):
        assert rp.read_source_freshness_payload(tmp_path / "missing.json") is None

    def test_existing_snapshot_is_read_and_validated(self, tmp_path):
        path = tmp_path / "source-freshness.json"
        payload = {
            "generated_at": GENERATED_AT,
            "entries": [{"source": "aruba_advisories", "count": 95, "minimum": 90}],
        }
        contracts.write_artifact(path, contracts.SOURCE_FRESHNESS_RESULT, payload)

        result = rp.read_source_freshness_payload(path)
        assert result is not None
        assert result["entries"][0]["source"] == "aruba_advisories"

    def test_invalid_snapshot_raises(self, tmp_path):
        path = tmp_path / "source-freshness.json"
        path.write_text(json.dumps({"generated_at": GENERATED_AT}), encoding="utf-8")
        with pytest.raises(contracts.ArtifactValidationError):
            rp.read_source_freshness_payload(path)


class TestProvenanceManifest:
    def test_shape_and_not_an_attestation_note(self):
        doc = rp.build_provenance_manifest(
            version="v0.7.0",
            subjects={"bundle.tar.gz": "a" * 64},
            generated_at=GENERATED_AT,
        )
        assert doc["release_version"] == "v0.7.0"
        assert doc["generated_at"] == GENERATED_AT
        assert doc["subjects"] == [{"name": "bundle.tar.gz", "sha256": "a" * 64}]
        assert "not a cryptographically signed attestation" in doc["note"]
        json.dumps(doc)  # must be JSON-serializable

    def test_subjects_sorted_by_name(self):
        doc = rp.build_provenance_manifest(
            version="v0.7.0",
            subjects={"z.json": "b" * 64, "a.json": "a" * 64},
            generated_at=GENERATED_AT,
        )
        names = [subject["name"] for subject in doc["subjects"]]
        assert names == ["a.json", "z.json"]

    def test_empty_subjects_rejected(self):
        with pytest.raises(rp.ReleasePackagingError, match="at least one subject"):
            rp.build_provenance_manifest(version="v0.7.0", subjects={})

    def test_subject_count_over_bound_rejected(self):
        subjects = {f"file-{i}.json": "a" * 64 for i in range(rp.MAX_PROVENANCE_SUBJECTS + 1)}
        with pytest.raises(rp.ReleasePackagingError, match="exceeding the"):
            rp.build_provenance_manifest(version="v0.7.0", subjects=subjects)

    def test_builder_id_is_local_outside_github_actions(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        doc = rp.build_provenance_manifest(
            version="v0.7.0", subjects={"a.json": "a" * 64}, generated_at=GENERATED_AT
        )
        assert doc["builder"]["id"] == "local"

    def test_builder_id_is_github_actions_when_env_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        doc = rp.build_provenance_manifest(
            version="v0.7.0", subjects={"a.json": "a" * 64}, generated_at=GENERATED_AT
        )
        assert doc["builder"]["id"] == "github-actions"


class TestChecksumsFile:
    def test_sha256sum_compatible_format(self, tmp_path):
        (tmp_path / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.json").write_text('{"b": 2}\n', encoding="utf-8")

        checksums_path = rp.write_checksums_file(tmp_path)
        lines = checksums_path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 2
        for line in lines:
            digest, _, name = line.partition("  ")
            assert len(digest) == 64
            assert all(c in "0123456789abcdef" for c in digest)
            assert not name.startswith("/")

    def test_entries_sorted_by_relative_path(self, tmp_path):
        (tmp_path / "z.json").write_text("z", encoding="utf-8")
        (tmp_path / "a.json").write_text("a", encoding="utf-8")
        checksums_path = rp.write_checksums_file(tmp_path)
        names = [line.split("  ", 1)[1] for line in checksums_path.read_text().splitlines()]
        assert names == sorted(names)


class TestDeterministicArchive:
    def _stage(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "b.json").write_text('{"b": 1}\n', encoding="utf-8")
        (staging / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
        sub = staging / "sub"
        sub.mkdir()
        (sub / "c.json").write_text('{"c": 1}\n', encoding="utf-8")
        return staging

    def test_archive_is_byte_identical_across_two_builds(self, tmp_path):
        staging = self._stage(tmp_path)
        archive1 = tmp_path / "out1.tar.gz"
        archive2 = tmp_path / "out2.tar.gz"

        digest1 = rp.build_deterministic_archive(staging, archive1, arcname_prefix="bundle")
        time.sleep(1.1)  # ensure any accidental real-time embedding would differ
        digest2 = rp.build_deterministic_archive(staging, archive2, arcname_prefix="bundle")

        assert digest1 == digest2
        assert archive1.read_bytes() == archive2.read_bytes()

    def test_archive_member_order_is_sorted(self, tmp_path):
        staging = self._stage(tmp_path)
        archive = tmp_path / "out.tar.gz"
        rp.build_deterministic_archive(staging, archive, arcname_prefix="bundle")

        with tarfile.open(archive, "r:gz") as tar:
            names = [member.name for member in tar.getmembers()]
        assert names == [
            "bundle/a.json",
            "bundle/b.json",
            "bundle/sub/c.json",
        ]

    def test_archive_member_metadata_is_fixed(self, tmp_path):
        staging = self._stage(tmp_path)
        archive = tmp_path / "out.tar.gz"
        rp.build_deterministic_archive(staging, archive, arcname_prefix="bundle")

        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                assert member.mtime == 0
                assert member.uid == 0
                assert member.gid == 0
                assert member.mode == 0o644

    def test_returned_digest_matches_archive_bytes(self, tmp_path):
        staging = self._stage(tmp_path)
        archive = tmp_path / "out.tar.gz"
        digest = rp.build_deterministic_archive(staging, archive, arcname_prefix="bundle")
        assert digest == rp.sha256_file(archive)

    def test_no_leftover_temp_file_after_build(self, tmp_path):
        staging = self._stage(tmp_path)
        archive = tmp_path / "out.tar.gz"
        rp.build_deterministic_archive(staging, archive, arcname_prefix="bundle")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out.tar.gz.")]
        assert leftovers == []
