"""Unit tests for scripts/validate_source_manifest.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import validate_source_manifest as vsm


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _write_source_meta(path: Path, pairs: dict[str, str]) -> None:
    body = ",\n".join(f'    "{k}": "{v}"' for k, v in pairs.items())
    path.write_text(f"SOURCE_META = {{\n{body}\n}}\n", encoding="utf-8")


def _write_doc_type_map(path: Path, pairs: dict[str, str]) -> None:
    body = ",\n".join(f'    "{k}": "{v}"' for k, v in pairs.items())
    path.write_text(f'_DOC_TYPE_TO_SOURCE: dict[str, str] = {{\n{body}\n}}\n', encoding="utf-8")


def _good_entry(root: Path, source: str, scraper_name: str) -> dict:
    scraper_path = root / "ingestion" / f"{scraper_name}.py"
    scraper_path.parent.mkdir(parents=True, exist_ok=True)
    scraper_path.write_text("# scraper\n", encoding="utf-8")
    return {
        "source": source,
        "doc_type": source.replace("_", "-"),
        "purpose": "test",
        "seed_urls": ["https://example.com"],
        "output_dir": f"ingestion/sources/{source}",
        "scraper": f"ingestion/{scraper_name}.py",
        "notes": "test entry",
    }


def _patch_paths(monkeypatch, root: Path):
    monkeypatch.setattr(vsm, "ROOT", root)
    monkeypatch.setattr(vsm, "MANIFEST_PATH", root / "ingestion" / "source_manifest.json")
    monkeypatch.setattr(vsm, "INGEST_DOCS_PATH", root / "ingestion" / "ingest_docs.py")
    monkeypatch.setattr(
        vsm,
        "RAG_PY_PATH",
        root / "src" / "hpe_networking_mcp" / "mcp_servers" / "rag.py",
    )


def test_valid_manifest_passes(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"my_docs": "my-docs"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"my-docs": "my_docs"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert fails == []


def test_missing_scraper_file_fails(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry["scraper"] = "ingestion/does_not_exist.py"
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"my_docs": "my-docs"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"my-docs": "my_docs"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert any("scraper exists" in c.name for c in fails)


def test_wrong_output_dir_convention_fails(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry["output_dir"] = "wrong/path"
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"my_docs": "my-docs"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"my-docs": "my_docs"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert any("output_dir convention" in c.name for c in fails)


def test_duplicate_source_keys_fails(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    _write_manifest(vsm.MANIFEST_PATH, [entry, dict(entry)])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"my_docs": "my-docs"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"my-docs": "my_docs"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert any("duplicate source keys" in c.name for c in fails)


def test_missing_source_meta_entry_fails(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"other_source": "other"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"my-docs": "my_docs"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert any("SOURCE_META" in c.name for c in fails)


def test_missing_doc_type_to_source_is_warn_not_fail(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {"my_docs": "my-docs"})
    _write_doc_type_map(vsm.RAG_PY_PATH, {"other-doc-type": "other_source"})

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    assert not any("_DOC_TYPE_TO_SOURCE" in c.name for c in fails)
    assert any("_DOC_TYPE_TO_SOURCE" in c.name for c in warns)


def test_invalid_json_fails_gracefully(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    vsm.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    vsm.MANIFEST_PATH.write_text("{not valid json", encoding="utf-8")

    checks = vsm.validate()
    assert len(checks) == 1
    assert checks[0].status == "FAIL"


def test_missing_required_field_fails(tmp_path: Path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    del entry["purpose"]
    _write_manifest(vsm.MANIFEST_PATH, [entry])

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]
    assert any("required fields" in c.name for c in fails)


# ---------------------------------------------------------------------------
# extra_script_phases: discovery must be declared to run before the scraper
# ---------------------------------------------------------------------------


def _setup_meta(monkeypatch, tmp_path, entry):
    _patch_paths(monkeypatch, tmp_path)
    _write_manifest(vsm.MANIFEST_PATH, [entry])
    vsm.RAG_PY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_source_meta(vsm.INGEST_DOCS_PATH, {entry["source"]: entry["doc_type"]})
    _write_doc_type_map(vsm.RAG_PY_PATH, {entry["doc_type"]: entry["source"]})


def _with_extra(root: Path, entry: dict, script: str, phase: str | None) -> dict:
    path = root / script
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# extra\n", encoding="utf-8")
    entry["extra_scripts"] = [script]
    if phase is not None:
        entry["extra_script_phases"] = {script: phase}
    return entry


def test_discovery_script_declared_pre_passes(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/discover_my_docs_urls.py", "pre")
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()

    assert [c for c in checks if c.status == "FAIL"] == []


def test_discovery_script_without_pre_phase_fails(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/discover_my_docs_urls.py", None)
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()
    fails = [c for c in checks if c.status == "FAIL"]

    assert any("must declare phase 'pre'" in c.detail for c in fails)


def test_discovery_script_declared_post_fails(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/discover_my_docs_urls.py", "post")
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()

    assert any("must declare phase 'pre'" in c.detail for c in checks if c.status == "FAIL")


def test_unknown_phase_value_fails(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/extract_my_docs.py", "whenever")
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()

    assert any("expected one of" in c.detail for c in checks if c.status == "FAIL")


def test_phase_for_unlisted_script_fails(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/extract_my_docs.py", "post")
    entry["extra_script_phases"]["ingestion/not_declared.py"] = "pre"
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()

    assert any("not listed in extra_scripts" in c.detail for c in checks if c.status == "FAIL")


def test_non_object_phase_map_fails(tmp_path: Path, monkeypatch):
    entry = _good_entry(tmp_path, "my_docs", "scrape_my_docs")
    entry = _with_extra(tmp_path, entry, "ingestion/extract_my_docs.py", "post")
    entry["extra_script_phases"] = ["pre"]
    _setup_meta(monkeypatch, tmp_path, entry)

    checks = vsm.validate()

    assert any("must be an object" in c.detail for c in checks if c.status == "FAIL")


def test_committed_manifest_declares_a_phase_for_every_extra_script():
    """No script may silently lose its phase in the migration."""
    manifest = json.loads(
        (Path(vsm.__file__).resolve().parents[1] / "ingestion" / "source_manifest.json")
        .read_text(encoding="utf-8")
    )
    for entry in manifest:
        extras = entry.get("extra_scripts") or []
        if not extras:
            continue
        phases = entry.get("extra_script_phases") or {}
        assert set(phases) == set(extras), entry["source"]
        assert set(phases.values()) <= set(vsm.EXTRA_SCRIPT_PHASES), entry["source"]
        for script in extras:
            if Path(script).name.startswith("discover_"):
                assert phases[script] == "pre", f"{entry['source']}:{script}"
