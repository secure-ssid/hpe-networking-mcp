"""Unit tests for ingestion.readme_registry and the two scripts built on it.

Covers the July 2026 ReadMe SuperHub migration repair:
- Parsing the oasPublicUrl pointer out of reference-page HTML (and failing
  loudly, not silently, when it's missing -- e.g. the dead pre-migration
  page shape, or a future format change).
- Fetching + validating the api-registry document.
- Manifest round trip: source URL / project / version / sha256 / fetched_at.
- Drift detection: unchanged / changed / fetch-failed classification.
- scrape_openapi.py's dedup-by-registry-id behavior with mocked network I/O.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from ingestion import readme_registry as rr

_SAMPLE_HTML = (
    '<html><script>{"oasPublicUrl":'
    '"@aruba-new-central-config/v26.04#efby2pmq0s5oms"}</script></html>'
)

_SAMPLE_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Security", "version": "v1alpha1"},
    "paths": {"/dot11k-profiles": {}, "/dot11k-profiles/{name}": {}},
}


class TestExtractOasPointer:
    def test_parses_project_version_and_registry_id(self):
        pointer = rr.extract_oas_pointer(_SAMPLE_HTML)

        assert pointer.project == "aruba-new-central-config"
        assert pointer.version == "v26.04"
        assert pointer.registry_id == "efby2pmq0s5oms"
        assert pointer.registry_url == f"{rr.REGISTRY_BASE_URL}/efby2pmq0s5oms"

    def test_missing_pointer_raises_loudly(self):
        with pytest.raises(rr.RegistryFetchError, match="oasPublicUrl"):
            rr.extract_oas_pointer("<html>no pointer here</html>")

    def test_dead_pre_migration_html_also_raises(self):
        # The pre-migration internal-ui markup embedded an "oasDefinition"
        # blob directly -- that shape must NOT be silently accepted as
        # today's format.
        stale_html = '{"oasDefinition": {"paths": {"/x": {}}}}'
        with pytest.raises(rr.RegistryFetchError):
            rr.extract_oas_pointer(stale_html)


class TestFetchRegistrySpec:
    def test_fetches_and_validates_json(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(_SAMPLE_SPEC).encode()

        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda *a, **k: _Resp())

        spec = rr.fetch_registry_spec(pointer)
        assert spec == _SAMPLE_SPEC

    def test_non_json_response_raises(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"not json"

        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda *a, **k: _Resp())

        with pytest.raises(rr.RegistryFetchError, match="valid JSON"):
            rr.fetch_registry_spec(pointer)

    def test_missing_paths_key_raises(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"info": {}}).encode()

        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda *a, **k: _Resp())

        with pytest.raises(rr.RegistryFetchError, match="paths"):
            rr.fetch_registry_spec(pointer)

    def test_network_error_raises_registry_fetch_error(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")

        def _boom(*a, **k):
            raise urllib.error.URLError("dead host")

        monkeypatch.setattr(rr.urllib.request, "urlopen", _boom)

        with pytest.raises(rr.RegistryFetchError, match="dead host"):
            rr.fetch_registry_spec(pointer)


class TestSpecFingerprint:
    def test_stable_across_key_order(self):
        a = rr.spec_fingerprint({"b": 1, "a": 2})
        b = rr.spec_fingerprint({"a": 2, "b": 1})
        assert a == b

    def test_changes_when_content_changes(self):
        a = rr.spec_fingerprint({"paths": {"/x": {}}})
        b = rr.spec_fingerprint({"paths": {"/x": {}, "/y": {}}})
        assert a != b


class TestRegistrySlug:
    def test_uses_title_and_registry_id_suffix(self):
        pointer = rr.OasPointer("proj", "v1", "efby2pmq0s5oms")
        slug = rr.registry_slug(pointer, {"info": {"title": "Security"}})
        assert slug == "security-efby2pmq0s"

    def test_falls_back_to_registry_id_when_title_missing(self):
        pointer = rr.OasPointer("proj", "v1", "efby2pmq0s5oms")
        slug = rr.registry_slug(pointer, {"info": {}})
        assert slug == "efby2pmq0s"


class TestManifest:
    def test_load_missing_manifest_returns_empty_shape(self, tmp_path):
        manifest = rr.load_manifest(tmp_path / "missing.json")
        assert manifest == {"generated_at": None, "registries": {}}

    def test_load_corrupt_manifest_returns_empty_shape(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert rr.load_manifest(path) == {"generated_at": None, "registries": {}}

    def test_save_then_load_round_trips(self, tmp_path):
        path = tmp_path / "manifest.json"
        manifest = {"generated_at": None, "registries": {}}
        entry = rr.build_registry_entry(
            rr.OasPointer("proj", "v1", "abc123"),
            _SAMPLE_SPEC,
            source_url="https://example.com/page",
            output_path="ingestion/sources/openapi_specs/security-abc123.json",
        )
        rr.upsert_registry_entry(manifest, entry)
        rr.save_manifest(path, manifest)

        loaded = rr.load_manifest(path)
        assert loaded["registries"]["abc123"]["title"] == "Security"
        assert loaded["registries"]["abc123"]["path_count"] == 2
        assert loaded["registries"]["abc123"]["source_url"] == "https://example.com/page"
        assert loaded["generated_at"] is not None

    def test_upsert_replaces_existing_entry_for_same_registry(self):
        manifest = {"generated_at": None, "registries": {}}
        pointer = rr.OasPointer("proj", "v1", "abc123")
        first = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://a", output_path="a.json"
        )
        rr.upsert_registry_entry(manifest, first)
        updated_spec = {**_SAMPLE_SPEC, "paths": {"/z": {}}}
        second = rr.build_registry_entry(
            pointer, updated_spec, source_url="https://b", output_path="b.json"
        )
        rr.upsert_registry_entry(manifest, second)

        assert len(manifest["registries"]) == 1
        assert manifest["registries"]["abc123"]["source_url"] == "https://b"


class TestDriftDetection:
    def test_unchanged_when_hash_matches(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")
        entry = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://example.com/page", output_path="x.json"
        )
        monkeypatch.setattr(
            rr, "fetch_spec_for_page", lambda url, **k: (pointer, _SAMPLE_SPEC)
        )

        result = rr.check_entry_drift(entry)

        assert result.status == "unchanged"

    def test_changed_when_hash_differs(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")
        entry = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://example.com/page", output_path="x.json"
        )
        new_spec = {**_SAMPLE_SPEC, "paths": {"/new-path": {}}}
        monkeypatch.setattr(rr, "fetch_spec_for_page", lambda url, **k: (pointer, new_spec))

        result = rr.check_entry_drift(entry)

        assert result.status == "changed"

    def test_changed_when_registry_id_moved(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")
        entry = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://example.com/page", output_path="x.json"
        )
        new_pointer = rr.OasPointer("proj", "v1", "different-id")
        monkeypatch.setattr(
            rr, "fetch_spec_for_page", lambda url, **k: (new_pointer, _SAMPLE_SPEC)
        )

        result = rr.check_entry_drift(entry)

        assert result.status == "changed"
        assert "different registry id" in result.detail

    def test_fetch_failure_is_reported_not_raised(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")
        entry = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://example.com/page", output_path="x.json"
        )

        def _boom(url, **k):
            raise rr.RegistryFetchError("portal moved again")

        monkeypatch.setattr(rr, "fetch_spec_for_page", _boom)

        result = rr.check_entry_drift(entry)

        assert result.status == "fetch_failed"
        assert "portal moved again" in result.detail

    def test_transient_timeout_is_retried(self, monkeypatch):
        pointer = rr.OasPointer("proj", "v1", "abc123")
        entry = rr.build_registry_entry(
            pointer, _SAMPLE_SPEC, source_url="https://example.com/page", output_path="x.json"
        )
        calls = 0

        def _flaky(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise rr.RegistryFetchError("The read operation timed out")
            return pointer, _SAMPLE_SPEC

        monkeypatch.setattr(rr, "fetch_spec_for_page", _flaky)
        monkeypatch.setattr(rr.time, "sleep", lambda _seconds: None)

        result = rr.check_entry_drift(entry)

        assert result.status == "unchanged"
        assert calls == 2


class TestScrapeOpenapiDedup:
    def test_dedupes_shared_registry_across_multiple_seed_urls(self, tmp_path, monkeypatch):
        """Two different reference-page URLs pointing at the SAME registry
        id must only trigger one registry fetch (mirrors production: many
        pages share one category spec)."""
        import ingestion.scrape_openapi as scrape_openapi

        urls_path = tmp_path / "urls.json"
        urls_path.write_text(json.dumps(["https://example.com/a", "https://example.com/b"]))

        page_html_by_url = {
            "https://example.com/a": _SAMPLE_HTML,
            "https://example.com/b": _SAMPLE_HTML,
        }

        fetch_calls = []

        def fake_fetch_page_html(url, **kwargs):
            return page_html_by_url[url]

        def fake_fetch_registry_spec(pointer_arg, **kwargs):
            fetch_calls.append(pointer_arg.registry_id)
            return _SAMPLE_SPEC

        monkeypatch.setattr(scrape_openapi, "OUTPUT_DIR", tmp_path / "specs")
        monkeypatch.setattr(scrape_openapi, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(scrape_openapi, "fetch_page_html", fake_fetch_page_html)
        monkeypatch.setattr(scrape_openapi, "fetch_registry_spec", fake_fetch_registry_spec)

        exit_code = scrape_openapi.main(["--urls", str(urls_path), "--workers", "2"])

        assert exit_code == 0
        assert fetch_calls == ["efby2pmq0s5oms"]  # exactly one registry fetch
        written = list((tmp_path / "specs").glob("*.json"))
        assert len(written) == 1

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert len(manifest["registries"]) == 1

    def test_page_errors_do_not_abort_the_whole_run(self, tmp_path, monkeypatch):
        import ingestion.scrape_openapi as scrape_openapi

        urls_path = tmp_path / "urls.json"
        urls_path.write_text(json.dumps(["https://example.com/dead", "https://example.com/ok"]))

        def fake_fetch_page_html(url, **kwargs):
            if "dead" in url:
                raise rr.RegistryFetchError("410 gone")
            return _SAMPLE_HTML

        monkeypatch.setattr(scrape_openapi, "OUTPUT_DIR", tmp_path / "specs")
        monkeypatch.setattr(scrape_openapi, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(scrape_openapi, "fetch_page_html", fake_fetch_page_html)
        monkeypatch.setattr(
            scrape_openapi, "fetch_registry_spec", lambda pointer, **k: _SAMPLE_SPEC
        )

        exit_code = scrape_openapi.main(["--urls", str(urls_path), "--workers", "2"])

        assert exit_code == 0  # page errors are reported, not fatal
        written = list((tmp_path / "specs").glob("*.json"))
        assert len(written) == 1

    def test_registry_fetch_failure_sets_nonzero_exit(self, tmp_path, monkeypatch):
        import ingestion.scrape_openapi as scrape_openapi

        urls_path = tmp_path / "urls.json"
        urls_path.write_text(json.dumps(["https://example.com/a"]))

        monkeypatch.setattr(scrape_openapi, "OUTPUT_DIR", tmp_path / "specs")
        monkeypatch.setattr(scrape_openapi, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(scrape_openapi, "fetch_page_html", lambda url, **k: _SAMPLE_HTML)

        def _boom(pointer, **k):
            raise rr.RegistryFetchError("registry gone")

        monkeypatch.setattr(scrape_openapi, "fetch_registry_spec", _boom)

        exit_code = scrape_openapi.main(["--urls", str(urls_path), "--workers", "2"])

        assert exit_code == 1
