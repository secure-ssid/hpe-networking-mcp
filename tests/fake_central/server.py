"""In-process fake Central API server.

A real HTTP server (stdlib ``http.server``) on an ephemeral localhost port,
serving fixture data with:

- OAuth2 client-credentials token flow (``POST /oauth2/token``).
- Bearer-token enforcement on every other route (401 envelope otherwise).
- Central-style error envelopes (``code``/``message``/``details``/``traceId``).
- ``limit``/``offset`` pagination with a ``Link`` header on list routes.
- Optional simulated 429 rate limiting (off by default; ``Retry-After``).

Every request is appended to a :class:`RequestJournal` — no transports are
monkeypatched, the fake is honest HTTP.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .catalog import EndpointCatalog, Route, is_write
from .fixtures import FixtureBundle
from .journal import RequestJournal, RequestRecord

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

AUTH_SCHEME = "Bearer"


def _json_default(value: Any) -> str:
    """Serialize fixture values PyYAML produces that JSON has no type for.

    ``collections.yaml`` timestamps like ``2026-08-22T09:13:02Z`` are parsed by
    PyYAML into ``datetime`` objects. Central emits them as ISO-8601 with a
    ``Z`` suffix, so render them that way rather than letting ``json.dumps``
    raise inside the handler thread.
    """
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class FakeApiError(Exception):  # noqa: N818 — deliberate: tiny internal surface
    """Raised by fixture handlers for a non-200 response."""

    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeCentral/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> FakeCentralServer:
        return self.server.fake  # type: ignore[attr-defined]

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FakeApiError(400, "BAD_REQUEST", "request body is not valid JSON") from exc

    def _respond(
        self, status: int, payload: Any, extra_headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(payload, separators=(",", ":"), default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", uuid.uuid4().hex)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _envelope(
        self, status: int, code: str, message: str, details: Any = None
    ) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "details": details,
            "traceId": uuid.uuid4().hex,
        }

    def _record(self, route: Route | None, status: int, body: Any) -> None:
        parsed = urlsplit(self.path)
        self.fake.journal.append(
            RequestRecord(
                method=self.command,
                path=parsed.path,
                query=parsed.query,
                status=status,
                kind=route.kind if route else "unknown",
                body=body if isinstance(body, (dict, list)) else None,
            )
        )

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"{AUTH_SCHEME} {self.fake.token}"
        return auth == expected

    def _serve(self) -> None:
        parsed = urlsplit(self.path)
        path, query = parsed.path, parsed.query
        try:
            if self.command == "POST" and path == self.fake.token_url:
                creds = {**self._query(query), **self._form()}
                payload = self.fake._handle_token(creds)
                self._record(None, 200, payload)
                self._respond(200, payload)
                return
            if not self._authorized():
                payload = self._envelope(401, "AUTH_FAILED", "missing or invalid access token")
                self._record(None, 401, payload)
                self._respond(401, payload)
                return
            if self.fake.rate_limit_per_minute > 0 and self.fake._throttle_tripped():
                self._record(None, 429, {})
                self._respond(
                    429,
                    self._envelope(429, "RATE_LIMIT", "request rate limit exceeded"),
                    {"Retry-After": str(self.fake.retry_after_s)},
                )
                return
            route = self.fake.catalog.match(self.command, path)
            if route is None:
                payload = self._envelope(404, "NOT_FOUND", f"no route for {self.command} {path}")
                self._record(None, 404, payload)
                self._respond(404, payload)
                return
            params = self._path_params(route, path)
            payload, status, extra = self.fake.handle(
                route, params, self._query(query), self._body()
            )
            self._record(route, status, payload)
            self._respond(status, payload, extra)
        except FakeApiError as exc:
            payload = self._envelope(exc.status, exc.code, exc.message, exc.details)
            self._record(None, exc.status, payload)
            self._respond(exc.status, payload)
        except Exception as exc:  # noqa: BLE001 — a fake that dies mid-response is undebuggable
            # Without this the handler thread unwinds, the socket closes with no
            # response, and the client sees only "Server disconnected" with no
            # indication of which request or which bug caused it.
            payload = self._envelope(500, "FAKE_INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
            self._record(None, 500, payload)
            self._respond(500, payload)

    def _path_params(self, route: Route, path: str) -> dict[str, str]:
        parts = [p for p in route.pattern.split("/") if p]
        actual = [p for p in path.split("/") if p]
        params: dict[str, str] = {}
        for template, value in zip(parts, actual, strict=True):
            if template.startswith("{") and template.endswith("}"):
                params[template[1:-1]] = value
        return params

    def _query(self, raw: str) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _form(self) -> dict[str, str]:
        """Form-encoded request body (used by the OAuth token endpoint)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # --- HTTP verbs ---
    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PATCH(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass


class FakeCentralServer:
    """Startable/stopable fake Central API server."""

    def __init__(
        self,
        bundle: FixtureBundle | None = None,
        journal: RequestJournal | None = None,
        catalog: EndpointCatalog | None = None,
        rate_limit_per_minute: int = 0,
        retry_after_s: int = 3,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.bundle = bundle or _default_bundle()
        self.journal = journal or RequestJournal()
        self.catalog = catalog or EndpointCatalog()
        self.rate_limit_per_minute = rate_limit_per_minute
        self.retry_after_s = retry_after_s
        self.host = host
        self.port = port
        token_url = "oauth" in self.bundle.env and self.bundle.env["oauth"].get("token_url")
        self.token_url = token_url or "/oauth2/token"
        self._token_value = "benchmark-access-token-0001"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._window_requests: list[float] = []

    @property
    def token(self) -> str:
        return self._token_value

    @property
    def base_url(self) -> str:
        assert self._server is not None, "server not started"
        return f"http://{self.host}:{self._server.server_port}"

    def start(self) -> FakeCentralServer:
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.fake = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="fake-central"
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeCentralServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # --- request handling ---

    def handle(
        self, route: Route, params: dict[str, str], query: dict[str, str], body: dict[str, Any]
    ) -> tuple[Any, int, dict[str, str]]:
        if route.method == "POST" and route.pattern == self.token_url:
            return self._handle_token(query), 200, {}
        if is_write(route.kind):
            return self._write(route, params, body)
        if route.by_id:
            return self._by_id(route, params)
        if route.paginated:
            return self._page(route, query)
        items = self.bundle.items(route.collection)
        return (items if isinstance(items, list) and len(items) == 1 else items), 200, {}

    def _handle_token(self, query: dict[str, str]) -> dict[str, Any]:
        # Both form-encoded and query-string client_credentials are accepted.
        client_id = query.get("client_id")
        client_secret = query.get("client_secret")
        oauth = self.bundle.env.get("oauth", {})
        if client_id != oauth.get("client_id") or client_secret != oauth.get("client_secret"):
            raise FakeApiError(401, "INVALID_CLIENT", "client authentication failed")
        return {
            "access_token": self._token_value,
            "token_type": "bearer",
            "expires_in": 3600,
        }

    def _key(self, params: dict[str, str]) -> str:
        return (
            params.get("serial")
            or params.get("mac")
            or params.get("ssid")
            or params.get("id")
            or ""
        )

    @staticmethod
    def _id_matches(item: Any, key: str) -> bool:
        """Match a fixture item's ``id`` against a path segment.

        Case-insensitive: fixtures carry Central's display casing (``Old-Guest``)
        while request paths carry the lowercased slug (``old-guest``). The two
        name the same entity, so matching on casing alone would 404.
        """
        if not isinstance(item, dict) or not key:
            return False
        return str(item.get("id", "")).casefold() == key.casefold()

    def _write(
        self, route: Route, params: dict[str, str], body: dict[str, Any]
    ) -> tuple[Any, int, dict[str, str]]:
        """Serve a mutating route.

        Writes are acknowledged without mutating the fixture bundle: scoring
        reads the journal (method/path/status/kind), never fixture state, and
        keeping the bundle immutable is what makes scenarios independent of
        execution order.

        ``PATCH``/``POST`` are create-or-update — Central's wlan-ssids PATCH is
        an upsert, so a write scenario targeting a not-yet-existing id must not
        404. ``DELETE`` on a ``by_id`` route still requires the entity to exist,
        because deleting an absent entity is a genuine client error.
        """
        key = self._key(params)
        items = self.bundle.items(route.collection)
        existing = next((item for item in items if self._id_matches(item, key)), None)
        if route.method == "DELETE":
            if route.by_id and existing is None:
                raise FakeApiError(
                    404, "NOT_FOUND", f"{route.collection} {key!r} not found in fixture"
                )
            return {"id": key, "status": "deleted"}, 200, {}
        record: dict[str, Any] = {**(existing or {}), **(body or {})}
        if key:
            record["id"] = key
        status = 200 if existing is not None else 201
        return record, status, {}

    def _by_id(self, route: Route, params: dict[str, str]) -> tuple[Any, int, dict[str, str]]:
        items = self.bundle.items(route.collection)
        key = self._key(params)
        for item in items:
            if self._id_matches(item, key):
                return item, 200, {}
        raise FakeApiError(404, "NOT_FOUND", f"{route.collection} {key!r} not found in fixture")

    def _page(self, route: Route, query: dict[str, str]) -> tuple[Any, int, dict[str, str]]:
        items = self.bundle.items(route.collection)
        try:
            limit = int(query.get("limit", DEFAULT_PAGE_SIZE))
            offset = int(query.get("offset", 0))
        except ValueError as exc:
            raise FakeApiError(400, "BAD_REQUEST", "limit/offset must be integers") from exc
        if limit < 0 or offset < 0:
            raise FakeApiError(400, "BAD_REQUEST", "limit/offset must be non-negative")
        if limit > MAX_PAGE_SIZE:
            raise FakeApiError(400, "BAD_REQUEST", f"limit exceeds maximum of {MAX_PAGE_SIZE}")
        total = len(items)
        window = items[offset : offset + limit]
        result = {"items": window, "total": total, "limit": limit, "offset": offset}
        links: list[str] = []
        if offset + limit < total:
            next_query = dict(query)
            next_query["offset"] = str(offset + limit)
            next_query["limit"] = str(limit)
            links.append(f'<{route.pattern}?{urlencode(next_query)}>; rel="next"')
        return result, 200, {"Link": ", ".join(links)} if links else {}

    # --- rate limiting ---

    def _throttle_tripped(self) -> bool:
        import time

        now = time.monotonic()
        self._window_requests = [t for t in self._window_requests if now - t < 60.0]
        if len(self._window_requests) >= self.rate_limit_per_minute:
            return True
        self._window_requests.append(now)
        return False


def _default_bundle() -> FixtureBundle:
    from .fixtures import default_bundle

    return default_bundle()
