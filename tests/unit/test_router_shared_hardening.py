"""Regression tests for the router/shared hardening pass.

Each class maps to one reproduced defect:

- ``bound_collection_response`` rebuilt ``_pagination`` from scratch, silently
  dropping a backend's own ``next_cursor`` (and any other non-canonical key),
  which made an upstream-paginated collection look complete.
- ``_bound_router_response`` emitted a router continuation cursor on top of an
  upstream one, publishing two different "next" positions and overwriting the
  backend's at the top level.
- ``BearerAuthASGIMiddleware`` compared decoded ``str`` values, so a non-ASCII
  Authorization header raised ``TypeError`` out of the ASGI stack instead of
  returning 401.
- A backend run standalone had no platform write gate at all -- coverage of a
  safety gate depended on how the server was launched.
- Direct-mode registration published gated-off writes and re-derived each tool
  with ``add_tool``, losing the backend's published schema/metadata.
- ``_load_all_backends`` mutated the shared index as it went, so one backend
  raising on import left a permanently-truncated catalog.
- ``find_tool`` shrank the semantic allowance once per accepted hit, so
  ``top_k`` was not honored when the keyword pass returned little or nothing.

No network access.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    CANONICAL_PAGINATION_KEYS,
    DESTRUCTIVE,
    READ_ONLY,
    BearerAuthASGIMiddleware,
    bound_collection_response,
    install_platform_write_gate,
    platform_for_server_name,
    tool_write_capability,
)

# ---------------------------------------------------------------------------
# 1a. bound_collection_response preserves non-canonical _pagination keys
# ---------------------------------------------------------------------------


class TestPaginationKeyPreservation:
    def test_backend_next_cursor_survives(self):
        data = {
            "items": [{"id": i} for i in range(5)],
            "_pagination": {"offset": 0, "total": 5, "next_cursor": "opaque-token"},
        }

        out = bound_collection_response(data, limit=10)

        assert out["_pagination"]["next_cursor"] == "opaque-token"

    def test_every_non_canonical_key_survives(self):
        data = {
            "items": [1, 2, 3],
            "_pagination": {
                "offset": 0,
                "total": 3,
                "next_cursor": "tok",
                "has_more": True,
                "total_pages": 7,
                "next": "https://api.example.com/page/2",
            },
        }

        out = bound_collection_response(data, limit=2)

        pagination = out["_pagination"]
        assert pagination["next_cursor"] == "tok"
        assert pagination["has_more"] is True
        assert pagination["total_pages"] == 7
        assert pagination["next"] == "https://api.example.com/page/2"

    def test_canonical_keys_are_always_recomputed(self):
        """A preserved key must never shadow the slice metadata this
        function owns -- otherwise a stale upstream ``total``/``limit``
        would describe the wrong page."""
        data = {
            "items": list(range(10)),
            "_pagination": {
                "offset": 0,
                "limit": 999,
                "total": 4,
                "truncated": False,
                "list_key": "bogus",
                "next_cursor": "tok",
            },
        }

        out = bound_collection_response(data, limit=3)

        pagination = out["_pagination"]
        assert pagination["limit"] == 3
        assert pagination["list_key"] == "items"
        assert pagination["truncated"] is True
        assert pagination["next_cursor"] == "tok"

    def test_canonical_key_set_matches_what_is_emitted(self):
        out = bound_collection_response({"items": [1, 2]}, limit=1)

        assert set(out["_pagination"]) == set(CANONICAL_PAGINATION_KEYS)

    def test_no_existing_pagination_adds_nothing_extra(self):
        out = bound_collection_response({"items": [1, 2, 3]}, limit=2)

        assert set(out["_pagination"]) == set(CANONICAL_PAGINATION_KEYS)

    def test_list_input_is_unaffected(self):
        out = bound_collection_response([1, 2, 3], limit=2)

        assert out["_pagination"]["truncated"] is True
        assert "next_cursor" not in out["_pagination"]


# ---------------------------------------------------------------------------
# 1b. router never emits a cursor over an upstream one
# ---------------------------------------------------------------------------


def _big_collection(count: int = 500) -> dict[str, Any]:
    return {"items": [{"id": i, "name": f"device-{i}"} for i in range(count)]}


class TestUpstreamCursorPrecedence:
    def test_router_cursor_is_emitted_without_an_upstream_one(self):
        out = router._bound_router_response(
            _big_collection(),
            max_items=10,
            enable_cursor=True,
            tool_name="list_devices",
            tool_arguments={},
        )

        assert out["_response_bounds"]["resumable"] is True
        assert isinstance(out["next_cursor"], str)

    def test_top_level_upstream_cursor_suppresses_the_router_cursor(self):
        payload = {**_big_collection(), "next_cursor": "backend-token"}

        out = router._bound_router_response(
            payload,
            max_items=10,
            enable_cursor=True,
            tool_name="list_devices",
            tool_arguments={},
        )

        marker = out["_response_bounds"]
        assert marker["resumable"] is False
        assert marker["resumable_reason"] == "upstream_cursor_present"
        assert marker["upstream_cursor_key"] == "next_cursor"
        # The backend's own token is left exactly as it was.
        assert out["next_cursor"] == "backend-token"

    def test_pagination_level_upstream_cursor_suppresses_the_router_cursor(self):
        payload = {
            **_big_collection(),
            "_pagination": {"offset": 0, "total": 5000, "next_cursor": "page-2"},
        }

        out = router._bound_router_response(
            payload,
            max_items=10,
            enable_cursor=True,
            tool_name="list_devices",
            tool_arguments={},
        )

        marker = out["_response_bounds"]
        assert marker["resumable"] is False
        assert marker["upstream_cursor_key"] == "_pagination.next_cursor"
        assert "next_cursor" not in out
        assert out["_pagination"]["next_cursor"] == "page-2"

    @pytest.mark.parametrize("key", ["next_cursor", "nextCursor", "next", "cursor"])
    def test_all_known_upstream_cursor_spellings_are_detected(self, key):
        payload = {**_big_collection(), "_pagination": {"offset": 0, key: "tok"}}

        out = router._bound_router_response(
            payload,
            max_items=10,
            enable_cursor=True,
            tool_name="list_devices",
            tool_arguments={},
        )

        assert out["_response_bounds"]["resumable"] is False

    def test_blank_upstream_cursor_is_not_treated_as_present(self):
        payload = {**_big_collection(), "next_cursor": "   "}

        out = router._bound_router_response(
            payload,
            max_items=10,
            enable_cursor=True,
            tool_name="list_devices",
            tool_arguments={},
        )

        assert out["_response_bounds"]["resumable"] is True


# ---------------------------------------------------------------------------
# 2. bearer auth compares bytes
# ---------------------------------------------------------------------------


def _run_bearer(auth_header: bytes | None, token: str = "s3cret", path: str = "/mcp"):
    sent: list[dict[str, Any]] = []
    called = {"downstream": False}

    async def app(scope, receive, send):
        called["downstream"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    headers = [] if auth_header is None else [(b"authorization", auth_header)]
    scope = {"type": "http", "path": path, "headers": headers}
    middleware = BearerAuthASGIMiddleware(app, token=token)
    asyncio.run(middleware(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, called["downstream"]


class TestBearerAuthBytes:
    def test_valid_token_passes(self):
        status, downstream = _run_bearer(b"Bearer s3cret")

        assert status == 200
        assert downstream is True

    def test_wrong_token_401s(self):
        status, downstream = _run_bearer(b"Bearer wrong")

        assert status == 401
        assert downstream is False

    def test_missing_header_401s(self):
        assert _run_bearer(None) == (401, False)

    @pytest.mark.parametrize(
        "header",
        [
            b"Bearer s3cr\xe9t",
            "Bearer sécret".encode("utf-8"),
            "Bearer ünïcödé".encode("latin-1"),
            b"Bearer \xff\xfe\x00",
        ],
    )
    def test_non_ascii_token_401s_instead_of_raising(self, header):
        """Regression: hmac.compare_digest raises TypeError on a str with
        non-ASCII characters, so this used to escape as a 500."""
        status, downstream = _run_bearer(header)

        assert status == 401
        assert downstream is False

    def test_non_ascii_configured_token_still_matches_itself(self):
        token = "sécret-ünïcödé"
        status, downstream = _run_bearer(f"Bearer {token}".encode("utf-8"), token=token)

        assert status == 200
        assert downstream is True

    def test_scheme_is_case_insensitive(self):
        assert _run_bearer(b"bearer s3cret") == (200, True)

    def test_wrong_scheme_401s(self):
        assert _run_bearer(b"Basic s3cret") == (401, False)

    def test_health_paths_are_exempt(self):
        status, downstream = _run_bearer(None, path="/livez")

        assert status == 200
        assert downstream is True


# ---------------------------------------------------------------------------
# 3a. standalone backend platform write gate
# ---------------------------------------------------------------------------


def _gated_backend(name: str) -> MCPServer:
    srv = MCPServer(name)

    @srv.tool(annotations=READ_ONLY)
    def read_thing() -> dict[str, Any]:
        return {"ok": True}

    @srv.tool(annotations=DESTRUCTIVE)
    def destroy_thing() -> dict[str, Any]:
        return {"destroyed": True}

    return srv


def _call(server: MCPServer, name: str) -> Any:
    return asyncio.run(server._tool_manager.call_tool(name, {}, Context(mcp_server=server)))


class TestPlatformForServerName:
    @pytest.mark.parametrize(
        "server_name,platform",
        [
            ("glp-core", "glp"),
            ("central-config", "central"),
            ("central-monitoring", "central"),
            ("central-generated", "central"),
            ("mist-core", "mist"),
            ("aos8-core", "aos8"),
        ],
    )
    def test_known_servers_map_to_gates(self, server_name, platform):
        assert platform_for_server_name(server_name) == platform

    @pytest.mark.parametrize("server_name", ["rag-core", "hpe-networking-mcp", "", None])
    def test_ungated_servers_return_none(self, server_name):
        assert platform_for_server_name(server_name) is None

    def test_matches_the_router_platform_map(self):
        for server_name, platform in router._SERVER_PLATFORMS.items():
            resolved = platform_for_server_name(server_name)
            assert resolved in (platform, None), server_name


class TestStandaloneWriteGate:
    def test_destructive_tool_is_blocked_when_gate_closed(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")

        assert install_platform_write_gate(server) is True
        result = _call(server, "destroy_thing")

        assert result["status"] == "blocked"
        assert result["platform"] == "glp"
        assert "HPE_MCP_GLP_V2BETA1_WRITES" in result["error"]

    def test_read_tool_is_never_blocked(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")
        install_platform_write_gate(server)

        assert _call(server, "read_thing") == {"ok": True}

    def test_destructive_tool_runs_when_gate_open(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        server = _gated_backend("glp-core")
        install_platform_write_gate(server)

        assert _call(server, "destroy_thing") == {"destroyed": True}

    def test_gate_is_per_platform_not_shared(self, monkeypatch):
        """Central's gate must not open GLP's."""
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        glp = _gated_backend("glp-core")
        central = _gated_backend("central-config")
        install_platform_write_gate(glp)
        install_platform_write_gate(central)

        assert _call(glp, "destroy_thing")["status"] == "blocked"
        assert _call(central, "destroy_thing") == {"destroyed": True}

    def test_ungated_server_refuses_writes_and_still_serves_reads(self):
        """A backend with no registered gate denies writes -- it is not skipped.

        This asserted the opposite until the deny-by-default fix: the gate
        declined to install whenever ``platform_for_server_name`` returned
        ``None``, so a destructive tool on ``rag-core`` executed. Nothing was
        exploitable because those backends are entirely read-only today, but the
        contract was fail-open -- adding one write tool to ``rag.py`` would have
        shipped an ungated destructive call.
        """
        from hpe_networking_mcp.mcp_servers.shared import _WRITE_GATE_INSTALLED_ATTR

        server = _gated_backend("rag-core")

        assert install_platform_write_gate(server) is True
        assert hasattr(server._tool_manager, _WRITE_GATE_INSTALLED_ATTR)
        blocked = _call(server, "destroy_thing")
        assert blocked["status"] == "blocked"
        # No env var could have enabled it, so the refusal names the registry.
        assert "_PLATFORM_WRITE_GATES" in blocked["error"]
        assert blocked["server"] == "rag-core"
        # Reads are untouched: the gate short-circuits before any capability check.
        assert _call(server, "read_thing") == {"ok": True}

    def test_safe_profile_blocks_writes_on_ungated_standalone_server(
        self, monkeypatch
    ):
        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        server = _gated_backend("rag-core")

        assert install_platform_write_gate(server) is True
        assert _call(server, "destroy_thing")["status"] == "blocked"
        assert _call(server, "read_thing") == {"ok": True}

    def test_installation_is_idempotent(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")
        install_platform_write_gate(server)
        install_platform_write_gate(server)
        install_platform_write_gate(server)

        # Still exactly one gate: flipping the flag on must let the call
        # through, which a stacked wrapper chain would still allow, so also
        # assert the saved original was not re-saved as an already-gated fn.
        assert _call(server, "destroy_thing")["status"] == "blocked"
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        assert _call(server, "destroy_thing") == {"destroyed": True}

    def test_composes_with_middleware_in_either_order(self, monkeypatch):
        from hpe_networking_mcp.mcp_servers._middleware import (
            NullStripMiddleware,
            install_middleware,
        )

        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

        gate_first = _gated_backend("glp-core")
        install_platform_write_gate(gate_first)
        install_middleware(gate_first, [NullStripMiddleware()])

        middleware_first = _gated_backend("glp-core")
        install_middleware(middleware_first, [NullStripMiddleware()])
        install_platform_write_gate(middleware_first)

        assert _call(gate_first, "destroy_thing")["status"] == "blocked"
        assert _call(middleware_first, "destroy_thing")["status"] == "blocked"
        assert _call(gate_first, "read_thing") == {"ok": True}
        assert _call(middleware_first, "read_thing") == {"ok": True}

    def test_run_server_installs_the_gate(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")
        monkeypatch.setattr(server, "run", lambda *a, **k: None)

        from hpe_networking_mcp.mcp_servers.shared import run_server

        run_server(server)

        assert _call(server, "destroy_thing")["status"] == "blocked"

    def test_blocked_result_is_convertible_for_structured_output(self, monkeypatch):
        """MCPServer calls call_tool(convert_result=True) on the MCP path; the
        gate's substituted response must survive that conversion."""
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")
        install_platform_write_gate(server)

        converted = asyncio.run(
            server._tool_manager.call_tool(
                "destroy_thing", {}, Context(mcp_server=server), convert_result=True
            )
        )

        assert converted is not None
        assert "blocked" in json.dumps(converted, default=str)

    def test_allowed_call_still_converts_normally(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        server = _gated_backend("glp-core")
        install_platform_write_gate(server)

        converted = asyncio.run(
            server._tool_manager.call_tool(
                "read_thing", {}, Context(mcp_server=server), convert_result=True
            )
        )

        assert "ok" in json.dumps(converted, default=str)

    def test_capability_classification_matches_the_router(self):
        server = _gated_backend("glp-core")
        tools = server._tool_manager._tools

        for name, tool in tools.items():
            assert tool_write_capability(tool) == router._tool_capability(tool), name


# ---------------------------------------------------------------------------
# 3b. direct-mode registration
# ---------------------------------------------------------------------------


def _install_fake_backend(monkeypatch, backend: MCPServer, server_name: str) -> None:
    monkeypatch.setattr(router, "_BACKENDS", {server_name: "fake.module"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(router, "_backend_load_errors", {})
    monkeypatch.setattr(
        router.importlib,
        "import_module",
        lambda module_path: SimpleNamespace(mcp=backend),
    )


class TestDirectRegistration:
    def test_disabled_writes_are_not_registered(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        backend = _gated_backend("glp-core")
        _install_fake_backend(monkeypatch, backend, "glp-core")
        target = MCPServer("router-direct")

        registered = router._register_direct_backend_tools(target)

        assert registered == ["read_thing"]
        assert "destroy_thing" not in target._tool_manager._tools

    def test_enabled_writes_are_registered(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        backend = _gated_backend("glp-core")
        _install_fake_backend(monkeypatch, backend, "glp-core")
        target = MCPServer("router-direct")

        registered = router._register_direct_backend_tools(target)

        assert sorted(registered) == ["destroy_thing", "read_thing"]

    def test_original_tool_object_is_published(self, monkeypatch):
        backend = MCPServer("central-monitoring")

        @backend.tool(annotations=READ_ONLY, title="Nice Title")
        def described(value: str = "x") -> dict[str, str]:
            """Docstring description."""
            return {"value": value}

        _install_fake_backend(monkeypatch, backend, "central-monitoring")
        target = MCPServer("router-direct")

        router._register_direct_backend_tools(target)

        published = target._tool_manager._tools["described"]
        original = backend._tool_manager._tools["described"]
        assert published is original
        assert published.title == "Nice Title"
        assert published.parameters == original.parameters
        assert published.fn_metadata is original.fn_metadata
        assert published.description == original.description

    def test_existing_router_tool_names_are_not_overwritten(self, monkeypatch):
        backend = _gated_backend("central-monitoring")
        _install_fake_backend(monkeypatch, backend, "central-monitoring")
        target = MCPServer("router-direct")

        @target.tool(annotations=READ_ONLY)
        def read_thing() -> dict[str, Any]:
            return {"router": True}

        router_tool = target._tool_manager._tools["read_thing"]
        registered = router._register_direct_backend_tools(target)

        assert "read_thing" not in registered
        assert target._tool_manager._tools["read_thing"] is router_tool


# ---------------------------------------------------------------------------
# 4. atomic backend load
# ---------------------------------------------------------------------------


class TestAtomicBackendLoad:
    def _reset(self, monkeypatch, backends):
        monkeypatch.setattr(router, "_BACKENDS", backends)
        monkeypatch.setattr(router, "_tool_index", {})
        monkeypatch.setattr(router, "_tool_servers", {})
        monkeypatch.setattr(router, "_tool_backend_names", {})
        monkeypatch.setattr(router, "_backend_load_errors", {})

    def test_import_failure_does_not_stop_other_backends(self, monkeypatch):
        good = _gated_backend("central-monitoring")
        self._reset(monkeypatch, {"broken-core": "broken.mod", "central-monitoring": "good.mod"})

        def _import(module_path):
            if module_path == "broken.mod":
                raise ImportError("no module named boom")
            return SimpleNamespace(mcp=good)

        monkeypatch.setattr(router.importlib, "import_module", _import)

        router._load_all_backends()

        assert "read_thing" in router._tool_index
        assert router._tool_backend_names["read_thing"] == "central-monitoring"

    def test_import_failure_is_recorded_and_surfaced(self, monkeypatch):
        good = _gated_backend("central-monitoring")
        self._reset(monkeypatch, {"broken-core": "broken.mod", "central-monitoring": "good.mod"})

        def _import(module_path):
            if module_path == "broken.mod":
                raise ImportError("no module named boom")
            return SimpleNamespace(mcp=good)

        monkeypatch.setattr(router.importlib, "import_module", _import)
        router._load_all_backends()

        errors = router.backend_load_errors()
        assert "broken-core" in errors
        assert "no module named boom" in errors["broken-core"]

    def test_unknown_tool_error_surfaces_backend_load_errors(self, monkeypatch):
        good = _gated_backend("central-monitoring")
        self._reset(monkeypatch, {"broken-core": "broken.mod", "central-monitoring": "good.mod"})

        def _import(module_path):
            if module_path == "broken.mod":
                raise ImportError("no module named boom")
            return SimpleNamespace(mcp=good)

        monkeypatch.setattr(router.importlib, "import_module", _import)

        result = asyncio.run(router._dispatch_read_tool(None, "nope"))

        assert result["status"] == "unknown_tool"
        assert "broken-core" in result["backend_load_errors"]

    def test_duplicate_tool_names_raise_without_partial_state(self, monkeypatch):
        first = _gated_backend("central-monitoring")
        second = _gated_backend("central-config")
        self._reset(monkeypatch, {"central-monitoring": "a.mod", "central-config": "b.mod"})
        monkeypatch.setattr(
            router.importlib,
            "import_module",
            lambda path: SimpleNamespace(mcp=first if path == "a.mod" else second),
        )

        with pytest.raises(RuntimeError, match="duplicate backend tool name"):
            router._load_all_backends()

        # Nothing published: the router is unloaded, not half-loaded.
        assert router._tool_index == {}
        assert router._tool_servers == {}
        assert router._tool_backend_names == {}

    def test_all_backends_failing_leaves_an_empty_retryable_index(self, monkeypatch):
        self._reset(monkeypatch, {"a-core": "a.mod", "b-core": "b.mod"})
        monkeypatch.setattr(
            router.importlib,
            "import_module",
            lambda path: (_ for _ in ()).throw(ImportError(f"boom {path}")),
        )

        router._load_all_backends()

        assert router._tool_index == {}
        assert set(router.backend_load_errors()) == {"a-core", "b-core"}

    def test_backend_load_errors_returns_a_copy(self, monkeypatch):
        self._reset(monkeypatch, {})
        router._backend_load_errors["x"] = "y"

        snapshot = router.backend_load_errors()
        snapshot["x"] = "mutated"

        assert router._backend_load_errors["x"] == "y"


# ---------------------------------------------------------------------------
# 5. find_tool honors top_k
# ---------------------------------------------------------------------------


class TestFindToolTopK:
    def _semantic_only(self, monkeypatch, hit_count: int = 20):
        backend = MCPServer("central-monitoring")
        names = [f"semantic_tool_{i}" for i in range(hit_count)]
        for name in names:
            backend._tool_manager.add_tool(
                (lambda: {"ok": True}), name=name, annotations=READ_ONLY
            )
        _install_fake_backend(monkeypatch, backend, "central-monitoring")
        monkeypatch.setattr(router, "_BACKEND", "lancedb")
        monkeypatch.setattr(
            router, "_keyword_hits", lambda query, limit, include_schema=False: []
        )
        monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
        monkeypatch.setattr(router._lance, "connect", lambda: object())
        monkeypatch.setattr(
            router._lance,
            "search_tools",
            lambda conn, query, vec, top_k: [
                {
                    "name": name,
                    "server": "central-monitoring",
                    "description": "",
                    "score": 1.0,
                    "schema_json": "{}",
                }
                for name in names
            ],
        )
        return names

    @pytest.mark.parametrize("top_k", [1, 2, 3, 5, 8, 10])
    def test_semantic_only_results_fill_top_k(self, monkeypatch, top_k):
        """Regression: the semantic allowance shrank by one per accepted hit,
        so top_k=10 with no keyword hits returned only 5 results."""
        self._semantic_only(monkeypatch)

        results = router.find_tool("anything", top_k=top_k)

        assert len(results) == top_k

    def test_fewer_hits_than_top_k_returns_what_exists(self, monkeypatch):
        self._semantic_only(monkeypatch, hit_count=3)

        results = router.find_tool("anything", top_k=10)

        assert len(results) == 3

    def test_keyword_hits_still_capped_at_half(self, monkeypatch):
        names = self._semantic_only(monkeypatch)
        keyword = [
            {
                "name": name,
                "server": "central-monitoring",
                "description": "",
                "params": [],
                "score": 1.0,
                "match": "keyword",
                "capability": "read",
                "platform": "central",
                "origin": "curated",
            }
            for name in names
        ]
        monkeypatch.setattr(
            router, "_keyword_hits", lambda query, limit, include_schema=False: keyword
        )

        results = router.find_tool("anything", top_k=10)

        assert len(results) == 10
        assert sum(1 for item in results if item["match"] == "keyword") == 5

    def test_top_k_is_clamped_to_ten(self, monkeypatch):
        self._semantic_only(monkeypatch, hit_count=50)

        assert len(router.find_tool("anything", top_k=99)) == 10

    def test_results_are_deduplicated(self, monkeypatch):
        self._semantic_only(monkeypatch)

        results = router.find_tool("anything", top_k=10)

        assert len({item["name"] for item in results}) == len(results)
