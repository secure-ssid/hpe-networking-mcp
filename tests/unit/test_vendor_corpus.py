"""The vendored OpenAPI corpus must be present, complete and self-describing.

`lookup_api` is a headline capability; it must work from a clean clone with no
network access and no scrape.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "openapi"
MANIFEST = VENDOR / "MANIFEST.json"
REGISTRY_MANIFEST = ROOT / "ingestion" / "openapi_registry_manifest.json"

#: A document that declares neither key is not an API description at all.
VERSION_KEYS = ("openapi", "swagger")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_manifest_exists():
    assert MANIFEST.is_file(), f"{MANIFEST} missing"


def test_manifest_schema_version():
    assert _manifest()["schema_version"] == 1


def test_every_declared_spec_is_present_and_unmodified():
    for entry in _manifest()["specs"]:
        path = VENDOR / entry["file"]
        assert path.is_file(), f"declared spec missing: {entry['file']}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"{entry['file']} does not match manifest sha256"


def test_no_undeclared_specs():
    declared = {e["file"] for e in _manifest()["specs"]}
    found = {p.name for p in VENDOR.glob("*.json")} - {"MANIFEST.json"}
    assert found == declared, f"undeclared: {found - declared}; missing: {declared - found}"


def test_every_spec_declares_provenance():
    for entry in _manifest()["specs"]:
        assert entry["source_url"].startswith("https://"), entry["file"]
        assert entry["license"], f"{entry['file']} has no license field"
        assert entry["fetched"], f"{entry['file']} has no fetched date"


def test_every_spec_carries_the_upstream_fingerprint():
    """``registry_sha256`` must agree with the registry manifest, entry by entry.

    It is the compact ``spec_fingerprint`` digest over the document's content,
    a different serialisation from ``sha256`` above. Reading the committed
    registry manifest and comparing values is not a recomputation, so this
    stays independent of ``ingestion.readme_registry`` internals while still
    catching a dropped registry or an invented digest.
    """
    registries = json.loads(REGISTRY_MANIFEST.read_text())["registries"]
    for entry in _manifest()["specs"]:
        registry_id = entry["registry_id"]
        assert registry_id in registries, (
            f"{entry['file']}: registry_id {registry_id} is not in {REGISTRY_MANIFEST.name}"
        )
        assert entry["registry_sha256"] == registries[registry_id]["sha256"], (
            f"{entry['file']}: registry_sha256 disagrees with {REGISTRY_MANIFEST.name}"
        )


def test_specs_parse_as_openapi():
    """*Every* document must declare an OpenAPI or Swagger version.

    Asserting that merely *some* document does would pass a corpus of 29 junk
    files and one real spec, which is exactly the failure this corpus exists to
    prevent.
    """
    offenders = []
    for entry in _manifest()["specs"]:
        doc = json.loads((VENDOR / entry["file"]).read_text())
        version = next((doc[k] for k in VERSION_KEYS if k in doc), None)
        if not isinstance(version, str) or not version:
            offenders.append(entry["file"])
    assert not offenders, f"documents with no OpenAPI/Swagger version: {offenders}"
