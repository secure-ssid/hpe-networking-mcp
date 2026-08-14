"""MCP session lifecycle (stdio + streamable-HTTP + multi-server)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.session_group import (
    ClientSessionGroup,
    ClientSessionParameters,
    SseServerParameters,
    StreamableHttpParameters,
)
from mcp.client.stdio import StdioServerParameters
from mcp_types import Implementation

from hpe_networking_mcp.cli_client.config import ServerProfile


def _namespace_hook(component_name: str, server_info: Implementation) -> str:
    """Prefix tools/resources/prompts with the server name to avoid collisions."""
    server = (server_info.name or "server").strip().replace(" ", "-")
    return f"{server}.{component_name}"


def profile_to_params(
    profile: ServerProfile,
) -> StdioServerParameters | StreamableHttpParameters | SseServerParameters:
    """Convert a ServerProfile into the SDK parameter object."""
    if profile.transport == "stdio":
        if not profile.command:
            raise ValueError(f"profile {profile.name!r}: stdio requires command")
        return StdioServerParameters(
            command=profile.command,
            args=list(profile.args),
            env=dict(profile.env) or None,
            cwd=profile.cwd,
        )
    if not profile.url:
        raise ValueError(f"profile {profile.name!r}: {profile.transport} requires url")
    if profile.transport == "sse":
        return SseServerParameters(url=profile.url, headers=dict(profile.headers) or None)
    return StreamableHttpParameters(
        url=profile.url,
        headers=dict(profile.headers) or None,
        terminate_on_close=True,
    )


@dataclass
class ConnectedServer:
    profile: ServerProfile
    session: ClientSession
    server_name: str


@dataclass
class SessionManager:
    """Owns a ClientSessionGroup and tracks profile → session mapping."""

    group: ClientSessionGroup
    connected: dict[str, ConnectedServer] = field(default_factory=dict)

    @classmethod
    def create(cls, *, namespace: bool = True) -> SessionManager:
        hook = _namespace_hook if namespace else None
        return cls(group=ClientSessionGroup(component_name_hook=hook))

    async def __aenter__(self) -> SessionManager:
        await self.group.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool | None:
        return await self.group.__aexit__(*exc)

    async def connect(self, profile: ServerProfile) -> ConnectedServer:
        if profile.name in self.connected:
            return self.connected[profile.name]
        params = profile_to_params(profile)
        session = await self.group.connect_to_server(
            params,
            session_params=ClientSessionParameters(
                client_info=Implementation(name="hpe-networking-mcp-client", version="0.8.0"),
            ),
        )
        # Best-effort server name from initialize result if available.
        server_name = profile.name
        try:
            init = getattr(session, "_server_info", None) or getattr(session, "server_info", None)
            if init is not None and getattr(init, "name", None):
                server_name = str(init.name)
        except Exception:
            pass
        rec = ConnectedServer(profile=profile, session=session, server_name=server_name)
        self.connected[profile.name] = rec
        return rec

    async def disconnect(self, profile_name: str) -> None:
        rec = self.connected.pop(profile_name, None)
        if rec is None:
            return
        await self.group.disconnect_from_server(rec.session)

    @property
    def tools(self) -> dict[str, Any]:
        return dict(self.group.tools)

    @property
    def resources(self) -> dict[str, Any]:
        return dict(self.group.resources)

    @property
    def prompts(self) -> dict[str, Any]:
        return dict(self.group.prompts)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.group.call_tool(name, arguments or {})

    def resolve_tool_name(self, query: str) -> str:
        """Resolve bare or namespaced tool names.

        Accepts:
          - exact namespaced name
          - bare tool name when unique across servers
          - suffix match (``server.tool`` ends with ``.tool``)
        """
        tools = self.tools
        if query in tools:
            return query
        # bare exact
        bare_hits = [n for n in tools if n == query or n.endswith(f".{query}")]
        if len(bare_hits) == 1:
            return bare_hits[0]
        if len(bare_hits) > 1:
            opts = ", ".join(sorted(bare_hits))
            raise KeyError(f"ambiguous tool {query!r}; candidates: {opts}")
        # case-insensitive contains
        q = query.lower()
        soft = [n for n in tools if q in n.lower()]
        if len(soft) == 1:
            return soft[0]
        if not soft:
            raise KeyError(f"unknown tool {query!r}")
        opts = ", ".join(sorted(soft)[:12])
        raise KeyError(f"ambiguous tool {query!r}; candidates: {opts}")
