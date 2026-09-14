---
title: "Chatbot session 404 loop diagnosis"
nav_order: 10
---

# Chatbot session 404 loop diagnosis

**Investigated:** 2026-09-04
**Scope:** The separate read-only chatbot deployment on port 8011

## Conclusion

The HPE Networking MCP server is not expiring sessions on a short timer and
did not crash-loop during the observed incident. The repeated `GET /mcp`
requests were made with a session ID that no longer existed after the
container was recreated. The chatbot client continued polling that stale ID
instead of discarding it and starting a new MCP initialize handshake.

This is a reconnect bug in the separate chatbot client. No server-side change
is in scope for this repository.

## Evidence

The live container inspection showed:

- The chatbot router was created at `2026-09-03T03:11:38Z` and started at
  `2026-09-03T03:11:50Z`.
- Docker reported `restart_count=0`, `oom=false`, and `healthy`.
- The main coding-agent router had the same healthy state and startup window.
- The chatbot router's wrapper cache was configured for 30 seconds. That cache
  stores wrapper results; it is not an MCP transport-session TTL.

The redacted chatbot log summary showed:

- The first stale-session 404 at `2026-09-03T03:12:02Z`, about 12 seconds
  after startup.
- 300 `GET /mcp` 404 responses through `2026-09-03T03:27:06Z`, with roughly
  three seconds between retries.
- 300 matching `unknown or expired session ID` messages.
- 54 successful session creations, 54 successful `GET /mcp` responses, 160
  successful `POST /mcp` responses, and 54 accepted `POST /mcp` responses.
- Zero session idle-timeout messages, zero session-crash cleanup messages,
  and zero error-level log lines in the captured window.

The health endpoints also returned HTTP 200 for `/livez`, `/readyz`, and
`/healthz` during the investigation.

## Session lifetime review

The repository pins MCP SDK `2.0.0`. Its
`StreamableHTTPSessionManager` supports an optional `session_idle_timeout`,
but the public `MCPServer.streamable_http_app()` path does not pass one. The
effective default is therefore `None` (no idle timeout). Sessions are held in
the server process's in-memory session map, so a process/container restart
necessarily invalidates session IDs created before that restart.

The server's 30-second
`HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_SECONDS` setting cannot explain a
three-second session retry loop or the loss of a session across a restart.

## Client handoff

When a request receives the MCP "session not found" response, the chatbot
client should:

1. Discard the cached `Mcp-Session-Id`.
2. Stop polling the old session.
3. Send a new `initialize` request without the stale session header.
4. Store the new session ID and resume normal polling.

The server should continue returning 404 for an unknown session ID. Changing
that behavior would hide stale-client state and would not restore the lost
session.
