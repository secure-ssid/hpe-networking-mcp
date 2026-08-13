from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_tools
from scripts import build_optional_product_manifests as optional_manifests


@pytest.mark.parametrize("platform", ["clearpass", "aos8", "uxi", "apstra"])
def test_committed_optional_manifest_matches_offline_provenance(platform):
    optional_manifests.check_committed_offline(platform)


def test_source_digest_drift_is_rejected():
    platform = "uxi"
    path = manifest_tools.manifest_path(platform)
    manifest = json.loads(path.read_text())
    rendered = manifest_tools.dumps(manifest)
    pin = optional_manifests.load_source_pin(platform)
    pin["source_sha256"] = "0" * 64

    with pytest.raises(optional_manifests.SourcePinError, match="source_sha256"):
        optional_manifests.validate_source_pin(platform, manifest, rendered, pin)


def test_manifest_digest_drift_is_rejected():
    platform = "aos8"
    path = manifest_tools.manifest_path(platform)
    manifest = json.loads(path.read_text())
    manifest["operations"][0]["summary"] = "changed without provenance review"
    rendered = manifest_tools.dumps(manifest)

    with pytest.raises(optional_manifests.SourcePinError, match="manifest_sha256"):
        optional_manifests.validate_source_pin(platform, manifest, rendered)


def test_registry_set_drift_is_rejected():
    platform = "clearpass"
    path = manifest_tools.manifest_path(platform)
    manifest = json.loads(path.read_text())
    rendered = manifest_tools.dumps(manifest)
    pin = optional_manifests.load_source_pin(platform)
    pin["registry_ids"] = pin["registry_ids"][:-1]

    with pytest.raises(optional_manifests.SourcePinError, match="registry_ids"):
        optional_manifests.validate_source_pin(platform, manifest, rendered, pin)


def test_apstra_embedded_provenance_matches_manifest_source():
    manifest = optional_manifests.build_apstra()

    assert manifest["provenance"]["source_sha256"] == manifest["source"]["sha256"]
