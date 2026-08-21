"""The spec index must build from the committed corpus alone — no scrape, no network."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline.clients import specs_index
from scripts.build_spec_index import build_spec_index

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "openapi"

REGISTRY_MANIFEST = ROOT / "ingestion" / "openapi_registry_manifest.json"
MANIFEST = VENDOR / "MANIFEST.json"

# The four keys Task 5 and Task 6 consume. ``specs_index.build`` also reports
# ``responses`` and ``skipped``; neither is part of this contract.
CONTRACT_KEYS = {"specs", "endpoints", "schemas", "fields"}

# Every table the builder owns. ``fts`` is an FTS5 virtual table, so it is
# compared through the columns it exposes rather than its shadow-table blobs.
_ROW_TABLES = ("endpoints", "schemas", "fields", "responses")
_FTS_COLUMNS = (
    "kind, spec_file, ref, body, source_family, source_url, "
    "platform, version, spec_version, identity"
)


def _content_digest(db_path: Path) -> str:
    """A digest over every indexed row — content, not just row counts.

    Two builds that agree on ``{"endpoints": 1675, ...}`` can still disagree
    on what those 1,675 rows say, so determinism has to be asserted against
    the rows themselves.
    """
    digest = hashlib.sha256()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table in _ROW_TABLES:
            digest.update(f"\n#{table}\n".encode())
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                digest.update(repr(row).encode())
        digest.update(b"\n#fts\n")
        for row in conn.execute(
            f"SELECT {_FTS_COLUMNS} FROM fts ORDER BY kind, spec_file, ref, body"
        ):
            digest.update(repr(row).encode())
    finally:
        conn.close()
    return digest.hexdigest()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, dict[str, int]]:
    """One build shared by the whole module — indexing 30 specs is not cheap."""
    out = tmp_path_factory.mktemp("spec_index") / "spec_index.db"
    return out, build_spec_index(VENDOR, out)


@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory) -> tuple[Path, dict[str, int]]:
    """An independent second build of the same corpus, for the determinism check."""
    out = tmp_path_factory.mktemp("spec_index_again") / "spec_index.db"
    return out, build_spec_index(VENDOR, out)


def test_build_produces_a_database(built):
    out, _ = built
    assert out.is_file() and out.stat().st_size > 0


def test_build_indexes_endpoints(built):
    _, stats = built
    assert stats["endpoints"] > 0, "no endpoints indexed from the vendored corpus"


def test_build_returns_exactly_the_contract_keys(built):
    _, stats = built
    assert set(stats) == CONTRACT_KEYS
    assert all(stats[key] > 0 for key in CONTRACT_KEYS), stats


def test_endpoints_are_queryable(built):
    out, _ = built
    with sqlite3.connect(out) as conn:
        (count,) = conn.execute("SELECT count(*) FROM endpoints").fetchone()
    assert count > 0


def test_build_is_deterministic(built, rebuilt):
    first, first_stats = built
    second, second_stats = rebuilt
    assert first_stats == second_stats
    assert _content_digest(first) == _content_digest(second)


def test_index_answers_through_the_query_layer(built):
    """The query layer, not raw SQL — an empty ``fts`` looks healthy in a row count.

    ``specs_index.search`` reads ``FROM fts WHERE fts MATCH ?``. A builder that
    fills ``endpoints`` but never writes ``fts`` passes every count assertion
    above and returns nothing for every search the ``lookup_api`` tool makes.
    """
    out, _ = built

    hits = specs_index.search("wlan", db_path=out)
    assert hits, "FTS search returned nothing — the fts table is empty or unindexed"
    assert all(hit["kind"] in {"endpoint", "schema"} for hit in hits)

    endpoints = specs_index.get_endpoint("/network-config", db_path=out)
    assert endpoints, "endpoint lookup returned nothing"
    assert all("/network-config" in row["path"] for row in endpoints)

    assert specs_index.lookup("wlan ssid configuration", db_path=out, top_k=3)


def test_indexed_rows_carry_the_scraped_source_family(built):
    """``vendor/openapi`` must produce the same ``source_family`` the scrape did.

    ``source_family`` is written into every row and is the leading column of
    ``idx_endpoints_source_platform_version``; if the vendored directory name
    changed it, every metadata-filtered query would quietly stop matching.

    ``platform`` is asserted as an exact set for the same reason: it is what
    ``lookup_api``'s ``platform=`` filter matches on, and a document whose
    platform failed to resolve lands as ``NULL`` and disappears from every
    filtered query while still counting as an indexed row.
    """
    out, _ = built
    with sqlite3.connect(out) as conn:
        families = {row[0] for row in conn.execute("SELECT DISTINCT source_family FROM endpoints")}
        platforms = {row[0] for row in conn.execute("SELECT DISTINCT platform FROM endpoints")}
    assert families == {"openapi_specs"}
    assert platforms == {"central", "mist"}


def test_every_vendored_spec_resolves_a_source_url(built):
    """No indexed document may answer "where did this come from?" with NULL.

    The corpus is vendored so it can be redistributed with verifiable
    provenance, and a row with a NULL ``source_url`` has none. This covers all
    31 documents, not the commit-pinned one alone: the registry manifest
    describes 30 of them and the corpus's own ``MANIFEST.json`` describes all
    31, so between the two there is no excuse for a gap.

    The spec-file count is asserted alongside, because "no NULLs" passes
    vacuously on an index that dropped the documents entirely.
    """
    out, _ = built
    declared = {entry["file"] for entry in json.loads(MANIFEST.read_text())["specs"]}
    with sqlite3.connect(out) as conn:
        indexed = {row[0] for row in conn.execute("SELECT DISTINCT spec_file FROM endpoints")}
        unattributed = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT spec_file FROM endpoints "
                "WHERE source_url IS NULL OR source_url = ''"
            )
        ]
    assert indexed == declared, f"not indexed: {declared - indexed}"
    assert not unattributed, f"indexed with no source_url: {unattributed}"


def test_central_provenance_still_comes_from_the_registry_manifest(built):
    """The vendored manifest fills gaps; it must never take over.

    ``ingestion/openapi_registry_manifest.json`` stays authoritative for the
    documents it declares. It supplies ``source_url`` *and* the portal
    ``version`` — the corpus manifest supplies only the former — so a change
    that quietly moved every row onto the fallback would show up here twice:
    as a version that stopped resolving, and as a URL that stopped being the
    portal reference page.
    """
    out, _ = built
    registries = json.loads(REGISTRY_MANIFEST.read_text())["registries"]
    expected = {
        Path(entry["output_path"]).name: entry["source_url"] for entry in registries.values()
    }
    with sqlite3.connect(out) as conn:
        observed = dict(
            conn.execute(
                "SELECT DISTINCT spec_file, source_url FROM endpoints WHERE platform = 'central'"
            )
        )
        versions = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT version FROM endpoints WHERE platform = 'central'"
            )
        }
    assert observed == expected
    assert versions and all(versions), "central rows lost their portal version"


def test_the_registry_manifest_wins_over_the_vendored_one():
    """Precedence at the seam itself, where the two disagree.

    Both manifests record the same ``source_url`` for the 30 Central
    documents, so no build can distinguish "primary won" from "fallback won"
    by value. This asserts the rule directly on conflicting inputs.
    """
    primary = {"a.json": {"source_url": "https://portal.example/a", "version": "v26.05"}}
    fallback = {
        "a.json": {"source_url": "https://raw.example/a"},
        "b.json": {"source_url": "https://raw.example/b"},
    }

    kept = specs_index._resolve_source_meta("a.json", primary, fallback)
    assert kept == {"source_url": "https://portal.example/a", "version": "v26.05"}

    filled = specs_index._resolve_source_meta("b.json", primary, fallback)
    assert filled == {"source_url": "https://raw.example/b"}

    assert specs_index._resolve_source_meta("c.json", primary, fallback) == {}


def test_build_refuses_an_empty_corpus(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="MANIFEST.json"):
        build_spec_index(empty, tmp_path / "out.db")


def test_default_source_dirs_falls_back_to_the_vendored_corpus(tmp_path, monkeypatch):
    """No scrape on disk: the committed corpus stands in, under the same family."""
    monkeypatch.setattr(specs_index, "_DEFAULT_SOURCE_DIRS", {
        "openapi_specs": tmp_path / "absent" / "openapi_specs",
        "product_specs": tmp_path / "absent" / "product_specs",
    })
    resolved = specs_index.default_source_dirs()
    assert resolved["openapi_specs"] == specs_index.VENDOR_OPENAPI_DIR
    assert specs_index._source_family_for_dir(resolved["openapi_specs"]) == "openapi_specs"


def test_default_source_dirs_prefers_a_live_scrape(tmp_path, monkeypatch):
    """A developer with a fresh scrape keeps reading their own output."""
    scraped = tmp_path / "openapi_specs"
    scraped.mkdir()
    (scraped / "spec.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(specs_index, "_DEFAULT_SOURCE_DIRS", {
        "openapi_specs": scraped,
        "product_specs": tmp_path / "product_specs",
    })
    assert specs_index.default_source_dirs()["openapi_specs"] == scraped
