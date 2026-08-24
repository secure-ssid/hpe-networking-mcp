"""Characterization tests for ``_edgeconnect_generated_headers``.

These assert on *structure* -- length, hash, character class -- never on the
credential-shaped literal itself. A test that asserts on the literal renders as
passing in redacting output channels while testing nothing.
"""

from __future__ import annotations

import hashlib

import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect

_HEADERS = edgeconnect._edgeconnect_generated_headers

# sha256 of the scheme prefix (including its single trailing space) that the
# implementation prepends when the configured auth header is ``Authorization``.
# Pinned as a digest so the expected value is never spelled out in the suite.
_AUTH_SCHEME_PREFIX_SHA256 = "35e5a9632e18bc9adad684a3eafdd297139c82b5693b7fa2a8d0efcaf024cdcf"
_AUTH_SCHEME_PREFIX_LEN = 7

_TOKEN = "t0k3n-value"


def test_accept_header_is_always_present():
    assert _HEADERS(_TOKEN, "X-Auth-Token")["Accept"] == "*/*"
    assert _HEADERS(_TOKEN, "X-Auth-Token", None)["Accept"] == "*/*"
    assert _HEADERS(_TOKEN, "X-Auth-Token", {})["Accept"] == "*/*"


def test_non_authorization_header_carries_the_token_verbatim():
    headers = _HEADERS(_TOKEN, "X-Auth-Token")

    assert headers["X-Auth-Token"] == _TOKEN
    assert "Authorization" not in headers


def test_auth_header_name_is_used_verbatim_as_the_key():
    headers = _HEADERS(_TOKEN, "X-Custom-Auth")

    assert "X-Custom-Auth" in headers
    assert "x-custom-auth" not in headers


def test_authorization_mode_prefixes_the_token_with_a_scheme():
    headers = _HEADERS(_TOKEN, "Authorization")
    value = headers["Authorization"]

    assert value.endswith(_TOKEN)
    assert value != _TOKEN

    prefix = value[: len(value) - len(_TOKEN)]
    assert len(prefix) == _AUTH_SCHEME_PREFIX_LEN
    assert hashlib.sha256(prefix.encode()).hexdigest() == _AUTH_SCHEME_PREFIX_SHA256

    scheme = prefix[:-1]
    assert prefix[-1] == " "
    assert scheme.isascii() and scheme.isalpha()
    assert value.count(" ") == 1


def test_authorization_mode_is_selected_case_insensitively():
    lowered = _HEADERS(_TOKEN, "authorization")["authorization"]
    upper = _HEADERS(_TOKEN, "AUTHORIZATION")["AUTHORIZATION"]

    assert lowered == upper
    assert lowered.endswith(_TOKEN) and lowered != _TOKEN


def test_model_supplied_auth_headers_are_dropped():
    headers = _HEADERS(
        _TOKEN,
        "X-Auth-Token",
        {
            "Authorization": "attacker",
            "Cookie": "attacker",
            "Host": "attacker",
            "X-Auth-Token": "attacker",
            "X-XSRF-Token": "attacker",
        },
    )

    assert set(headers) == {"Accept", "X-Auth-Token"}
    assert headers["X-Auth-Token"] == _TOKEN


def test_shadowing_filter_ignores_case_and_surrounding_whitespace():
    headers = _HEADERS(_TOKEN, "X-Auth-Token", {" AUTHORIZATION ": "attacker"})

    assert " AUTHORIZATION " not in headers
    assert not any(k.strip().lower() == "authorization" for k in headers)


def test_benign_extra_headers_pass_through_unchanged():
    headers = _HEADERS(_TOKEN, "X-Auth-Token", {"X-Trace": "ok", "Accept": "application/json"})

    assert headers["X-Trace"] == "ok"
    assert headers["Accept"] == "application/json"
    assert headers["X-Auth-Token"] == _TOKEN


def test_every_blocked_header_name_is_filtered():
    blocked = edgeconnect._EDGECONNECT_GENERATED_AUTH_HEADERS
    headers = _HEADERS(_TOKEN, "X-Auth-Token", {name: "attacker" for name in blocked})

    assert set(headers) == {"Accept", "X-Auth-Token"}


def test_caller_extras_are_not_mutated():
    extra = {"X-Trace": "ok"}
    _HEADERS(_TOKEN, "X-Auth-Token", extra)

    assert extra == {"X-Trace": "ok"}
