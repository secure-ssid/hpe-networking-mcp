"""Unit tests for ingestion.ingest_docs chunk-id determinism and bounding.

Covers two RAG/ingestion audit findings:

- Chunk ids must be identical regardless of whether ``ingest_docs.py`` (and
  thus its module-level ``SOURCES_DIR``) was invoked with a relative or an
  absolute path — otherwise re-running ingestion from a different cwd would
  look like every chunk changed, defeating incremental upsert/delete.
- ``chunking.chunk_text`` must always return chunks bounded by
  ``CHUNK_SIZE``, even for text with none of the structural separators
  (paragraph/line/sentence/space) the splitter prefers.
"""

from __future__ import annotations

from pathlib import Path

from ingestion import ingest_docs
from ingestion.chunking import CHUNK_SIZE, MIN_CHUNK_SIZE, _merge_small_chunks, chunk_text


def test_collect_points_ids_stable_across_relative_and_absolute_invocation(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "sources"
    (docs_root / "sample_docs").mkdir(parents=True)
    file_path = docs_root / "sample_docs" / "page.md"
    file_path.write_text(
        "Some sample documentation content long enough to form at least "
        "one chunk of text for this regression test.",
        encoding="utf-8",
    )

    # Absolute invocation: SOURCES_DIR itself is an absolute path (e.g. the
    # script was launched with `uv run python /abs/path/ingestion/ingest_docs.py`).
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", docs_root)
    abs_records = ingest_docs.collect_points(docs_root / "sample_docs", "guide")

    # Relative invocation: SOURCES_DIR is a relative path resolved against a
    # different cwd (e.g. `uv run python ingestion/ingest_docs.py` from the
    # repo root) — the same physical file, named differently.
    monkeypatch.chdir(tmp_path)
    rel_sources_dir = Path("sources")
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", rel_sources_dir)
    rel_records = ingest_docs.collect_points(rel_sources_dir / "sample_docs", "guide")

    assert len(abs_records) == len(rel_records) == 1
    assert abs_records[0]["id"] == rel_records[0]["id"]
    assert abs_records[0]["file_path"] == rel_records[0]["file_path"]


def test_stable_id_is_deterministic_for_same_relative_path():
    assert ingest_docs.stable_id("sample_docs/page.md", 0) == ingest_docs.stable_id(
        "sample_docs/page.md", 0
    )
    assert ingest_docs.stable_id("sample_docs/page.md", 0) != ingest_docs.stable_id(
        "sample_docs/page.md", 1
    )


def test_chunk_text_bounds_unbroken_text_via_character_fallback():
    # No paragraph/line/sentence/space separators at all -- previously came
    # back as a single unbounded chunk instead of being split at CHUNK_SIZE.
    text = "a" * (CHUNK_SIZE * 5)

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)
    assert "".join(chunks).replace("a", "") == ""


def test_chunk_text_still_prefers_structural_separators():
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."

    chunks = chunk_text(text)

    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)
    assert any("Paragraph one." in chunk for chunk in chunks)


def test_chunk_text_merges_orphan_heading_into_following_body():
    # Real-world defect (found via ask_docs("EX4400 switch power
    # specifications") returning only a bare "# EX4400 -- specifications"
    # heading): a short heading immediately followed by a blank line and a
    # body paragraph large enough that heading+body would exceed CHUNK_SIZE
    # is split into its own tiny, low-information "orphan" chunk that can
    # outrank the real content for title-keyword queries. It must instead be
    # folded into a neighboring chunk.
    heading = "# Widget X specifications"
    body_paragraph_1 = "x" * 790  # heading + body_paragraph_1 > CHUNK_SIZE
    body_paragraph_2 = "y" * 250  # stays its own (already >= MIN_CHUNK_SIZE)
    text = f"{heading}\n\n{body_paragraph_1}\n\n{body_paragraph_2}"

    chunks = chunk_text(text)

    assert heading not in chunks  # never isolated on its own
    assert all(len(chunk) >= MIN_CHUNK_SIZE for chunk in chunks)
    assert any(heading in chunk and "x" in chunk for chunk in chunks)
    assert any("y" * 250 in chunk for chunk in chunks)


def test_merge_small_chunks_folds_run_of_tiny_chunks_forward():
    # Several consecutive sub-threshold chunks (e.g. produced by adjacent
    # recursive splits, not just a single isolated heading) must fold
    # forward together rather than surviving as separate tiny chunks.
    chunks = ["tiny one", "tiny two", "tiny three", "z" * 300]

    merged = _merge_small_chunks(chunks)

    assert all(len(chunk) >= MIN_CHUNK_SIZE for chunk in merged)
    assert merged == ["tiny one\n\ntiny two\n\ntiny three\n\n" + "z" * 300]


def test_merge_small_chunks_folds_trailing_chunk_backward():
    # A small chunk with no successor (text ends on a short final piece)
    # has nothing to merge forward into, so it must fold into the previous
    # chunk instead of surviving as its own tiny final chunk.
    chunks = ["a" * 300, "b" * 300, "tiny tail"]

    merged = _merge_small_chunks(chunks)

    assert merged == ["a" * 300, "b" * 300 + "\n\ntiny tail"]


def test_merge_small_chunks_returns_single_chunk_when_all_input_is_small():
    # A document that never reaches MIN_CHUNK_SIZE in total has nothing to
    # merge with -- it is returned as its own (undersized) single chunk
    # rather than being dropped or left as multiple sub-threshold pieces.
    chunks = ["a" * 30, "b" * 30, "c" * 30]

    merged = _merge_small_chunks(chunks)

    assert merged == ["a" * 30 + "\n\n" + "b" * 30 + "\n\n" + "c" * 30]


def test_merge_small_chunks_noop_when_all_chunks_already_adequate():
    chunks = ["p" * 250, "q" * 300, "r" * 400]

    assert _merge_small_chunks(chunks) == chunks


def test_merge_small_chunks_noop_for_single_chunk_input():
    assert _merge_small_chunks(["only chunk"]) == ["only chunk"]
    assert _merge_small_chunks([]) == []


def test_schema_to_text_skips_boolean_property_schemas():
    # OpenAPI 3.1 / JSON Schema allows boolean schemas (true/false) as a
    # property value — these must be skipped, not crash on ``bool.get``.
    schema = {
        "description": "Example",
        "properties": {
            "allowAnything": True,
            "denyAnything": False,
            "name": {"type": "string", "description": "the name"},
        },
    }

    text = ingest_docs._schema_to_text("spec", "Example", schema)

    assert text is not None
    assert "name" in text
    assert "allowAnything" not in text


def test_collect_openapi_points_survives_boolean_property_schema(tmp_path, monkeypatch):
    import json

    specs = tmp_path / "sources" / "openapi_specs"
    specs.mkdir(parents=True)
    (specs / "api.json").write_text(
        json.dumps(
            {
                "info": {"title": "API"},
                "components": {
                    "schemas": {
                        "Thing": {
                            "description": "d",
                            "properties": {"flag": True, "id": {"type": "string"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", tmp_path / "sources")

    records = ingest_docs.collect_openapi_points(specs)

    assert any(r["doc_type"] == "openapi" for r in records)


def test_collect_points_skips_symlink_resolving_outside_sources(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    (sources / "sample_docs").mkdir(parents=True)
    real = sources / "sample_docs" / "page.md"
    real.write_text("in-tree content long enough for a chunk.", encoding="utf-8")

    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("out-of-tree content that must not be indexed.", encoding="utf-8")
    link = sources / "sample_docs" / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks unsupported on this platform")

    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources)

    records = ingest_docs.collect_points(sources / "sample_docs", "guide")

    file_paths = {r["file_path"] for r in records}
    assert "sample_docs/page.md" in file_paths
    assert all("secret" not in fp for fp in file_paths)


# ---------------------------------------------------------------------------
# HTML main-content extraction
#
# WebHelp books wrap every topic in an identical nav/header/footer shell that
# is several times larger than the topic itself. Indexing the whole document
# put tens of thousands of near-identical boilerplate chunks into the corpus.
# ---------------------------------------------------------------------------

_NAV = "Home Terminology Change Support PDFs More Info Account Settings Logout " * 12


def test_html_to_text_prefers_main_region_over_nav_chrome():
    html = (
        f"<html><body><div class='nav'>{_NAV}</div>"
        "<div role='main'><h1>show ap debug log-config</h1>"
        "<p>This command shows AP log configuration for the specified access point. "
        "Use the ap-name parameter to select an access point by name, or ip-addr to "
        "select it by address. The example output lists whether the AP is registered "
        "with its managed device and which log levels are currently in effect.</p>"
        "</div></body></html>"
    )
    text = ingest_docs.html_to_text(html)
    assert "show ap debug log-config" in text
    assert "Terminology Change" not in text


def test_html_to_text_falls_back_when_no_main_region():
    """Some books mark no content region at all — losing those pages entirely
    would be far worse than keeping their chrome."""
    body = "Private 5G solution components. " * 20
    text = ingest_docs.html_to_text(f"<html><body><p>{body}</p></body></html>")
    assert "Private 5G" in text


def test_html_to_text_falls_back_when_main_region_is_effectively_empty():
    """A matching but near-empty region means the selector is being used for
    something else (a nav landing page), so it must not silently win."""
    body = "Real topic content that must survive. " * 20
    html = f"<html><body><main>  </main><p>{body}</p></body></html>"
    text = ingest_docs.html_to_text(html)
    assert "Real topic content" in text


def test_html_to_text_drops_script_and_style_bodies():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var SECRET_TOKEN=1;</script><p>visible copy</p></body></html>"
    )
    text = ingest_docs.html_to_text(html)
    assert "visible copy" in text
    assert "SECRET_TOKEN" not in text
    assert "color:red" not in text
