"""HTTP transport startup/hardening tests for ``shared.run_server``.

Installed MCP SDK 2.x's ``MCPServer.Settings`` carries no
``host``/``port``/``transport_security`` fields any more -- those are now
explicit keyword arguments to ``MCPServer.run(...)`` /
``MCPServer.streamable_http_app(...)`` (see
``mcp.server.mcpserver.server.MCPServer`` in the installed SDK). The bulk of
this file exercises ``shared._configure_http_transport`` (now a pure
host/port -> ``TransportSecuritySettings | None`` builder) and
``shared.run_server``'s dispatch with a lightweight ``_DummyMCP`` double.

``TestRealStreamableHttpTransport`` at the bottom goes one level further and
drives an actual ``uvicorn``-served ``MCPServer`` over a real loopback TCP
socket -- health endpoints, a real MCP ``initialize``/``tools/list``/
``tools/call`` round trip, and the bearer-auth-gated variant -- so a
regression in the real ``run()``/``streamable_http_app()`` plumbing (wrong
kwarg name, wrong host threaded through, etc.) fails a test instead of only
surfacing at manual startup. No Central/GLP credentials or network access
required: everything binds to 127.0.0.1 on an OS-assigned ephemeral port.
"""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace

import pytest
from mcp.server.transport_security import TransportSecuritySettings

from hpe_networking_mcp.mcp_servers.shared import (
    BearerAuthASGIMiddleware,
    InvalidRuntimeConfigError,
    UnsafeHttpBindingError,
    _configure_http_transport,
    _http_bearer_token,
    _is_loopback_host,
    _register_health_routes,
    _serve_streamable_http_with_bearer,
    run_server,
)


class _DummyMCP:
    """Minimal double for ``MCPServer`` -- no ``settings.host``/``port``/
    ``transport_security`` (those fields don't exist on the installed SDK's
    ``Settings`` model), just enough for ``run_server`` to dispatch through.

    It does carry a ``_tool_manager`` with a ``call_tool``, because
    ``run_server`` installs the platform write gate on every backend it starts
    -- including ones with no registered platform gate, which are now refused
    rather than left ungated. A double without that seam would be a server the
    gate cannot be installed on, which no real backend is.
    """

    def __init__(self) -> None:
        self.settings = SimpleNamespace(log_level="INFO")
        self.run_calls: list[dict] = []
        self.custom_routes: list[tuple[str, list[str]]] = []
        self._tool_manager = SimpleNamespace(
            call_tool=self._call_tool, _tools={}, list_tools=list
        )

    async def _call_tool(self, name, arguments, context=None, convert_result=False):
        raise AssertionError(f"transport tests never dispatch tools (got {name!r})")

    def run(self, **kwargs):
        self.run_calls.append(kwargs)

    def custom_route(self, path, methods, name=None, include_in_schema=True):
        def decorator(fn):
            self.custom_routes.append((path, methods))
            return fn

        return decorator


# ---------------------------------------------------------------------------
# run_server dispatch -- host/port/transport_security threaded explicitly
# ---------------------------------------------------------------------------


def test_run_server_threads_host_port_and_security_into_run_kwargs(monkeypatch):
    server = _DummyMCP()
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "9000")

    run_server(server)

    assert len(server.run_calls) == 1
    call = server.run_calls[0]
    assert call["transport"] == "streamable-http"
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 9000
    # Loopback + no explicit MCP_ALLOWED_HOSTS/ORIGINS -> None, meaning "let
    # the SDK apply its own loopback-only default" (see
    # _configure_http_transport docstring).
    assert call["transport_security"] is None


def test_run_server_defaults_http_to_8010(monkeypatch):
    server = _DummyMCP()
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)

    run_server(server)

    call = server.run_calls[0]
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8010


def test_run_server_registers_health_routes_on_http(monkeypatch):
    server = _DummyMCP()
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")

    run_server(server)

    paths = {path for path, _methods in server.custom_routes}
    assert {"/livez", "/readyz", "/healthz"} <= paths


def test_register_health_routes_is_idempotent():
    server = _DummyMCP()
    _register_health_routes(server)
    _register_health_routes(server)

    assert len(server.custom_routes) == 3


def test_run_server_stdio_keeps_default_run(monkeypatch):
    server = _DummyMCP()
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    run_server(server)

    assert server.run_calls == [{}]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_run_server_rejects_contradictory_access_profile(monkeypatch, transport):
    server = _DummyMCP()
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
    monkeypatch.setenv("HPE_MCP_READONLY", "1")

    with pytest.raises(InvalidRuntimeConfigError, match="conflicts"):
        run_server(server)

    assert server.run_calls == []


# ---------------------------------------------------------------------------
# _configure_http_transport -- pure host/port -> TransportSecuritySettings
# ---------------------------------------------------------------------------


def test_configure_http_transport_loopback_default_defers_to_sdk(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("MCP_DNS_REBINDING_PROTECTION", raising=False)

    # None here is deliberate: the installed SDK's streamable_http_app/
    # sse_app auto-build a loopback-safe TransportSecuritySettings when
    # transport_security=None and host is exactly 127.0.0.1/localhost/::1.
    assert _configure_http_transport("127.0.0.1", 8010) is None


def test_configure_http_transport_applies_custom_security_allowlists(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com,localhost:*")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://app.example.com,http://localhost:*")
    monkeypatch.setenv("MCP_DNS_REBINDING_PROTECTION", "true")

    security = _configure_http_transport("127.0.0.1", 8010)

    assert isinstance(security, TransportSecuritySettings)
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com", "localhost:*"]
    assert security.allowed_origins == ["https://app.example.com", "http://localhost:*"]


# ---------------------------------------------------------------------------
# Host/origin allow-list hardening for non-loopback MCP_HOST
# ---------------------------------------------------------------------------


class TestLoopbackDetection:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.5.5.5"])
    def test_loopback_hosts(self, host):
        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "mcp.example.com", "::"])
    def test_non_loopback_hosts(self, host):
        assert _is_loopback_host(host) is False


class TestUnsafeBindingRefusal:
    def test_public_bind_without_allowlist_raises(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        with pytest.raises(UnsafeHttpBindingError, match="MCP_ALLOWED_HOSTS"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_partial_allowlist_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        with pytest.raises(UnsafeHttpBindingError):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_wildcard_allowlist_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")

        with pytest.raises(UnsafeHttpBindingError, match="wildcard"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_wildcard_rejected_even_with_legacy_opt_in(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")
        monkeypatch.setenv("HPE_MCP_ALLOW_WILDCARD_HTTP_ALLOWLIST", "1")

        with pytest.raises(UnsafeHttpBindingError, match="wildcard"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_explicit_allowlist_succeeds(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

        security = _configure_http_transport("0.0.0.0", 8010)

        assert security.allowed_hosts == ["mcp.example.com:*"]
        assert security.allowed_origins == ["https://mcp.example.com"]

    def test_public_bind_with_dns_rebinding_disabled_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.setenv("MCP_DNS_REBINDING_PROTECTION", "0")

        with pytest.raises(UnsafeHttpBindingError, match="DNS-rebinding"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_dns_rebinding_disabled_allowed_with_opt_in(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.setenv("MCP_DNS_REBINDING_PROTECTION", "0")
        monkeypatch.setenv("HPE_MCP_ALLOW_INSECURE_HTTP_BINDING", "1")

        _configure_http_transport("0.0.0.0", 8010)  # should not raise

    def test_loopback_bind_never_requires_allowlist(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        _configure_http_transport("127.0.0.1", 8010)  # should not raise

    def test_run_server_propagates_unsafe_binding_error(self, monkeypatch):
        server = _DummyMCP()
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        with pytest.raises(UnsafeHttpBindingError):
            run_server(server)

        assert server.run_calls == []  # never reached mcp_instance.run()


# ---------------------------------------------------------------------------
# Optional bearer-token protection
# ---------------------------------------------------------------------------


class TestBearerToken:
    def test_bearer_token_unset_by_default(self, monkeypatch):
        monkeypatch.delenv("MCP_HTTP_BEARER_TOKEN", raising=False)
        assert _http_bearer_token() is None

    def test_bearer_token_blank_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "   ")
        assert _http_bearer_token() is None

    def test_bearer_token_read_from_env(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "s3cr3t")
        assert _http_bearer_token() == "s3cr3t"

    def test_run_server_without_bearer_token_uses_default_run(self, monkeypatch):
        server = _DummyMCP()
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.delenv("MCP_HTTP_BEARER_TOKEN", raising=False)

        run_server(server)

        assert len(server.run_calls) == 1
        assert server.run_calls[0]["transport"] == "streamable-http"

    def test_run_server_with_bearer_token_on_sse_fails_closed(self, monkeypatch):
        server = _DummyMCP()
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "s3cr3t")

        with pytest.raises(UnsafeHttpBindingError, match="cannot enforce"):
            run_server(server)

        assert server.run_calls == []


class _RecordingASGIApp:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        response_start = {"type": "http.response.start", "status": 200, "headers": []}
        await send(response_start)
        await send({"type": "http.response.body", "body": b"{}"})


async def _drive_asgi(app, path: str, headers: list[tuple[bytes, bytes]]):
    scope = {"type": "http", "path": path, "headers": headers}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


class TestBearerAuthASGIMiddleware:
    def test_health_paths_are_exempt(self):
        inner = _RecordingASGIApp()
        mw = BearerAuthASGIMiddleware(inner, token="s3cr3t")

        sent = asyncio.run(_drive_asgi(mw, "/livez", []))

        assert inner.calls, "inner app should have been called for an exempt path"
        assert sent[0]["status"] == 200

    def test_missing_authorization_is_rejected(self):
        inner = _RecordingASGIApp()
        mw = BearerAuthASGIMiddleware(inner, token="s3cr3t")

        sent = asyncio.run(_drive_asgi(mw, "/mcp", []))

        assert not inner.calls, "inner app must not run without a valid token"
        assert sent[0]["status"] == 401

    def test_wrong_token_is_rejected(self):
        inner = _RecordingASGIApp()
        mw = BearerAuthASGIMiddleware(inner, token="s3cr3t")

        sent = asyncio.run(
            _drive_asgi(mw, "/mcp", [(b"authorization", b"Bearer wrong-token")])
        )

        assert not inner.calls
        assert sent[0]["status"] == 401

    def test_correct_token_is_accepted(self):
        inner = _RecordingASGIApp()
        mw = BearerAuthASGIMiddleware(inner, token="s3cr3t")

        sent = asyncio.run(
            _drive_asgi(mw, "/mcp", [(b"authorization", b"Bearer s3cr3t")])
        )

        assert inner.calls
        assert sent[0]["status"] == 200


# ---------------------------------------------------------------------------
# Real streamable-HTTP transport -- actual loopback socket, actual MCP client
# ---------------------------------------------------------------------------
#
# Everything above proves the dispatch logic in isolation with a double.
# These tests instead build a real ``MCPServer``, run it with ``uvicorn`` on
# an OS-assigned loopback port, and drive it with the SDK's own
# ``streamable_http_client`` -- a real ``initialize``/``tools/list``/
# ``tools/call`` JSON-RPC round trip over a real socket, plus the health
# endpoints and (in the second test) the bearer-auth ASGI wrapper. No
# Central/GLP credentials or external network access needed.


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_error: OSError | None = None
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"nothing listening on {host}:{port}") from last_error


def _build_smoke_server(name: str):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)

    @server.tool()
    def ping() -> str:
        """Ping tool used only by the HTTP transport smoke tests."""
        return "pong"

    return server


@pytest.fixture(autouse=True)
def _reset_sse_global_shutdown_latch(monkeypatch):
    """Neutralize sse_starlette's process-global shutdown latch.

    ``sse_starlette`` arms a per-loop watcher that polls every 0.5s and, the
    moment ANY uvicorn server's ``should_exit`` is observed, latches the
    class-global ``AppStatus.should_exit = True`` -- and never resets it.
    Every ``EventSourceResponse`` created afterwards then tears itself down
    right after its response headers ("ASGI callable returned without
    completing response"), which made any in-process server restart
    deterministically kill its follow-up SSE responses.

    These tests manage server lifecycles explicitly via ``should_exit`` and
    task cancellation, never process signals -- exactly the condition
    AppStatus' own docstring demands before disabling automatic drain --
    so disable the drain for each test and clear any latch an earlier test
    left behind. ``monkeypatch`` restores both class attributes afterward.
    """
    import sse_starlette.sse as sse_module

    monkeypatch.setattr(sse_module.AppStatus, "should_exit", False)
    monkeypatch.setattr(
        sse_module.AppStatus, "enable_automatic_graceful_drain", False
    )


class TestRealStreamableHttpTransport:
    def test_health_and_real_mcp_round_trip_over_streamable_http(self):
        import httpx
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        host = "127.0.0.1"
        port = _free_loopback_port()
        server = _build_smoke_server("http-transport-smoke")

        _register_health_routes(server)
        transport_security = _configure_http_transport(host, port)
        app = server.streamable_http_app(host=host, transport_security=transport_security)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        uv_server = uvicorn.Server(config)

        async def _run():
            serve_task = asyncio.create_task(uv_server.serve())
            try:
                await _wait_for_port(host, port)

                async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as http_client:
                    livez = await http_client.get("/livez")
                    assert livez.status_code == 200
                    assert livez.json() == {"status": "ok"}

                    readyz = await http_client.get("/readyz")
                    assert readyz.status_code in (200, 503)
                    assert "detail" in readyz.json()

                async with streamable_http_client(f"http://{host}:{port}/mcp") as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        assert init.server_info.name == "http-transport-smoke"

                        tools = await session.list_tools()
                        assert {t.name for t in tools.tools} == {"ping"}

                        call = await session.call_tool("ping", {})
                        assert call.content[0].text == "pong"
            finally:
                uv_server.should_exit = True
                await serve_task

        asyncio.run(_run())

    def test_bearer_auth_blocks_unauthenticated_and_allows_correct_token(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        host = "127.0.0.1"
        port = _free_loopback_port()
        token = "s3cr3t-test-token"
        server = _build_smoke_server("http-transport-bearer-smoke")

        _register_health_routes(server)
        transport_security = _configure_http_transport(host, port)

        async def _run():
            serve_task = asyncio.create_task(
                _serve_streamable_http_with_bearer(server, token, host, port, transport_security)
            )
            try:
                await _wait_for_port(host, port)

                # Health stays exempt from the bearer check.
                async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as http_client:
                    livez = await http_client.get("/livez")
                    assert livez.status_code == 200

                    unauthenticated = await http_client.post("/mcp", json={})
                    assert unauthenticated.status_code == 401

                    wrong_token = await http_client.post(
                        "/mcp",
                        json={},
                        headers={"Authorization": "Bearer wrong-token"},
                    )
                    assert wrong_token.status_code == 401

                # Correct bearer token -> real initialize/tools/list/call.
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}
                ) as http_client:
                    async with streamable_http_client(
                        f"http://{host}:{port}/mcp", http_client=http_client
                    ) as (read, write):
                        async with ClientSession(read, write) as session:
                            init = await session.initialize()
                            assert init.server_info.name == "http-transport-bearer-smoke"

                            tools = await session.list_tools()
                            assert {t.name for t in tools.tools} == {"ping"}

                            call = await session.call_tool("ping", {})
                            assert call.content[0].text == "pong"
            finally:
                serve_task.cancel()
                try:
                    await serve_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())

    def test_restart_rejects_stale_session_and_allows_fresh_initialize(self):
        import httpx
        import uvicorn
        from mcp.server.mcpserver import MCPServer

        host = "127.0.0.1"
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "session-lifecycle-smoke", "version": "1"},
            },
        }
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        async def _run(port: int):
            servers = []
            tasks = []

            async def start_server(name: str):
                server = MCPServer(name)

                @server.tool()
                def ping() -> str:
                    return "pong"

                transport_security = _configure_http_transport(host, port)
                app = server.streamable_http_app(
                    host=host,
                    transport_security=transport_security,
                )
                config = uvicorn.Config(app, host=host, port=port, log_level="warning")
                uv_server = uvicorn.Server(config)
                task = asyncio.create_task(uv_server.serve())
                await _wait_for_port(host, port)
                servers.append(uv_server)
                tasks.append(task)

            try:
                await start_server("session-lifecycle-before")
                async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                    initialized = await client.post(
                        "/mcp",
                        headers=request_headers,
                        json=init_payload,
                    )
                    assert initialized.status_code == 200
                    session_id = initialized.headers["mcp-session-id"]

                    established_headers = {
                        **request_headers,
                        "Mcp-Session-Id": session_id,
                        "Mcp-Protocol-Version": "2025-06-18",
                    }
                    listed = await client.post(
                        "/mcp",
                        headers=established_headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        },
                    )
                    assert listed.status_code == 200

                servers[0].should_exit = True
                await tasks[0]

                await start_server("session-lifecycle-after")
                async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                    stale = await client.post(
                        "/mcp",
                        headers=established_headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/list",
                            "params": {},
                        },
                    )
                    assert stale.status_code == 404
                    assert "Session not found" in stale.text

                    fresh = await client.post(
                        "/mcp",
                        headers=request_headers,
                        json={**init_payload, "id": 4},
                    )
                    assert fresh.status_code == 200
                    assert fresh.headers["mcp-session-id"] != session_id
            finally:
                for server in servers[1:]:
                    server.should_exit = True
                for task in tasks[1:]:
                    await task

        # Transient-flake tolerance: under full-suite load uvicorn has been
        # observed tearing the restart handoff down mid-response ("ASGI
        # callable returned without completing response"), surfacing
        # client-side as httpx.RemoteProtocolError "incomplete chunked
        # read". Each attempt is fully self-contained (fresh port, fresh
        # servers), so retrying the whole scenario absorbs the flake while
        # every assertion inside stays strict.
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                asyncio.run(_run(_free_loopback_port()))
            except httpx.TransportError as exc:
                last_error = exc
                time.sleep(0.25)
            else:
                break
        else:
            raise AssertionError(
                "transport scenario hit a transport error on all 3 attempts"
            ) from last_error
