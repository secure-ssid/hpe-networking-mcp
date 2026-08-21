"""A missing spec index degrades honestly; it never looks like a real answer.

Two layers, two contracts:

* The library (``specs_index``) raises. Every query entry point shares one
  ``connect()`` seam, so every one of them must raise the *same* actionable
  ``FileNotFoundError`` naming ``scripts/build_spec_index.py`` — never a raw
  ``sqlite3.OperationalError``, which tells a caller nothing it can act on.
* The MCP tool layer (``rag.lookup_api``) translates that into a degraded
  marker. It must never render as a bare ``[]``: an LLM handed an empty list
  concludes the endpoint does not exist and tells the operator so, which in a
  network-automation tool is a fabrication, not a miss.

Every test passes ``db_path=`` explicitly. ``lookup`` and friends bind
``db_path: Path = DB_PATH`` as a *default argument at import time*, so
rebinding ``specs_index.DB_PATH`` afterwards changes nothing and would
silently exercise the developer's real ``data/specs.sqlite``.
"""

from __future__ import annotations

import functools
import json
import sqlite3

import pytest

from hpe_networking_mcp.mcp_servers import rag
from hpe_networking_mcp.pipeline.clients import specs_index

BUILD_COMMAND_FRAGMENT = "build_spec_index"

FIXTURE_SPECS = {
    "wlan-config.json": {
        "info": {"title": "WLAN Config"},
        "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
        "paths": {
            "/network-config/v1alpha1/wlan-ssids": {
                "post": {
                    "operationId": "createWlanSsid",
                    "summary": "Create a WLAN SSID",
                    "description": "Create a WLAN SSID profile for a scope.",
                },
            },
        },
        "components": {"schemas": {}},
    },
}


def _query_calls(db_path):
    """Every ``specs_index`` entry point that opens the index to read it.

    Keyed by name so a failure names the offender. ``get_response_description``
    is deliberately absent: it documents itself as never raising and is
    asserted separately.
    """
    return {
        "lookup": lambda: specs_index.lookup("create a wlan ssid", db_path=db_path),
        "search": lambda: specs_index.search("wlan", db_path=db_path),
        "get_endpoint": lambda: specs_index.get_endpoint("wlan-ssids", db_path=db_path),
        "get_exact_endpoint": lambda: specs_index.get_exact_endpoint(
            "POST", "/network-config/v1alpha1/wlan-ssids", db_path=db_path
        ),
        "get_endpoint_by_operation_id": lambda: specs_index.get_endpoint_by_operation_id(
            "createWlanSsid", db_path=db_path
        ),
        "get_schema": lambda: specs_index.get_schema("Wlan", db_path=db_path),
        "get_enum": lambda: specs_index.get_enum("auth-type", db_path=db_path),
    }


@pytest.fixture
def absent_index(tmp_path):
    path = tmp_path / "absent.sqlite"
    assert not path.exists()
    return path


@pytest.fixture
def built_index(tmp_path):
    specs_dir = tmp_path / "openapi_specs"
    specs_dir.mkdir()
    for name, spec in FIXTURE_SPECS.items():
        (specs_dir / name).write_text(json.dumps(spec))
    db_path = tmp_path / "built.sqlite"
    counts = specs_index.build(specs_dir=specs_dir, db_path=db_path)
    assert counts["endpoints"] == 1
    return db_path


class TestLibraryRaisesOneActionableError:
    @pytest.mark.parametrize("name", sorted(_query_calls("/nonexistent")))
    def test_every_query_entry_point_raises_file_not_found(self, absent_index, name):
        specs_index.clear_lookup_cache()
        with pytest.raises(FileNotFoundError) as excinfo:
            _query_calls(absent_index)[name]()
        assert BUILD_COMMAND_FRAGMENT in str(excinfo.value), name

    @pytest.mark.parametrize("name", sorted(_query_calls("/nonexistent")))
    def test_no_sqlite_error_reaches_the_caller(self, absent_index, name):
        specs_index.clear_lookup_cache()
        try:
            _query_calls(absent_index)[name]()
        except sqlite3.Error as exc:  # pragma: no cover - the failure we guard
            pytest.fail(f"{name} leaked a raw sqlite error: {exc!r}")
        except FileNotFoundError:
            pass

    def test_error_names_the_offline_corpus_so_no_scrape_is_attempted(self, absent_index):
        with pytest.raises(FileNotFoundError, match="vendor/openapi"):
            specs_index.lookup("create a wlan ssid", db_path=absent_index)

    def test_error_names_the_missing_path(self, absent_index):
        with pytest.raises(FileNotFoundError, match=str(absent_index.name)):
            specs_index.search("wlan", db_path=absent_index)

    def test_best_effort_response_lookup_still_degrades_to_none(self, absent_index):
        # Documented as never raising: reactive error enrichment, not a query
        # a user depends on. It must keep that contract through the new seam.
        assert specs_index.get_response_description("central", 429, db_path=absent_index) is None


class TestNoDatabaseIsMaterialized:
    """A read against a missing index must not leave a zero-byte file behind.

    ``sqlite3.connect`` on a plain path creates one, and everything that probes
    with ``is_file()`` would then believe an index exists.
    """

    @pytest.mark.parametrize("name", sorted(_query_calls("/nonexistent")))
    def test_query_creates_no_database_file(self, absent_index, name):
        specs_index.clear_lookup_cache()
        with pytest.raises(FileNotFoundError):
            _query_calls(absent_index)[name]()
        assert not absent_index.exists(), f"{name} materialized {absent_index}"

    def test_response_description_creates_no_database_file(self, absent_index):
        specs_index.get_response_description("central", 429, db_path=absent_index)
        assert not absent_index.exists()


def _lookup_api_against(monkeypatch, db_path, query):
    """Run the real ``lookup_api`` tool with ``specs_index`` pointed at ``db_path``.

    ``lookup_api`` takes no ``db_path`` — it is an MCP tool signature and stays
    that way. Binding the path with ``functools.partial`` redirects *where* the
    index is read from while still running the real ``specs_index.lookup``
    (real SQLite, real degradation path), rather than stubbing its behaviour.
    """
    monkeypatch.setattr(
        specs_index,
        "lookup",
        functools.partial(specs_index.lookup, db_path=db_path),
    )
    specs_index.clear_lookup_cache()
    return rag.lookup_api(query)


class TestToolLayerRendersDegradation:
    def test_missing_index_is_marked_degraded(self, monkeypatch, absent_index):
        result = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")
        assert result[0]["degraded"] is True

    def test_degraded_marker_explains_how_to_fix_it(self, monkeypatch, absent_index):
        result = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")
        assert BUILD_COMMAND_FRAGMENT in result[0]["hint"]

    def test_hint_is_the_remedy_alone_and_error_is_the_full_diagnostic(
        self, monkeypatch, absent_index
    ):
        # Two keys must earn their place. ``error`` says which file was looked
        # for; ``hint`` says what to run. Identical strings under both would
        # cost the model a second read for nothing.
        entry = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")[0]
        assert entry["hint"] != entry["error"]
        assert str(absent_index) in entry["error"]
        assert str(absent_index) not in entry["hint"]

    def test_hint_is_the_shared_constant_not_a_second_copy(self, monkeypatch, absent_index):
        # Sliced or restated command text drifts from the exception's the
        # moment either is reworded.
        entry = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")[0]
        assert entry["hint"] == specs_index.MISSING_INDEX_REMEDY
        assert entry["hint"] in entry["error"]

    def test_degraded_marker_is_not_an_empty_list(self, monkeypatch, absent_index):
        result = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")
        assert result != []

    def test_degraded_marker_carries_no_fabricated_hits(self, monkeypatch, absent_index):
        result = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")
        assert len(result) == 1
        assert not result[0].get("text")
        assert not result[0].get("file_path")


class TestMissingIndexIsDistinguishableFromNoMatch:
    """The fabrication guard — the reason this file exists.

    A built index with nothing to say returns ``[]``. That is a true statement:
    the specs were consulted and hold no answer, so the caller should fall back
    to prose search. A *missing* index returns the degraded marker. If the two
    rendered the same way, an operator asking about a real endpoint on a
    checkout with no index would be told the endpoint does not exist.
    """

    def test_built_index_with_no_match_returns_empty(self, monkeypatch, built_index):
        result = _lookup_api_against(
            monkeypatch, built_index, "quantum teleportation flux capacitor calibration"
        )
        assert result == []

    def test_built_index_with_a_match_returns_hits(self, monkeypatch, built_index):
        # Also proves the redirect in ``_lookup_api_against`` actually takes
        # effect: these hits can only come from the fixture index.
        result = _lookup_api_against(monkeypatch, built_index, "createWlanSsid")
        assert result and "wlan-config.json" in result[0]["file_path"]

    def test_no_match_and_missing_index_do_not_render_alike(
        self, monkeypatch, built_index, absent_index
    ):
        no_match = _lookup_api_against(
            monkeypatch, built_index, "quantum teleportation flux capacitor calibration"
        )
        missing = _lookup_api_against(monkeypatch, absent_index, "create a wlan ssid")

        assert no_match == []
        assert missing != []
        assert missing[0]["degraded"] is True
        # A genuine no-match must never claim degradation: that would send the
        # caller off rebuilding an index that is already complete.
        assert not any(hit.get("degraded") for hit in no_match)
