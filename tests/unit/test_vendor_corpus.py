"""The vendored OpenAPI corpus must be present, complete and self-describing.

`lookup_api` is a headline capability; it must work from a clean clone with no
network access and no scrape.
"""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "openapi"
MANIFEST = VENDOR / "MANIFEST.json"
REGISTRY_MANIFEST = ROOT / "ingestion" / "openapi_registry_manifest.json"

#: A document that declares neither key is not an API description at all.
VERSION_KEYS = ("openapi", "swagger")

#: The one document vendored from a git commit rather than a ReadMe registry.
MIST_FILE = "mist.openapi.json"
MIST_REPO = "mistsys/mist_openapi"
MIST_COMMIT = "315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e"
MIST_SHA256 = "2c3d769ef188bbce1b9db7a0774b5a10812d0a5bc11960b768de47b66bb88bbf"
MIST_PATH_COUNT = 756


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


def test_every_spec_is_pinned_to_a_verifiable_upstream():
    """Two pinning schemes, one rule: no entry may be unpinned.

    A ReadMe-registry document is pinned by ``registry_id`` plus
    ``registry_sha256``, which must agree with the registry manifest, entry by
    entry. ``registry_sha256`` is the compact ``spec_fingerprint`` digest over
    the document's content, a different serialisation from ``sha256`` above.
    Reading the committed registry manifest and comparing values is not a
    recomputation, so this stays independent of ``ingestion.readme_registry``
    internals while still catching a dropped registry or an invented digest.

    A git-hosted document is pinned by ``upstream_repo`` plus
    ``upstream_commit``, and that commit must appear in ``source_url``: a
    branch URL re-resolves under you, so a pin nothing can re-fetch is no pin.
    """
    registries = json.loads(REGISTRY_MANIFEST.read_text())["registries"]
    for entry in _manifest()["specs"]:
        if "registry_id" in entry:
            registry_id = entry["registry_id"]
            assert registry_id in registries, (
                f"{entry['file']}: registry_id {registry_id} is not in {REGISTRY_MANIFEST.name}"
            )
            assert entry["registry_sha256"] == registries[registry_id]["sha256"], (
                f"{entry['file']}: registry_sha256 disagrees with {REGISTRY_MANIFEST.name}"
            )
        elif "upstream_commit" in entry:
            commit = entry["upstream_commit"]
            assert re.fullmatch(r"[0-9a-f]{40}", commit), (
                f"{entry['file']}: upstream_commit {commit!r} is not a full commit sha"
            )
            assert entry.get("upstream_repo"), f"{entry['file']} has no upstream_repo"
            assert commit in entry["source_url"], (
                f"{entry['file']}: source_url {entry['source_url']} is not pinned to {commit}"
            )
        else:
            raise AssertionError(
                f"{entry['file']} is unpinned: it declares neither a registry_id "
                "resolving in the registry manifest nor an upstream commit pin"
            )


def test_the_mist_spec_is_vendored_verbatim_under_mit():
    """Present, MIT, and pinned to a commit rather than a branch.

    The Central documents arrive from an API as parsed objects and are
    re-serialised, so their ``sha256`` covers our indent=2 bytes. This one
    arrives as a file and is vendored byte-for-byte, so its ``sha256`` is the
    digest of exactly what GitHub serves at the pinned commit — asserted
    literally, because that identity is the whole basis of the pin.
    """
    entry = next((e for e in _manifest()["specs"] if e["file"] == MIST_FILE), None)
    assert entry is not None, f"{MIST_FILE} is not declared in MANIFEST.json"
    assert entry["license"] == "MIT", entry["license"]
    assert entry["upstream_repo"] == MIST_REPO
    assert entry["upstream_commit"] == MIST_COMMIT
    assert entry["sha256"] == MIST_SHA256
    assert MIST_COMMIT in entry["source_url"], entry["source_url"]
    for branch in ("/master/", "/main/"):
        assert branch not in entry["source_url"], (
            f"source_url names a branch, not a commit: {entry['source_url']}"
        )
    document = json.loads((VENDOR / MIST_FILE).read_text())
    assert entry["path_count"] == len(document["paths"]) == MIST_PATH_COUNT


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
