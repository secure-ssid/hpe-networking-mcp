"""Regression tests for the nested-stringified-blob redaction/tokenization
hardening fix.

Problem this closes: ``shared.redact_sensitive``, ``SecretTokenizeMiddleware``,
and ``PIITokenizeMiddleware`` all walked a result tree looking for
sensitive/PII-*keyed* string values, but only ever inspected a string
value's *immediate* parent key. Some APIs return a field whose value is
itself a JSON- or Python-repr-serialized dict/list carrying nested details
as one opaque string (for example, a generically-named annotation/scope
field). A sensitive or PII value hidden inside that blob was invisible to
all three redaction/tokenization paths regardless of how the outer field
happened to be named -- this is the "device serials leaking inside a
stringified `@`-annotation blob" defect class.

Covers:
- ``shared.parse_stringified_container`` / ``serialize_stringified_container``:
  the shared, stateless, quote-agnostic (JSON + Python-repr) parse/reserialize
  primitives, including the cheap bracket/length pre-check that keeps
  ordinary strings a zero-parse-attempt no-op.
- ``shared.redact_sensitive``: a sensitive field nested inside a stringified
  blob (either dialect) is now redacted; a blob with nothing sensitive
  inside is returned byte-for-byte unchanged (no accidental reformatting).
- ``SecretTokenizeMiddleware`` / ``PIITokenizeMiddleware``: the same blob
  awareness in ``_walk_tokenize``/``_walk_resolve``, including the full
  read-then-write round trip through a blob-nested token.
- Safety guards: strings that don't look like a container, and strings
  longer than the size cap, are never parsed or mutated.
"""

from __future__ import annotations

import pytest

import hpe_networking_mcp.mcp_servers.shared as sh
from hpe_networking_mcp.mcp_servers._middleware.pii_tokenizer import (
    _TOKEN_RE as _PII_TOKEN_RE,
)
from hpe_networking_mcp.mcp_servers._middleware.pii_tokenizer import (
    PIITokenizeMiddleware,
)
from hpe_networking_mcp.mcp_servers._middleware.secret_tokenizer import (
    _TOKEN_RE as _SECRET_TOKEN_RE,
)
from hpe_networking_mcp.mcp_servers._middleware.secret_tokenizer import (
    SecretTokenizeMiddleware,
)


# ---------------------------------------------------------------------------
# parse_stringified_container / serialize_stringified_container
# ---------------------------------------------------------------------------
class TestParseStringifiedContainer:
    def test_parses_json_dict(self):
        parsed, dialect = sh.parse_stringified_container('{"a": 1, "b": "two"}')
        assert parsed == {"a": 1, "b": "two"}
        assert dialect == "json"

    def test_parses_json_list(self):
        parsed, dialect = sh.parse_stringified_container('["a", "b", 3]')
        assert parsed == ["a", "b", 3]
        assert dialect == "json"

    def test_parses_python_repr_dict_single_quotes(self):
        parsed, dialect = sh.parse_stringified_container("{'a': 1, 'b': 'two'}")
        assert parsed == {"a": 1, "b": "two"}
        assert dialect == "python"

    def test_parses_python_repr_with_booleans_and_none(self):
        parsed, dialect = sh.parse_stringified_container(
            "{'enabled': True, 'note': None, 'count': 5}"
        )
        assert parsed == {"enabled": True, "note": None, "count": 5}
        assert dialect == "python"

    def test_parses_python_tuple(self):
        parsed, dialect = sh.parse_stringified_container("('a', 'b')")
        assert parsed == ("a", "b")
        assert dialect == "python"

    def test_strips_surrounding_whitespace(self):
        parsed, dialect = sh.parse_stringified_container('  {"a": 1}  \n')
        assert parsed == {"a": 1}
        assert dialect == "json"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "{",
            "just a normal string",
            "no brackets here at all",
            "12345",
            "true",
            "null",
            "a" * 10,
        ],
    )
    def test_ordinary_strings_return_none(self, text):
        assert sh.parse_stringified_container(text) is None

    def test_bracket_looking_but_unparseable_text_returns_none(self):
        # Starts/ends with matching brackets but is not valid JSON or a
        # valid Python literal in either dialect -- must degrade to "not a
        # blob", never raise.
        assert sh.parse_stringified_container("{not: valid, at: all!}") is None

    def test_mismatched_brackets_return_none(self):
        assert sh.parse_stringified_container('{"a": 1]') is None

    def test_scalar_literal_inside_brackets_is_not_a_container(self):
        # ast.literal_eval("(1)") evaluates to the int 1, not a tuple --
        # confirm the isinstance(parsed, (dict, list, tuple)) gate rejects
        # non-container results from the python-literal fallback too.
        assert sh.parse_stringified_container("(1)") is None

    def test_over_length_cap_returns_none_even_if_valid_json(self):
        huge = "[" + ",".join(["1"] * 40_000) + "]"
        assert len(huge) > sh._MAX_STRINGIFIED_CONTAINER_LENGTH
        assert sh.parse_stringified_container(huge) is None

    def test_empty_container_is_still_parsed(self):
        assert sh.parse_stringified_container("{}") == ({}, "json")
        assert sh.parse_stringified_container("[]") == ([], "json")

    def test_never_raises_on_pathological_input(self):
        # A large run of open brackets is valid-looking by the cheap
        # pre-check (first char '[' matches last char via a much later
        # close) but must not raise even if the underlying parser bails.
        pathological = "[" * 200 + "]" * 200
        # Should not raise; result may or may not be None depending on
        # whether the parser accepts deeply nested empty lists, but this
        # must complete without an exception either way.
        sh.parse_stringified_container(pathological)


class TestSerializeStringifiedContainer:
    def test_json_dialect_round_trips_through_json_loads(self):
        text = sh.serialize_stringified_container({"a": 1, "b": "two"}, "json")
        assert sh.parse_stringified_container(text) == ({"a": 1, "b": "two"}, "json")

    def test_python_dialect_uses_repr(self):
        text = sh.serialize_stringified_container({"a": 1, "b": "two"}, "python")
        assert text == repr({"a": 1, "b": "two"})


# ---------------------------------------------------------------------------
# shared.redact_sensitive — blob-nested redaction
# ---------------------------------------------------------------------------
class TestRedactSensitiveNestedBlob:
    def test_sensitive_field_inside_json_blob_is_redacted(self):
        payload = {
            "device_function": '{"scope": "ap-group", "radius_secret": "hunter2"}',
        }
        out = sh.redact_sensitive(payload)
        assert out["device_function"] != payload["device_function"]
        reparsed, _ = sh.parse_stringified_container(out["device_function"])
        assert reparsed["radius_secret"] == sh._REDACTED
        assert reparsed["scope"] == "ap-group"

    def test_sensitive_field_inside_python_repr_blob_is_redacted(self):
        payload = {
            "annotation": "{'scope': 'ap-group', 'api_key': 'sekrit-value'}",
        }
        out = sh.redact_sensitive(payload)
        reparsed, dialect = sh.parse_stringified_container(out["annotation"])
        assert dialect == "python"
        assert reparsed["api_key"] == sh._REDACTED
        assert reparsed["scope"] == "ap-group"

    def test_benign_blob_passed_through_byte_for_byte(self):
        blob = "{'scope': 'ap-group', 'note': 'nothing sensitive here'}"
        payload = {"annotation": blob}
        out = sh.redact_sensitive(payload)
        # Nothing inside changed -- must be the *exact original string*, not
        # a re-serialized (possibly reformatted) equivalent.
        assert out["annotation"] is blob or out["annotation"] == blob

    def test_ordinary_string_field_untouched(self):
        payload = {"description": "This is a perfectly normal free-text field."}
        out = sh.redact_sensitive(payload)
        assert out["description"] == payload["description"]

    def test_nested_list_of_dicts_inside_blob(self):
        blob = '{"members": [{"password": "p1"}, {"password": "p2"}]}'
        out = sh.redact_sensitive({"config_blob": blob})
        reparsed, _ = sh.parse_stringified_container(out["config_blob"])
        assert all(m["password"] == sh._REDACTED for m in reparsed["members"])

    def test_doubly_nested_blob_is_recursively_redacted(self):
        inner = "{'psk': 'inner-secret'}"
        # A dict literal whose value is itself the stringified inner blob --
        # exercised via ast.literal_eval since it mixes quote styles.
        outer_py = "{'outer_field': " + repr(inner) + "}"
        out = sh.redact_sensitive({"blob": outer_py})
        assert "inner-secret" not in out["blob"]

    def test_directly_sensitive_key_still_wins_over_blob_parsing(self):
        # A key that is itself recognized as sensitive is redacted as a
        # whole value (existing behavior), even though its value also
        # happens to look like a parseable container.
        out = sh.redact_sensitive({"password": '{"a": 1}'})
        assert out["password"] == sh._REDACTED


# ---------------------------------------------------------------------------
# SecretTokenizeMiddleware — blob-nested tokenization + round trip
# ---------------------------------------------------------------------------
class TestSecretTokenizerNestedBlob:
    def test_secret_nested_in_json_blob_is_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        result = mw.after_call(
            "get_network_profile",
            {},
            {"device_function": '{"scope": "ap-group", "radius_secret": "hunter2"}'},
        )

        assert result is not None
        reparsed, _ = sh.parse_stringified_container(result["device_function"])
        assert _SECRET_TOKEN_RE.fullmatch(reparsed["radius_secret"])
        assert reparsed["scope"] == "ap-group"

    def test_secret_nested_in_python_blob_round_trips(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        blob = "{'scope': 'ap-group', 'radius_secret': 'hunter2'}"
        tokenized = mw.after_call("get_network_profile", {}, {"device_function": blob})
        assert tokenized is not None

        resolved = mw.before_call(
            "set_network_profile", {"device_function": tokenized["device_function"]}
        )
        assert resolved == {"device_function": blob}

    def test_benign_blob_yields_no_change(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        payload = {"annotation": "{'scope': 'ap-group', 'note': 'fine'}"}
        result = mw.after_call("get_network_profile", {}, payload)

        assert result is None

    def test_ordinary_long_description_never_parsed_or_mutated(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        payload = {"description": "A perfectly ordinary free-text description field."}
        result = mw.after_call("get_thing", {}, payload)

        assert result is None


# ---------------------------------------------------------------------------
# PIITokenizeMiddleware — blob-nested tokenization + round trip
# ---------------------------------------------------------------------------
class TestPIITokenizerNestedBlob:
    def test_pii_nested_in_json_blob_is_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.after_call(
            "list_visitors",
            {},
            {"metadata": '{"site": "hq", "email": "guest@example.com"}'},
        )

        assert result is not None
        reparsed, _ = sh.parse_stringified_container(result["metadata"])
        assert _PII_TOKEN_RE.fullmatch(reparsed["email"])
        assert reparsed["site"] == "hq"

    def test_pii_nested_in_python_blob_round_trips(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        blob = "{'site': 'hq', 'email': 'guest@example.com'}"
        tokenized = mw.after_call("list_visitors", {}, {"metadata": blob})
        assert tokenized is not None

        resolved = mw.before_call("update_visitor", {"metadata": tokenized["metadata"]})
        assert resolved == {"metadata": blob}

    def test_benign_blob_yields_no_change(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        payload = {"metadata": "{'site': 'hq', 'note': 'no pii here'}"}
        result = mw.after_call("list_visitors", {}, payload)

        assert result is None


# ---------------------------------------------------------------------------
# Chained: both middlewares tokenizing the same blob independently
# ---------------------------------------------------------------------------
class TestBothTokenizersOnSameBlob:
    def test_secret_and_pii_tokenize_independently_in_same_blob(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        secret_mw = SecretTokenizeMiddleware()
        pii_mw = PIITokenizeMiddleware()

        blob = "{'radius_secret': 'hunter2', 'contact_email': 'noc@example.com'}"
        result = {"device_function": blob}

        after_secret = secret_mw.after_call("get_network_profile", {}, result)
        assert after_secret is not None
        after_pii = pii_mw.after_call(
            "get_network_profile", {}, after_secret
        )
        final = after_pii if after_pii is not None else after_secret

        reparsed, _ = sh.parse_stringified_container(final["device_function"])
        assert _SECRET_TOKEN_RE.fullmatch(reparsed["radius_secret"])
        assert _PII_TOKEN_RE.fullmatch(reparsed["contact_email"])

        # Round trip both back to plaintext on a later write call.
        write_args = {"device_function": final["device_function"]}
        resolved_secret = secret_mw.before_call("set_network_profile", write_args)
        resolved_pii = pii_mw.before_call(
            "set_network_profile", resolved_secret if resolved_secret is not None else write_args
        )
        final_resolved = resolved_pii if resolved_pii is not None else resolved_secret
        assert final_resolved == {"device_function": blob}
