"""A caller must be able to weigh an answer, not just receive it.

``lookup_api`` answers from the committed ``vendor/openapi`` corpus and
``ask_docs``/``search_docs`` answer from a locally built prose index. Both
return text with no indication of *what* produced it, so a model cannot tell a
2026 spec from a two-year-old scrape, nor an MIT-licensed Juniper document
from proprietary HPE material. ``corpus_provenance`` is the one place that
question is answerable.

The invariant this file exists to defend is the same one the rest of the
hardening work has been defending: **three different absences must stay three
different facts.**

1. the corpus is not there,
2. the corpus is there but the index over it was never built,
3. the corpus is there, the index is built, and it simply holds nothing.

Collapsing (1) or (2) into (3) is the fabrication mode -- a model handed
"0 documents" or an empty list concludes the material does not exist and tells
an operator so. Collapsing (2) into (1) sends that operator to re-fetch 23 MB
they already have instead of running one offline build command.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.mcp_servers import rag
from hpe_networking_mcp.pipeline.clients import specs_index

MIST_FILE = "mist.openapi.json"


def _vendor_manifest() -> dict:
    path = specs_index.VENDOR_OPENAPI_DIR / specs_index.VENDOR_MANIFEST_NAME
    return json.loads(path.read_text())


@pytest.fixture
def empty_corpus(tmp_path, monkeypatch):
    """Point the tool at a directory holding no vendored corpus at all."""
    corpus = tmp_path / "vendor-openapi"
    corpus.mkdir()
    monkeypatch.setattr(specs_index, "VENDOR_OPENAPI_DIR", corpus)
    return corpus


@pytest.fixture
def prose_dir(tmp_path, monkeypatch):
    """Point the prose section at an empty local data directory."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(rag, "PROSE_DATA_DIR", data)
    return data


class TestVendoredCorpusOnACleanCheckout:
    """The committed corpus is provenance the repository can always answer."""

    def test_reports_the_committed_corpus_without_network_or_local_build(self):
        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is True
        assert api_specs["document_count"] == len(_vendor_manifest()["specs"])
        assert api_specs["api_paths"] == sum(
            entry["path_count"] for entry in _vendor_manifest()["specs"]
        )
        assert api_specs["fetched"]["earliest"] <= api_specs["fetched"]["latest"]
        assert api_specs["notice"].endswith("NOTICE.md")
        assert "lookup_api" in api_specs["backs"]

    def test_reports_every_manifest_file_as_present_on_disk(self):
        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["files_missing"] == []

    def test_keeps_the_two_licence_regimes_apart(self):
        """NOTICE.md: 30 proprietary HPE documents, one MIT Juniper document.

        A single merged licence line would let a redistributor read "MIT" over
        material that is not MIT, which is the one claim this corpus must
        never make.
        """
        licenses = rag.corpus_provenance()["api_specs"]["licenses"]

        by_text = {entry["license"]: entry for entry in licenses}
        assert len(by_text) == 2
        assert by_text["MIT"]["document_count"] == 1
        proprietary = [text for text in by_text if text != "MIT"]
        assert "Proprietary" in proprietary[0]
        assert by_text[proprietary[0]]["document_count"] == 30


class TestThreeAbsencesStayThreeFacts:
    def test_an_unbuilt_index_never_reads_as_a_missing_corpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(specs_index, "DB_PATH", tmp_path / "absent.sqlite")

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is True
        assert api_specs["document_count"] > 0
        assert api_specs["index"]["built"] is False
        assert api_specs["index"]["remedy"] == specs_index.MISSING_INDEX_REMEDY

    def test_a_missing_corpus_never_reads_as_an_unbuilt_index(
        self, tmp_path, monkeypatch, empty_corpus
    ):
        built = tmp_path / "specs.sqlite"
        built.write_bytes(b"")
        monkeypatch.setattr(specs_index, "DB_PATH", built)

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is False
        assert api_specs["index"]["built"] is True
        assert "document_count" not in api_specs
        assert "vendor_openapi_corpus.py" in api_specs["remedy"]

    def test_a_missing_corpus_is_never_reported_as_zero_documents(self, empty_corpus):
        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs.get("document_count") is None
        assert api_specs.get("licenses") is None


class TestMalformedManifestDegrades:
    def test_truncated_json_degrades_instead_of_raising(self, empty_corpus):
        (empty_corpus / specs_index.VENDOR_MANIFEST_NAME).write_text('{"specs": [{"file":')

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is False
        assert api_specs["error"]
        assert api_specs["remedy"]

    def test_a_manifest_without_a_spec_list_degrades(self, empty_corpus):
        (empty_corpus / specs_index.VENDOR_MANIFEST_NAME).write_text('{"schema_version": 1}')

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is False
        assert api_specs.get("document_count") is None

    def test_unreadable_entries_are_counted_not_silently_dropped(self, empty_corpus):
        (empty_corpus / "kept.json").write_text("{}")
        (empty_corpus / specs_index.VENDOR_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "specs": [
                        {"file": "kept.json", "path_count": 2, "license": "MIT"},
                        "not-an-object",
                        {"no_file_key": True},
                    ],
                }
            )
        )

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is True
        assert api_specs["document_count"] == 1
        assert api_specs["unreadable_entries"] == 2

    def test_a_declared_file_absent_from_disk_is_named(self, empty_corpus):
        (empty_corpus / specs_index.VENDOR_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "specs": [{"file": "gone.json", "path_count": 1, "license": "MIT"}],
                }
            )
        )

        api_specs = rag.corpus_provenance()["api_specs"]

        assert api_specs["available"] is True
        assert api_specs["files_missing"] == ["gone.json"]


class TestDetailIsOptIn:
    """A model-facing tool pays for every token it returns by default."""

    def test_the_default_call_carries_no_per_document_rows(self):
        api_specs = rag.corpus_provenance()["api_specs"]

        assert "documents" not in api_specs
        assert api_specs["detail_hint"]

    def test_detail_returns_the_full_pinned_entry_for_every_document(self):
        documents = rag.corpus_provenance(detail=True)["api_specs"]["documents"]

        assert len(documents) == len(_vendor_manifest()["specs"])
        mist = next(entry for entry in documents if entry["file"] == MIST_FILE)
        assert mist["license"] == "MIT"
        assert len(mist["sha256"]) == 64
        assert mist["source_url"].startswith("https://")
        assert mist["fetched"]
        assert mist["pin"]["upstream_commit"]

    def test_a_lookup_api_file_path_resolves_to_one_document(self):
        """``lookup_api`` hits carry ``openapi_specs/<file>#<ref>`` file paths.

        Handing that value straight back is the whole point: it is what a
        caller holds when it wants to know what backed the answer it just got.
        """
        result = rag.corpus_provenance(spec=f"openapi_specs/{MIST_FILE}#Wlan")

        documents = result["api_specs"]["documents"]
        assert [entry["file"] for entry in documents] == [MIST_FILE]

    def test_an_unknown_document_is_an_empty_selection_not_a_missing_corpus(self):
        result = rag.corpus_provenance(spec="no-such-spec.json")["api_specs"]

        assert result["available"] is True
        assert result["documents"] == []
        assert "no-such-spec.json" in result["note"]


class TestProseCorpus:
    def test_absence_carries_a_remedy_and_never_an_empty_source_list(self, prose_dir):
        prose = rag.corpus_provenance()["prose_docs"]

        assert prose["available"] is False
        assert prose["sources"] is None
        assert "ingest_docs.py" in prose["remedy"]
        assert "search_docs" in prose["backs"]

    def test_a_built_index_reports_its_sources_and_build_time(self, prose_dir):
        (prose_dir / rag.PROSE_DOCS_INDEX_NAME).mkdir()
        (prose_dir / rag.PROSE_INDEX_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "artifacts": {"docs.lance": {"modified_at": "2026-08-20T10:00:00+00:00"}},
                    "sources": {
                        "tech_docs": {
                            "present": True,
                            "indexed_chunk_count": 2014,
                            "last_refreshed_at": "2026-08-15T09:00:00+00:00",
                            "required": False,
                        }
                    },
                }
            )
        )

        prose = rag.corpus_provenance()["prose_docs"]

        assert prose["available"] is True
        assert prose["built_at"] == "2026-08-20T10:00:00+00:00"
        assert prose["sources"] == [
            {
                "source": "tech_docs",
                "indexed_chunks": 2014,
                "last_refreshed_at": "2026-08-15T09:00:00+00:00",
                "required": False,
            }
        ]

    def test_a_built_index_with_no_manifest_is_not_reported_as_sourceless(self, prose_dir):
        (prose_dir / rag.PROSE_DOCS_INDEX_NAME).mkdir()

        prose = rag.corpus_provenance()["prose_docs"]

        assert prose["available"] is True
        assert prose["sources"] is None
        assert "package_indexes.py" in prose["remedy"]

    def test_a_malformed_index_manifest_degrades(self, prose_dir):
        (prose_dir / rag.PROSE_DOCS_INDEX_NAME).mkdir()
        (prose_dir / rag.PROSE_INDEX_MANIFEST_NAME).write_text("{not json")

        prose = rag.corpus_provenance()["prose_docs"]

        assert prose["available"] is True
        assert prose["sources"] is None
        assert prose["error"]


class TestRegisteredOnTheMcpSurface:
    @pytest.mark.anyio
    async def test_listed_and_callable_through_a_real_client(self):
        from mcp.client._memory import InMemoryTransport
        from mcp.client.session import ClientSession

        async with InMemoryTransport(rag.mcp) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert "corpus_provenance" in names
                tool = next(t for t in listed.tools if t.name == "corpus_provenance")
                assert tool.annotations.read_only_hint is True

                called = await session.call_tool("corpus_provenance", {})

        payload = json.loads(called.content[0].text)
        assert payload["api_specs"]["available"] is True
        assert payload["api_specs"]["document_count"] > 0
