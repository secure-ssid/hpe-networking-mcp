"""Unit tests for ``error_help.reactive_hint`` -- the combined spec-grounded
+ generic-fallback enrichment for a failed MCP tool call's status code.

Covers: status-code coercion/validation (int, digit string, bool rejection,
2xx no-op, unknown code), the generic fallback table on its own, the
spec-grounded half layered on top of it via a real (fixture-built) specs
index, and every degradation path (no platform, unresolved platform,
missing db, pre-rebuild db without a ``responses`` table) yielding the
generic half rather than raising or returning nothing.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline.clients import error_help, specs_index


@pytest.fixture
def mist_rate_limit_db(tmp_path):
    """A tiny built specs index with exactly one platform/status_code
    response, spec-grounded via OpenAPI's $ref reusable-response pattern."""
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "mist-sites.json").write_text(
        json.dumps(
            {
                "info": {"title": "Mist Sites"},
                "servers": [{"url": "https://api.mist.com/api/v1"}],
                "paths": {
                    "/sites/{site_id}": {
                        "get": {
                            "operationId": "getSiteInfo",
                            "responses": {
                                "429": {"$ref": "#/components/responses/RateLimited"},
                            },
                        },
                    },
                },
                "components": {
                    "schemas": {},
                    "responses": {
                        "RateLimited": {
                            "description": "API rate limit exceeded for this token.",
                        },
                    },
                },
            }
        )
    )
    db_path = tmp_path / "specs.sqlite"
    specs_index.build(specs_dir=specs_dir, db_path=db_path)
    return db_path


class TestStatusCodeCoercion:
    def test_2xx_returns_none(self):
        assert error_help.reactive_hint("get_thing", 200) is None
        assert error_help.reactive_hint("get_thing", 299) is None

    def test_non_numeric_string_returns_none(self):
        assert error_help.reactive_hint("get_thing", "oops") is None

    def test_bool_is_rejected_even_though_bool_is_an_int_subclass(self):
        assert error_help.reactive_hint("get_thing", True) is None
        assert error_help.reactive_hint("get_thing", False) is None

    def test_none_status_code_returns_none(self):
        assert error_help.reactive_hint("get_thing", None) is None

    def test_digit_string_is_accepted(self):
        assert error_help.reactive_hint("get_thing", "404") == error_help._GENERIC_STATUS_HINTS[404]

    def test_unknown_status_code_with_no_platform_returns_none(self):
        assert error_help.reactive_hint("get_thing", 418) is None


class TestGenericFallbackOnly:
    def test_common_codes_return_their_generic_text(self):
        for code in (400, 401, 403, 404, 409, 422, 429, 500, 503):
            hint = error_help.reactive_hint("get_thing", code)
            assert hint == error_help._GENERIC_STATUS_HINTS[code]

    def test_no_platform_argument_still_returns_generic(self):
        assert error_help.reactive_hint("get_thing", 429) == error_help._GENERIC_STATUS_HINTS[429]

    def test_tool_name_is_accepted_but_not_required_to_matter(self):
        # tool_name is reserved for a future enrichment; any value (including
        # None) must not change the outcome for the same status code.
        a = error_help.reactive_hint("get_thing", 404)
        b = error_help.reactive_hint(None, 404)
        c = error_help.reactive_hint("totally_different_tool", 404)
        assert a == b == c


class TestSpecGroundedEnrichment:
    def test_spec_grounded_text_is_appended_to_generic(self, mist_rate_limit_db):
        hint = error_help.reactive_hint(
            "get_site_info", 429, platform="mist", db_path=mist_rate_limit_db
        )
        assert hint.startswith(error_help._GENERIC_STATUS_HINTS[429])
        assert "API rate limit exceeded for this token." in hint

    def test_unresolved_platform_falls_back_to_generic_only(self, mist_rate_limit_db):
        hint = error_help.reactive_hint(
            "get_site_info", 429, platform="glp", db_path=mist_rate_limit_db
        )
        assert hint == error_help._GENERIC_STATUS_HINTS[429]

    def test_code_with_no_generic_entry_can_still_surface_spec_text_alone(
        self, tmp_path
    ):
        # 418 has no entry in _GENERIC_STATUS_HINTS -- build a fixture where
        # the spec index has an answer for it anyway, to prove the
        # spec-grounded half stands alone when there's no generic text to
        # combine it with.
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "mist-teapot.json").write_text(
            json.dumps(
                {
                    "info": {"title": "Mist Teapot"},
                    "servers": [{"url": "https://api.mist.com/api/v1"}],
                    "paths": {
                        "/teapots/{id}": {
                            "get": {
                                "operationId": "getTeapot",
                                "responses": {
                                    "418": {"description": "This teapot is a teapot."},
                                },
                            },
                        },
                    },
                    "components": {"schemas": {}},
                }
            )
        )
        db_path = tmp_path / "specs.sqlite"
        specs_index.build(specs_dir=specs_dir, db_path=db_path)
        hint = error_help.reactive_hint("get_teapot", 418, platform="mist", db_path=db_path)
        assert hint == "This API documents 418 here as: This teapot is a teapot."

    def test_missing_db_degrades_to_generic_only_not_raise(self, tmp_path):
        missing = tmp_path / "nope.sqlite"
        hint = error_help.reactive_hint("get_site_info", 429, platform="mist", db_path=missing)
        assert hint == error_help._GENERIC_STATUS_HINTS[429]

    def test_pre_rebuild_db_without_responses_table_degrades_to_generic(
        self, mist_rate_limit_db
    ):
        import sqlite3

        conn = sqlite3.connect(mist_rate_limit_db)
        try:
            conn.execute("DROP TABLE responses")
            conn.commit()
        finally:
            conn.close()
        hint = error_help.reactive_hint(
            "get_site_info", 429, platform="mist", db_path=mist_rate_limit_db
        )
        assert hint == error_help._GENERIC_STATUS_HINTS[429]

    def test_corrupt_db_degrades_to_generic_not_raise(self, tmp_path):
        corrupt = tmp_path / "specs.sqlite"
        corrupt.write_bytes(b"not a sqlite file")
        hint = error_help.reactive_hint("get_site_info", 429, platform="mist", db_path=corrupt)
        assert hint == error_help._GENERIC_STATUS_HINTS[429]
