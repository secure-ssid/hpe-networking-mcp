"""Integration-style tests for scripts.build_release_bundle.

Exercises the full, offline release-bundle assembly pipeline end to end
against this repository's real (but entirely local/offline) evidence
sources -- never over the network, never with live credentials. Uses
``--no-indexes`` equivalents (``include_indexes=False``) to keep the test
fast and independent of whether ``data/`` RAG/OpenAPI indexes happen to
be built locally.

Covers:
- A full bundle assembles without error and produces a self-consistent,
  schema-valid ``release-manifest.json``, a CycloneDX ``sbom.json``, a
  ``provenance.json`` (with the "not signed" note), and a ``CHECKSUMS.txt``
  covering every staged file.
- The final archive round-trips through ``hpe_networking_mcp.pipeline.release_restore``'s
  safe-extraction + schema-validation path.
- Assembling into an already-existing staging directory refuses to
  overwrite it.
- No credentials/tenant/secret-shaped strings ever appear in any staged
  JSON file (bounded content scan).
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import release_packaging as rp
from hpe_networking_mcp.pipeline import release_restore as rr
from scripts import build_release_bundle


@pytest.fixture(scope="module")
def built_bundle(tmp_path_factory):
    """Assemble one full release bundle (no indexes, to keep this fast)
    and share it read-only across every test in this module."""
    output_dir = tmp_path_factory.mktemp("release-bundle-output")
    result = build_release_bundle.assemble_release_bundle(
        version="v0.0.0-test-fixture",
        output_dir=output_dir,
        include_indexes=False,
    )
    return result


class TestAssembleReleaseBundle:
    def test_produces_expected_top_level_files(self, built_bundle):
        staging = built_bundle.staging_dir
        assert (staging / "release-manifest.json").is_file()
        assert (staging / "sbom.json").is_file()
        assert (staging / "provenance.json").is_file()
        assert (staging / "CHECKSUMS.txt").is_file()
        assert (staging / "evidence" / "validation-matrix.json").is_file()
        assert (staging / "evidence" / "capability-snapshot.json").is_file()

    def test_release_manifest_is_schema_valid_and_lists_every_contract_file(self, built_bundle):
        payload = json.loads(built_bundle.release_manifest_path.read_text(encoding="utf-8"))
        manifest = contracts.build_artifact(contracts.RELEASE_ARTIFACT_MANIFEST, payload)
        assert manifest.release_version == "v0.0.0-test-fixture"
        filenames = {entry.filename for entry in manifest.entries}
        assert "validation-matrix.json" in filenames
        assert "capability-snapshot.json" in filenames

    def test_sbom_is_cyclonedx_shaped(self, built_bundle):
        doc = json.loads(built_bundle.sbom_path.read_text(encoding="utf-8"))
        assert doc["bomFormat"] == "CycloneDX"
        assert "components" in doc
        assert len(doc["components"]) > 0

    def test_provenance_references_every_checksummed_subject(self, built_bundle):
        checksums_lines = built_bundle.checksums_path.read_text(encoding="utf-8").splitlines()
        checksum_names = {line.split("  ", 1)[1] for line in checksums_lines if line.strip()}
        provenance = json.loads(built_bundle.provenance_path.read_text(encoding="utf-8"))
        provenance_names = {subject["name"] for subject in provenance["subjects"]}
        # provenance.json is written after the first checksums pass (whose
        # subjects are recorded into provenance.json itself), then a final
        # checksums pass adds provenance.json to the file listing -- so the
        # final checksums file is exactly the provenance subjects plus
        # provenance.json itself. CHECKSUMS.txt never lists itself.
        assert checksum_names - {"provenance.json"} == provenance_names

    def test_archive_and_checksum_exist(self, built_bundle):
        assert built_bundle.archive_path.is_file()
        assert built_bundle.archive_checksum_path.is_file()
        assert rp.sha256_file(built_bundle.archive_path) in (
            built_bundle.archive_checksum_path.read_text(encoding="utf-8")
        )

    def test_archive_restores_and_validates_via_release_restore(self, built_bundle):
        report = rr.smoke_test_bundle(built_bundle.archive_path)
        assert report.member_count > 0
        assert "validation-matrix.json" in report.validated_contract_files
        assert "sbom.json" in report.structural_checks

    def test_refuses_to_overwrite_existing_staging_dir(self, tmp_path):
        build_release_bundle.assemble_release_bundle(
            version="v0.0.0-dup", output_dir=tmp_path, include_indexes=False
        )
        with pytest.raises(rp.ReleasePackagingError, match="already exists"):
            build_release_bundle.assemble_release_bundle(
                version="v0.0.0-dup", output_dir=tmp_path, include_indexes=False
            )

    def test_no_secret_or_credential_shaped_strings_in_staged_json(self, built_bundle):
        # Catch secret-shaped JSON keys/values and bearer tokens — not tool-name
        # substrings like clearpass_admin_user_password_policy_* which legitimately
        # appear in router automation plans.
        import re

        secret_kv = re.compile(
            r'"(password|passwd|api_key|client_secret|access_token|refresh_token)"\s*:\s*'
            r'"(?!(?:\[REDACTED\]|redacted|<redacted>|"?\s*"?))[^"]+"',
            re.I,
        )
        bearer = re.compile(r"bearer\s+[A-Za-z0-9._\-+/=]{8,}", re.I)
        for json_path in built_bundle.staging_dir.rglob("*.json"):
            text = json_path.read_text(encoding="utf-8")
            m = secret_kv.search(text)
            assert m is None, f"{json_path} contains secret-shaped field {m.group(0)!r}"
            m = bearer.search(text)
            assert m is None, f"{json_path} contains bearer token {m.group(0)!r}"
