"""Unit tests for the ``responses`` table — the engine behind reactive error
hints (``get_response_description``) — and its ingestion-time dependencies
(``_platform_for_server``, ``_response_description``).

The fixture specs in ``test_specs_index_lookup.py`` deliberately use
``https://example.test/...`` servers so ``_platform_for_server`` never
matches, meaning that file exercises endpoints/schemas/fields but never
populates ``responses`` at all. This file uses realistic Central/Mist/shared
server URLs so the new table's ingestion and query logic gets real coverage.

Test bar:
- platform derivation matches Central/Mist real hostnames and returns None
  for the shared HPE SSO host and for empty/unknown hosts
- a response using OpenAPI's local $ref reusable-response pattern resolves
  to its named ``components.responses`` description
- responses are only captured for a spec whose platform could be resolved
- majority-vote dedups identical (method, path, status_code, description)
  rows before counting, so a mirrored spec file can't outvote a distinct one
- a dominant description (>= min_share) wins; a genuine split returns None
- unknown platform/status_code, a missing db, and a pre-rebuild db with no
  ``responses`` table all degrade to None rather than raising
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hpe_networking_mcp.pipeline.clients import specs_index

# One spec per platform, each with an inline-description response AND a
# $ref-based reusable response (the realistic OpenAPI authoring pattern for
# shared 401/403/429 shapes), plus a shared/no-platform spec whose responses
# must NOT be captured at all.
RESPONSE_FIXTURE_SPECS = {
    "central-vlans.json": {
        "info": {"title": "Central VLANs"},
        "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
        "paths": {
            "/network-config/v1/layer2-vlan/{id}": {
                "get": {
                    "operationId": "getVlan",
                    "responses": {
                        "200": {"description": "OK"},
                        "404": {"description": "VLAN not found."},
                        "429": {"$ref": "#/components/responses/RateLimited"},
                        "401": {"$ref": "#/components/responses/Unauthorized"},
                    },
                },
            },
        },
        "components": {
            "schemas": {},
            "responses": {
                "RateLimited": {"description": "Too many requests; back off and retry."},
                "Unauthorized": {"description": "Missing or invalid bearer token."},
            },
        },
    },
    # A second, mirrored Central spec defining the SAME operation with the
    # SAME response text -- exactly the "grouped config bundle + per-feature
    # bundle" duplication the real Aruba spec corpus ships. Must not be
    # allowed to outvote a distinct minority answer for the same code.
    "central-vlans-mirror.json": {
        "info": {"title": "Central VLANs (mirror)"},
        "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
        "paths": {
            "/network-config/v1/layer2-vlan/{id}": {
                "get": {
                    "operationId": "getVlan",
                    "responses": {
                        "404": {"description": "VLAN not found."},
                        "401": {"$ref": "#/components/responses/Unauthorized"},
                    },
                },
            },
        },
        "components": {
            "schemas": {},
            "responses": {
                "Unauthorized": {"description": "Missing or invalid bearer token."},
            },
        },
    },
    # A distinct Central endpoint whose 404 means something genuinely
    # different -- with the mirror counted only once, this is 1 vote against
    # the vlan-not-found wording's 1 (deduped) vote: a real split, not a
    # landslide, so majority-vote must return None for 404 on this platform.
    "central-firmware.json": {
        "info": {"title": "Central Firmware"},
        "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
        "paths": {
            "/network-services/v1alpha1/firmware-details/{serial}": {
                "get": {
                    "operationId": "getFirmware",
                    "responses": {
                        "404": {"description": "Device serial not found in this scope."},
                    },
                },
            },
        },
        "components": {"schemas": {}},
    },
    "mist-sites.json": {
        "info": {"title": "Mist Sites"},
        "servers": [{"url": "https://api.mist.com/api/v1"}],
        "paths": {
            "/sites/{site_id}": {
                "get": {
                    "operationId": "getSiteInfo",
                    "responses": {
                        "200": {"description": "OK"},
                        "429": {"$ref": "#/components/responses/RateLimited"},
                    },
                },
            },
        },
        "components": {
            "schemas": {},
            "responses": {
                "RateLimited": {"description": "API rate limit exceeded for this token."},
            },
        },
    },
    # A shared authorization spec with no single-platform host -- its
    # responses must be skipped entirely, not attributed to any platform.
    "authorization.json": {
        "info": {"title": "HPE SSO Authorization"},
        "servers": [{"url": "https://sso.common.cloud.hpe.com"}],
        "paths": {
            "/oauth2/token": {
                "post": {
                    "operationId": "createToken",
                    "responses": {
                        "401": {"description": "Invalid client credentials."},
                    },
                },
            },
        },
        "components": {"schemas": {}},
    },
}


@pytest.fixture
def responses_db(tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for fname, spec in RESPONSE_FIXTURE_SPECS.items():
        (specs_dir / fname).write_text(json.dumps(spec))
    db_path = tmp_path / "specs.sqlite"
    counts = specs_index.build(specs_dir=specs_dir, db_path=db_path)
    return db_path, counts


class TestPlatformForServer:
    def test_central_hostname(self):
        assert specs_index._platform_for_server(
            "https://apigw-prod2.central.arubanetworks.com"
        ) == "central"

    def test_mist_hostname(self):
        assert specs_index._platform_for_server("https://api.mist.com/api/v1") == "mist"

    def test_shared_sso_hostname_returns_none(self):
        assert specs_index._platform_for_server("https://sso.common.cloud.hpe.com") is None

    def test_unknown_hostname_returns_none(self):
        assert specs_index._platform_for_server("https://example.test/whatever") is None

    def test_empty_or_none_returns_none(self):
        assert specs_index._platform_for_server("") is None
        assert specs_index._platform_for_server(None) is None


class TestResponseDescriptionRefResolution:
    def test_ref_response_resolves_to_named_component(self):
        spec = RESPONSE_FIXTURE_SPECS["central-vlans.json"]
        resp = {"$ref": "#/components/responses/RateLimited"}
        assert (
            specs_index._response_description(spec, resp)
            == "Too many requests; back off and retry."
        )

    def test_inline_response_used_directly(self):
        resp = {"description": "VLAN not found."}
        assert specs_index._response_description({}, resp) == "VLAN not found."

    def test_unresolvable_ref_returns_empty_string(self):
        resp = {"$ref": "#/components/responses/DoesNotExist"}
        assert specs_index._response_description({"components": {"responses": {}}}, resp) == ""

    def test_missing_description_returns_empty_string(self):
        assert specs_index._response_description({}, {}) == ""


class TestResponsesIngestion:
    def test_responses_only_captured_for_resolvable_platform(self, responses_db):
        _db_path, counts = responses_db
        # central-vlans (4) + central-vlans-mirror (2) + central-firmware (1)
        # + mist-sites (2) = 9. authorization.json's 401 is NOT counted: its
        # host has no resolvable platform.
        assert counts["responses"] == 9

    def test_shared_sso_spec_contributes_no_response_rows(self, responses_db):
        db_path, _counts = responses_db
        conn = specs_index.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM responses WHERE spec_file = 'authorization.json'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_ref_based_response_lands_with_resolved_description(self, responses_db):
        db_path, _counts = responses_db
        conn = specs_index.connect(db_path)
        try:
            row = conn.execute(
                "SELECT description FROM responses WHERE platform = 'mist' AND status_code = '429'"
            ).fetchone()
        finally:
            conn.close()
        assert row["description"] == "API rate limit exceeded for this token."

    def test_counts_dict_gained_responses_key_additively(self, responses_db):
        # Existing callers only assert individual keys (see
        # test_specs_index_lookup.py), never exact dict equality, so a new
        # "responses" key must not need any caller update.
        _db_path, counts = responses_db
        assert counts.keys() >= {"specs", "endpoints", "schemas", "fields", "responses", "skipped"}


class TestGetResponseDescription:
    def test_dominant_description_wins_after_dedup(self, responses_db):
        db_path, _counts = responses_db
        # "Missing or invalid bearer token." appears in both central-vlans.json
        # and central-vlans-mirror.json for the SAME (method, path) -- deduped
        # to one vote, and is the ONLY distinct 401 answer for central, so it
        # must win outright regardless of the dedup.
        assert (
            specs_index.get_response_description("central", 401, db_path=db_path)
            == "Missing or invalid bearer token."
        )

    def test_mirrored_spec_does_not_manufacture_a_false_majority(self, responses_db):
        db_path, _counts = responses_db
        # "VLAN not found." is deduped to ONE vote (identical row mirrored in
        # two spec files for the same method+path); "Device serial not found
        # in this scope." is a distinct endpoint's genuine second vote. 1-vs-1
        # is a real split (no group reaches the 0.6 default share), so this
        # must return None -- if the mirror were double-counted instead of
        # deduped, "VLAN not found." would incorrectly win 2-to-1.
        assert specs_index.get_response_description("central", 404, db_path=db_path) is None

    def test_single_platform_response_is_returned(self, responses_db):
        db_path, _counts = responses_db
        assert (
            specs_index.get_response_description("mist", 429, db_path=db_path)
            == "API rate limit exceeded for this token."
        )

    def test_unknown_platform_returns_none(self, responses_db):
        db_path, _counts = responses_db
        assert specs_index.get_response_description("glp", 401, db_path=db_path) is None

    def test_unknown_status_code_returns_none(self, responses_db):
        db_path, _counts = responses_db
        assert specs_index.get_response_description("central", 599, db_path=db_path) is None

    def test_empty_platform_returns_none(self, responses_db):
        db_path, _counts = responses_db
        assert specs_index.get_response_description("", 401, db_path=db_path) is None
        assert specs_index.get_response_description(None, 401, db_path=db_path) is None

    def test_accepts_int_or_str_status_code(self, responses_db):
        db_path, _counts = responses_db
        by_int = specs_index.get_response_description("mist", 429, db_path=db_path)
        by_str = specs_index.get_response_description("mist", "429", db_path=db_path)
        assert by_int == by_str == "API rate limit exceeded for this token."

    def test_missing_db_file_returns_none_and_creates_nothing(self, tmp_path):
        """Degrading to ``None`` is only half the contract.

        ``sqlite3.connect`` opens read-write and creates an empty database
        when the path is missing, so a read here used to leave a zero-byte
        ``data/specs.sqlite`` behind in a corpus-free checkout. Every probe
        that only checks ``is_file()`` then reports an index that has no
        tables, and derived-fact tooling fails instead of taking its no-data
        path.
        """
        missing = tmp_path / "nope.sqlite"
        assert specs_index.get_response_description("central", 401, db_path=missing) is None
        assert not missing.exists()

    def test_pre_rebuild_db_without_responses_table_returns_none_not_raise(self, responses_db):
        """A real data/specs.sqlite built before this feature shipped has
        every other table but no ``responses`` table at all -- this must
        degrade to None, never raise, so old/un-rebuilt indexes keep working."""
        db_path, _counts = responses_db
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE responses")
            conn.commit()
        finally:
            conn.close()
        assert specs_index.get_response_description("central", 401, db_path=db_path) is None

    def test_corrupt_db_returns_none_not_raise(self, tmp_path):
        corrupt = tmp_path / "specs.sqlite"
        corrupt.write_bytes(b"not a sqlite file")
        assert specs_index.get_response_description("central", 401, db_path=corrupt) is None
