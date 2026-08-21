"""Unit tests for ingestion/check_updates.py — tiered freshness detection."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from hpe_networking_mcp.pipeline.clients.source_state import SourceStateStore
from ingestion import check_updates


class _FakeResponse:
    """Minimal stand-in for the context-manager object urlopen() returns."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.com/x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b""),
    )


def test_check_url_new_when_no_prior_state(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    def fake_urlopen(req, timeout):
        return _FakeResponse(b"hello world", {"ETag": "e1", "Last-Modified": "lm1"})

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)

    assert result["status"] == "new"
    assert result["method"] == "content_hash"
    row = store.get("https://example.com/x")
    assert row["etag"] == "e1"
    assert row["content_hash"] is not None


def test_check_url_unchanged_via_conditional_304(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")
    store.record_checked(
        "https://example.com/x", "src", etag="e1", last_modified="lm1",
        content_hash="deadbeef", changed=True,
    )

    def fake_urlopen(req, timeout):
        raise _http_error(304)

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)

    assert result["status"] == "unchanged"
    assert result["method"] == "metadata_304"
    # unchanged hash preserved
    assert store.get("https://example.com/x")["content_hash"] == "deadbeef"


def test_check_url_changed_falls_back_to_content_hash(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")
    store.record_checked(
        "https://example.com/x", "src", etag="e1", content_hash="oldhash", changed=True,
    )

    def fake_urlopen(req, timeout):
        return _FakeResponse(b"new content", {"ETag": "e2"})

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)

    assert result["status"] == "changed"
    assert result["method"] == "content_hash"
    assert store.get("https://example.com/x")["content_hash"] != "oldhash"


def test_check_url_unchanged_hash_when_body_identical(tmp_path: Path, monkeypatch):
    import hashlib
    store = SourceStateStore(tmp_path / "state.sqlite")
    body = b"same content"
    prior_hash = hashlib.sha256(body).hexdigest()
    store.record_checked("https://example.com/x", "src", content_hash=prior_hash, changed=True)

    def fake_urlopen(req, timeout):
        return _FakeResponse(body, {})

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)
    assert result["status"] == "unchanged"
    assert result["method"] == "content_hash"


def test_check_url_blocked_on_403(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    def fake_urlopen(req, timeout):
        raise _http_error(403)

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)
    assert result["status"] == "blocked"
    assert result["method"] == "http_blocked"
    # blocked check should not write state
    assert store.get("https://example.com/x") is None


def test_check_url_error_on_other_http_codes(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    def fake_urlopen(req, timeout):
        raise _http_error(500)

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=False)
    assert result["status"] == "error"


def test_check_url_dry_run_does_not_persist(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    def fake_urlopen(req, timeout):
        return _FakeResponse(b"hello", {"ETag": "e1"})

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", fake_urlopen)

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=True)
    assert result["status"] == "new"
    assert store.get("https://example.com/x") is None


def test_resolve_urls_via_header_scan(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(check_updates, "ROOT", tmp_path)
    output_dir = tmp_path / "ingestion" / "sources" / "my_docs"
    output_dir.mkdir(parents=True)
    (output_dir / "page1.md").write_text(
        "<!-- source: https://example.com/page1 -->\n\ncontent", encoding="utf-8"
    )
    (output_dir / "page2.md").write_text(
        "<!-- source: https://example.com/page2 -->\n\ncontent", encoding="utf-8"
    )

    entry = {
        "source": "my_docs", "output_dir": "ingestion/sources/my_docs",
        "scraper": "ingestion/scrape_my_docs.py",
    }
    urls, reason = check_updates.resolve_urls(entry)
    assert reason is None
    assert set(urls) == {"https://example.com/page1", "https://example.com/page2"}


def test_retired_internal_ui_host_is_never_reconstructed():
    """The pre-migration internal-ui download origin was retired with the
    July 2026 ReadMe SuperHub move; rebuilding those URLs from local
    filenames produced dead-host errors that looked like freshness signal.
    It must not come back."""
    assert not hasattr(check_updates, "OPENAPI_BASE_URL")

    # The host may only survive as prose (docstring/comment) explaining the
    # removal -- never as a value that could be requested.
    source = Path(check_updates.__file__).read_text(encoding="utf-8")
    assert 'https://internal-ui' not in source
    assert '"https://internal-ui.central.arubanetworks.com' not in source

    # And the current resolvers are the registry/api-next ones.
    assert set(check_updates.MANIFEST_RESOLVERS) == {"openapi_specs", "product_specs"}
    assert check_updates.REGISTRY_BASE_URL.startswith("https://dash.readme.com/api/v1/api-registry")


def test_resolve_urls_openapi_specs_uses_registry_manifest(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "openapi_registry_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "registries": {
                    "abc123": {
                        "registry_id": "abc123",
                        "source_url": "https://developer.arubanetworks.com/new-central/reference/x",
                        "sha256": "deadbeef",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_updates, "REGISTRY_MANIFEST_PATH", manifest)

    entry = {
        "source": "openapi_specs", "output_dir": "ingestion/sources/openapi_specs",
        "scraper": "ingestion/scrape_openapi.py",
    }
    urls, reason = check_updates.resolve_urls(entry)

    assert reason is None
    assert urls == [
        f"{check_updates.REGISTRY_BASE_URL}/abc123",
        "https://developer.arubanetworks.com/new-central/reference/x",
    ]


def test_resolve_urls_openapi_specs_reports_empty_registry_manifest(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "openapi_registry_manifest.json"
    manifest.write_text(json.dumps({"registries": {}}), encoding="utf-8")
    monkeypatch.setattr(check_updates, "REGISTRY_MANIFEST_PATH", manifest)

    urls, reason = check_updates.resolve_urls(
        {"source": "openapi_specs", "output_dir": "ingestion/sources/openapi_specs"}
    )

    assert urls == []
    assert "scrape_openapi.py" in reason


def test_resolve_urls_product_specs_uses_apinext_manifest(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "product_specs_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "specs": [
                    {"source_url": "https://developer.arubanetworks.com/aoscx/reference/a"},
                    {"source_url": "https://developer.arubanetworks.com/cppm/reference/b"},
                    {"source_url": "https://developer.arubanetworks.com/aoscx/reference/a"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_updates, "PRODUCT_SPECS_MANIFEST_PATH", manifest)

    urls, reason = check_updates.resolve_urls(
        {"source": "product_specs", "output_dir": "ingestion/sources/product_specs"}
    )

    assert reason is None
    assert urls == [
        "https://developer.arubanetworks.com/aoscx/reference/a",
        "https://developer.arubanetworks.com/cppm/reference/b",
    ]


def test_resolve_urls_corrupt_manifest_reports_reason_not_crash(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "openapi_registry_manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(check_updates, "REGISTRY_MANIFEST_PATH", manifest)

    urls, reason = check_updates.resolve_urls(
        {"source": "openapi_specs", "output_dir": "ingestion/sources/openapi_specs"}
    )

    assert urls == []
    assert "missing or unreadable" in reason


# ---------------------------------------------------------------------------
# Result classification: transport failures are never content drift
# ---------------------------------------------------------------------------


def test_blocked_and_error_map_to_unavailable_not_drift(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    for code in (403, 429, 500):
        monkeypatch.setattr(
            check_updates.urllib.request, "urlopen",
            lambda req, timeout, _c=code: (_ for _ in ()).throw(_http_error(_c)),
        )
        result = check_updates.check_url("https://example.com/x", "src", store, dry_run=True)
        assert result["result_class"] == taxonomy.UNAVAILABLE
        assert result["result_class"] != taxonomy.CONTENT_DRIFT


def test_gone_url_maps_to_source_removed(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")
    monkeypatch.setattr(
        check_updates.urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_http_error(404)),
    )

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=True)

    assert result["status"] == "gone"
    assert result["result_class"] == taxonomy.SOURCE_REMOVED


def test_first_ever_check_is_baseline_not_added(tmp_path: Path, monkeypatch):
    """With no prior state for the source at all, a first fetch establishes a
    baseline -- calling that an 'added source' would flag the whole corpus."""
    store = SourceStateStore(tmp_path / "state.sqlite")
    monkeypatch.setattr(
        check_updates.urllib.request, "urlopen",
        lambda req, timeout: _FakeResponse(b"body", {}),
    )

    result = check_updates.check_url(
        "https://example.com/x", "src", store, False, baseline_exists=False
    )

    assert result["status"] == "baseline"
    assert result["result_class"] == taxonomy.NOT_CHECKED


def test_new_url_against_existing_baseline_is_source_added(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")
    monkeypatch.setattr(
        check_updates.urllib.request, "urlopen",
        lambda req, timeout: _FakeResponse(b"body", {}),
    )

    result = check_updates.check_url(
        "https://example.com/x", "src", store, False, baseline_exists=True
    )

    assert result["status"] == "new"
    assert result["result_class"] == taxonomy.SOURCE_ADDED


def test_changed_body_is_content_drift(tmp_path: Path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")
    store.record_checked("https://example.com/x", "src", content_hash="old", changed=True)
    monkeypatch.setattr(
        check_updates.urllib.request, "urlopen",
        lambda req, timeout: _FakeResponse(b"new body", {}),
    )

    result = check_updates.check_url("https://example.com/x", "src", store, dry_run=True)

    assert result["result_class"] == taxonomy.CONTENT_DRIFT


def test_source_result_class_prefers_incomplete_over_drift():
    counts = dict.fromkeys(taxonomy.RESULT_CLASSES, 0)
    counts[taxonomy.CONTENT_DRIFT] = 5
    counts[taxonomy.UNAVAILABLE] = 1
    assert check_updates.source_result_class(counts) == taxonomy.UNAVAILABLE

    counts[taxonomy.UNAVAILABLE] = 0
    assert check_updates.source_result_class(counts) == taxonomy.CONTENT_DRIFT


def test_offline_source_check_fetches_nothing_and_claims_no_freshness(tmp_path, monkeypatch):
    store = SourceStateStore(tmp_path / "state.sqlite")

    def _boom(*args, **kwargs):
        raise AssertionError("offline check must not open a connection")

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(
        check_updates, "resolve_urls", lambda entry: (["https://example.com/a"], None)
    )

    report = check_updates.check_source(
        {"source": "src", "output_dir": "ingestion/sources/src"},
        store,
        dry_run=True,
        offline=True,
    )

    assert report["result_class"] == taxonomy.NOT_CHECKED
    assert report["known_urls"] == 1
    assert report["checked"] == 0


def test_build_report_marks_transport_failures_as_incomplete():
    reports = [
        {
            "source": "a", "resolvable": True, "reason": None,
            "result_class": taxonomy.UNAVAILABLE, "checked": 3, "changed": 0, "new": 0,
            "gone": 0, "blocked": 3, "errors": 0, "unchanged": 0,
        }
    ]
    report = check_updates.build_report(reports, offline=False)

    assert report["check_incomplete"] is True
    assert report["content_drift_detected"] is False
    assert report["exit_code"] == taxonomy.EXIT_UNAVAILABLE


def test_resolve_urls_unresolvable_when_no_scraper(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(check_updates, "ROOT", tmp_path)
    entry = {
        "source": "feature_navigator", "output_dir": "ingestion/sources/feature_navigator",
        "scraper": None,
    }
    urls, reason = check_updates.resolve_urls(entry)
    assert urls == []
    assert reason is not None
    assert "no scraper registered" in reason


def test_resolve_urls_unresolvable_when_no_baseline(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(check_updates, "ROOT", tmp_path)
    entry = {
        "source": "aos_techdocs", "output_dir": "ingestion/sources/aos_techdocs",
        "scraper": "ingestion/scrape_aos_pw.py",
    }
    urls, reason = check_updates.resolve_urls(entry)
    assert urls == []
    assert reason is not None
    assert "scrape_aos_pw.py" in reason


def test_check_url_is_rate_limited():
    """A freshness check walks every known URL of every source — thousands of
    requests post-expansion — so it must stay paced like the scrapers it backs.
    Guards against the delay being dropped or the worker count creeping back up.
    """
    assert check_updates.REQUEST_DELAY >= 0.4
    assert check_updates.CHECK_WORKERS <= 4


def test_check_url_sleeps_before_requesting(monkeypatch):
    """The delay must be inside the per-URL worker path, not merely defined —
    a module-level constant that nothing calls would silently unthrottle the crawl.
    """
    calls: list[float] = []
    monkeypatch.setattr(check_updates.time, "sleep", lambda s: calls.append(s))

    def _boom(*args, **kwargs):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(check_updates.urllib.request, "urlopen", _boom)

    class _Store:
        def get(self, url):
            return None

    result = check_updates.check_url("https://example.invalid/x", "s", _Store(), dry_run=True)
    assert result["status"] == "error"
    assert calls == [check_updates.REQUEST_DELAY]


def test_seed_files_resolve_both_committed_shapes(tmp_path, monkeypatch):
    """Seed files ship in two shapes and both must resolve to plain URLs.

    Most discover scripts write a flat list of paths/URLs, but
    ``mist_product_updates`` writes discovery records (``{url, title,
    year}``). A resolver that assumes strings raises ``AttributeError`` on
    the record shape and takes the whole offline drift run down with it, so
    pin both here.
    """
    monkeypatch.setattr(check_updates, "ROOT", tmp_path)

    (tmp_path / "flat.json").write_text(
        json.dumps(["https://example.invalid/a", "https://example.invalid/b"]),
        encoding="utf-8",
    )
    (tmp_path / "records.json").write_text(
        json.dumps(
            [
                {"url": "https://example.invalid/2026", "title": "2026 notes", "year": 2026},
                {"url": "https://example.invalid/2025", "title": "2025 notes", "year": 2025},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "relative.json").write_text(
        json.dumps(["/docs/one", "/docs/two"]), encoding="utf-8"
    )

    assert check_updates._urls_from_seed_file("flat.json", []) == [
        "https://example.invalid/a",
        "https://example.invalid/b",
    ]
    assert check_updates._urls_from_seed_file("records.json", []) == [
        "https://example.invalid/2026",
        "https://example.invalid/2025",
    ]
    assert check_updates._urls_from_seed_file(
        "relative.json", ["https://seed.invalid/start"]
    ) == ["https://seed.invalid/docs/one", "https://seed.invalid/docs/two"]


def test_seed_file_records_without_a_url_are_dropped(tmp_path, monkeypatch):
    """A malformed record must not become an empty or ``None`` URL."""
    monkeypatch.setattr(check_updates, "ROOT", tmp_path)
    (tmp_path / "mixed.json").write_text(
        json.dumps([{"title": "no url"}, {"url": "https://example.invalid/ok"}, ""]),
        encoding="utf-8",
    )

    assert check_updates._urls_from_seed_file("mixed.json", []) == [
        "https://example.invalid/ok"
    ]
