from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpe_networking_mcp.pipeline.clients import milvus_client as mc


def test_missing_dependency_is_clear(monkeypatch):
    monkeypatch.setattr(mc, "_load_client_class", lambda: (_ for _ in ()).throw(
        mc.MilvusDependencyError("install `pymilvus[milvus-lite]`")
    ))

    with pytest.raises(mc.MilvusDependencyError, match="pymilvus"):
        mc.MilvusLiteStore("pilot.db")


def test_stable_id_is_repeatable_and_ignores_vector():
    record = {
        "text": "WPA3",
        "source": "developer_docs",
        "file_path": "ssid.md",
        "chunk_index": 4,
        "vector": [1.0, 2.0],
    }
    changed = {**record, "vector": [9.0, 8.0]}
    assert mc.stable_id(record) == mc.stable_id(changed)
    assert len(mc.stable_id(record)) == 64


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"source": "developer_docs"}, "source == 'developer_docs'"),
        ({"source": ["developer_docs", "tech_docs"]},
         "source in ['developer_docs', 'tech_docs']"),
        ({"title": "O'Reilly"}, "title == 'O\\'Reilly'"),
    ],
)
def test_build_filter(metadata, expected):
    assert mc.build_filter(metadata) == expected


def test_build_filter_rejects_expression_injection():
    with pytest.raises(ValueError, match="invalid metadata filter field"):
        mc.build_filter({"source || true": "x"})


class FakeSchema:
    def __init__(self):
        self.fields = []

    def add_field(self, **kwargs):
        self.fields.append(kwargs)


class FakeMilvus:
    def __init__(self):
        self.collections = set()
        self.rows = []
        self.search_kwargs = None

    def has_collection(self, *, collection_name):
        return collection_name in self.collections

    def create_schema(self, **_kwargs):
        return FakeSchema()

    def create_collection(self, *, collection_name, schema):
        self.collections.add(collection_name)

    def upsert(self, *, collection_name, data):
        assert collection_name == "docs"
        self.rows.extend(data)

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        return [[{"id": "stable", "distance": 0.91, "entity": {"source": "docs"}}]]


class HybridMilvus(FakeMilvus):
    def hybrid_search(self, **kwargs):
        self.hybrid_kwargs = kwargs
        return [[{"id": "hybrid", "distance": 0.8, "entity": {"source": "docs"}}]]


def test_store_persists_local_path_and_searches_with_bounded_filter(tmp_path):
    fake = FakeMilvus()
    store = mc.MilvusLiteStore(tmp_path / "nested" / "pilot.db",
                               collection_name="docs", client=fake,
                               data_types=SimpleNamespace(VARCHAR="varchar",
                                                           FLOAT_VECTOR="float_vector"))
    assert store.path == tmp_path / "nested" / "pilot.db"
    assert store.upsert([{"text": "one", "source": "docs", "vector": [1.0, 0.0]}]) == 1

    hits = store.search([1.0, 0.0], top_k=500, metadata_filter={"source": "docs"})

    assert hits == [{"id": "stable", "score": 0.91, "source": "docs"}]
    assert fake.search_kwargs["limit"] == mc.MAX_SEARCH_TOP_K
    assert fake.search_kwargs["filter"] == "source == 'docs'"
    assert fake.search_kwargs["anns_field"] == "vector"


def test_hybrid_reports_unsupported_client(tmp_path):
    with pytest.raises(mc.MilvusCapabilityError, match="does not expose hybrid_search"):
        mc.MilvusLiteStore(tmp_path / "pilot.db", client=FakeMilvus()).hybrid_search(
            search_requests=[], ranker=SimpleNamespace()
        )


def test_hybrid_delegates_only_when_client_exposes_native_api(tmp_path):
    fake = HybridMilvus()
    store = mc.MilvusLiteStore(tmp_path / "pilot.db", client=fake)

    assert store.hybrid_search(search_requests=["dense", "sparse"], ranker="rrf") == [
        {"id": "hybrid", "score": 0.8, "source": "docs"}
    ]
    assert fake.hybrid_kwargs["limit"] == 15
    assert fake.hybrid_kwargs["reqs"] == ["dense", "sparse"]


def test_non_local_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="local .db"):
        mc.MilvusLiteStore(tmp_path / "milvus")
