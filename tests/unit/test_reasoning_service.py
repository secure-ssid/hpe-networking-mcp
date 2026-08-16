from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hpe_networking_mcp.cli_client.ai import (
    AiBackend,
    AiResponse,
    AiStreamChunk,
    ChatMessage,
    ReasoningEvent,
    ReasoningService,
    ToolCallDelta,
    ToolCallRequest,
    bound_tool_result,
)
from hpe_networking_mcp.cli_client.config import load_client_config
from hpe_networking_mcp.cli_client.safety import SafetyPolicy


class _Backend(AiBackend):
    def __init__(self, responses: list[list[AiStreamChunk]]) -> None:
        self.responses = responses
        self.calls: list[list[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "test-provider:test-model"

    async def complete(
        self,
        messages: list[ChatMessage],
        tools=None,
        system_prompt=None,
    ) -> AiResponse:
        chunks = self.responses[min(len(self.calls), len(self.responses) - 1)]
        self.calls.append(list(messages))
        return AiResponse(content="".join(chunk.delta_content for chunk in chunks))

    async def complete_stream(
        self,
        messages: list[ChatMessage],
        tools=None,
        system_prompt=None,
    ):
        self.calls.append(list(messages))
        for chunk in self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]:
            yield chunk


class _Tool:
    def __init__(self, name: str, read_only: bool) -> None:
        self.name = name
        self.description = name
        self.inputSchema = {"type": "object"}
        self.annotations = {"readOnlyHint": read_only}


class _Manager:
    def __init__(self, tool: _Tool) -> None:
        self.tools = {tool.name: tool}
        self.calls: list[tuple[str, dict]] = []

    async def list_all_tools(self):
        return list(self.tools.values())

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {"ok": True, "data": "x" * 1000}


@pytest.mark.anyio
async def test_service_streams_without_exposing_thought_and_keeps_memory():
    backend = _Backend(
        [
            [
                AiStreamChunk(
                    delta_content="answer",
                    thought_content="private chain of thought",
                    finish_reason="stop",
                )
            ]
        ]
    )
    service = ReasoningService(backend)

    events = [event async for event in service.stream("hello")]

    assert [event.kind for event in events] == ["started", "text_delta", "completed"]
    assert all("private chain" not in event.content for event in events)
    assert service.metadata == {
        "provider": "test-provider",
        "model": "test-model",
        "backend": "test-provider:test-model",
    }
    assert [message.content for message in service.memory.messages] == ["hello", "answer"]

    await service.complete("follow up")
    assert len(backend.calls[-1]) == 3


@pytest.mark.anyio
async def test_service_dispatches_read_only_and_blocks_writes():
    read_backend = _Backend(
        [
            [
                AiStreamChunk(
                    tool_calls=[
                        ToolCallRequest("read-1", "list_clients", {}),
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [AiStreamChunk(delta_content="read complete", finish_reason="stop")],
        ]
    )
    read_manager = _Manager(_Tool("list_clients", read_only=True))
    read_service = ReasoningService(
        read_backend,
        read_manager,
        safety_policy=SafetyPolicy(),
        max_result_chars=120,
    )
    read_events = [event async for event in read_service.stream("check clients")]
    assert read_manager.calls == [("list_clients", {})]
    result_event = next(event for event in read_events if event.kind == "tool_result")
    assert result_event.allowed is True
    assert len(result_event.content) <= 120

    write_backend = _Backend(
        [[AiStreamChunk(tool_calls=[ToolCallRequest("write-1", "create_site", {})])]]
    )
    write_manager = _Manager(_Tool("create_site", read_only=False))
    write_service = ReasoningService(write_backend, write_manager)
    write_events = [event async for event in write_service.stream("create a site")]
    assert write_manager.calls == []
    blocked = next(event for event in write_events if event.kind == "tool_result")
    assert blocked.allowed is False
    assert "blocked" in blocked.content.lower() or "read-only" in blocked.content.lower()


@pytest.mark.anyio
async def test_service_accepts_catalog_and_dispatcher_without_session_manager():
    backend = _Backend(
        [
            [AiStreamChunk(tool_calls=[ToolCallRequest("read-1", "list_clients", {})])],
            [AiStreamChunk(delta_content="done")],
        ]
    )
    calls = []

    async def dispatch(name, arguments):
        calls.append((name, arguments))
        return {"ok": True}

    service = ReasoningService(
        backend,
        tool_dispatch=dispatch,
        tool_specs=[{"name": "list_clients", "read_only": True}],
    )
    events = [event async for event in service.stream("inspect")]
    assert calls == [("list_clients", {})]
    assert any(event.kind == "completed" and event.content == "done" for event in events)


@pytest.mark.anyio
async def test_service_resolves_namespaced_mcp_tools_for_model_safe_names():
    backend = _Backend(
        [
            [AiStreamChunk(tool_calls=[ToolCallRequest("call-1", "router_list_clients", {})])],
            [AiStreamChunk(delta_content="done")],
        ]
    )
    tool = _Tool("list_clients", read_only=True)

    class NamespacedManager(_Manager):
        def __init__(self):
            self.tools = {"router.list_clients": tool}
            self.calls = []

        def resolve_tool_name(self, name):
            if name in self.tools:
                return name
            if name == "list_clients":
                return "router.list_clients"
            raise KeyError(name)

    manager = NamespacedManager()
    service = ReasoningService(backend, manager)
    [event async for event in service.stream("inspect")]
    assert manager.calls == [("router.list_clients", {})]


def test_stream_tool_call_delta_merge_deduplicates_repeated_metadata():
    calls = ReasoningService._merge_tool_call_deltas(
        [
            ToolCallDelta(
                index=0,
                call_id="toolu_1",
                tool_name="list_clients",
            ),
            ToolCallDelta(
                index=0,
                call_id="toolu_1",
                tool_name="list_clients",
                arguments_fragment='{"limit":',
            ),
            ToolCallDelta(
                index=0,
                call_id="toolu_1",
                tool_name="list_clients",
                arguments_fragment="10}",
            ),
        ]
    )
    assert calls == [
        ToolCallRequest(
            call_id="toolu_1",
            tool_name="list_clients",
            arguments={"limit": 10},
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments_fragment", "error"),
    [
        ('{"limit":', "invalid JSON arguments"),
        ("[]", "JSON object"),
    ],
)
async def test_service_refuses_malformed_streamed_tool_arguments(
    arguments_fragment: str, error: str
):
    backend = _Backend(
        [
            [
                AiStreamChunk(
                    tool_call_deltas=[
                        ToolCallDelta(
                            index=0,
                            call_id="unsafe-1",
                            tool_name="create_site",
                            arguments_fragment=arguments_fragment,
                        )
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [AiStreamChunk(delta_content="request refused", finish_reason="stop")],
        ]
    )
    manager = _Manager(_Tool("create_site", read_only=False))
    service = ReasoningService(
        backend,
        manager,
        safety_policy=SafetyPolicy(
            read_only_default=False,
            allow_writes=True,
            confirmed=True,
        ),
    )

    events = [event async for event in service.stream("create a site")]

    assert manager.calls == []
    blocked = next(event for event in events if event.kind == "tool_result")
    assert blocked.allowed is False
    assert error in blocked.content


@pytest.mark.anyio
async def test_service_refuses_non_object_tool_arguments():
    backend = _Backend(
        [
            [AiStreamChunk(tool_calls=[ToolCallRequest("unsafe-1", "create_site", [])])],
            [AiStreamChunk(delta_content="request refused", finish_reason="stop")],
        ]
    )
    manager = _Manager(_Tool("create_site", read_only=False))
    service = ReasoningService(
        backend,
        manager,
        safety_policy=SafetyPolicy(
            read_only_default=False,
            allow_writes=True,
            confirmed=True,
        ),
    )

    events = [event async for event in service.stream("create a site")]

    assert manager.calls == []
    blocked = next(event for event in events if event.kind == "tool_result")
    assert blocked.allowed is False
    assert "JSON object" in blocked.content


@pytest.mark.anyio
async def test_service_cancellation_and_empty_prompt_do_not_call_backend():
    backend = _Backend([[AiStreamChunk(delta_content="unexpected")]])
    service = ReasoningService(backend)
    cancel = asyncio.Event()
    cancel.set()

    cancelled = [event async for event in service.stream("hello", cancel_event=cancel)]
    assert [event.kind for event in cancelled] == ["started", "cancelled"]
    assert backend.calls == []

    assert [event async for event in service.stream("   ")] == []


@pytest.mark.anyio
async def test_service_cancellation_interrupts_a_waiting_provider_stream():
    started = asyncio.Event()

    class SlowBackend(_Backend):
        async def complete_stream(self, messages, tools=None, system_prompt=None):
            started.set()
            await asyncio.sleep(30)
            yield AiStreamChunk(delta_content="too late")

    service = ReasoningService(SlowBackend([[]]))
    cancel = asyncio.Event()

    async def collect():
        return [event async for event in service.stream("hello", cancel_event=cancel)]

    pending = asyncio.create_task(collect())
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel.set()
    events = await asyncio.wait_for(pending, timeout=1)

    assert [event.kind for event in events] == ["started", "cancelled"]


@pytest.mark.anyio
async def test_service_cancellation_interrupts_legacy_complete_fallback():
    started = asyncio.Event()

    class CompleteOnlySlowBackend(_Backend):
        async def complete_stream(self, messages, tools=None, system_prompt=None):
            if False:
                yield AiStreamChunk()

        async def complete(self, messages, tools=None, system_prompt=None):
            started.set()
            await asyncio.sleep(30)
            return AiResponse(content="too late")

    service = ReasoningService(CompleteOnlySlowBackend([[]]))
    cancel = asyncio.Event()

    async def collect():
        return [event async for event in service.stream("hello", cancel_event=cancel)]

    pending = asyncio.create_task(collect())
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel.set()
    events = await asyncio.wait_for(pending, timeout=1)

    assert [event.kind for event in events] == ["started", "cancelled"]


def test_bound_tool_result_is_strict_and_preserves_error_marker():
    result = bound_tool_result(Exception("connection refused"), max_chars=32)
    assert len(result) <= 32
    assert "ERROR" in result or "truncated" in result


def test_ai_config_env_overrides_file(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        '{"ai": {"provider": "ollama", "model": "file-model"}, "servers": {}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HPE_MCP_AI_PROVIDER", "openai")
    monkeypatch.setenv("HPE_MCP_AI_MODEL", "env-model")
    cfg = load_client_config(config_path=config)
    assert cfg.ai_provider == "openai"
    assert cfg.ai_model == "env-model"


@pytest.mark.anyio
async def test_plain_chat_non_tty_exits_before_connecting(monkeypatch):
    from hpe_networking_mcp.cli import mcp_cli

    monkeypatch.setattr(
        mcp_cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False))
    )

    class _UnexpectedSession:
        @classmethod
        def create(cls, **_kwargs):
            raise AssertionError("non-TTY chat must not create an MCP session")

    monkeypatch.setattr(mcp_cli, "SessionManager", _UnexpectedSession)
    assert await mcp_cli.run_chat(SimpleNamespace(default_profile="local"), SafetyPolicy()) == 0


@pytest.mark.anyio
async def test_plain_chat_dispatches_nonempty_prompt_to_configured_backend(monkeypatch):
    from hpe_networking_mcp.cli import mcp_cli
    from hpe_networking_mcp.cli_client.config import ServerProfile

    lines = iter(["check wireless health"])
    seen: list[str] = []

    def fake_read_line(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration as exc:
            raise EOFError from exc

    class FakeManager:
        tools = {}

        @classmethod
        def create(cls, **_kwargs):
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    profile = ServerProfile(name="local", transport="stdio", command="true")

    async def fake_ensure_connected(_mgr, _cfg):
        return profile

    class FakeService:
        metadata = {"provider": "ollama", "model": "qwen"}
        total_usage = {}

        async def stream(self, prompt, *, cancel_event=None):
            seen.append(prompt)
            yield ReasoningEvent(kind="started", provider="ollama", model="qwen")
            yield ReasoningEvent(
                kind="text_delta",
                content="backend answer",
                provider="ollama",
                model="qwen",
            )
            yield ReasoningEvent(
                kind="completed",
                content="backend answer",
                provider="ollama",
                model="qwen",
            )

    monkeypatch.setattr(mcp_cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    monkeypatch.setattr(mcp_cli, "read_line", fake_read_line)
    monkeypatch.setattr(mcp_cli, "SessionManager", FakeManager)
    monkeypatch.setattr(mcp_cli, "ensure_connected", fake_ensure_connected)
    monkeypatch.setattr(
        mcp_cli, "create_reasoning_service", lambda *_args, **_kwargs: FakeService()
    )

    cfg = SimpleNamespace(default_profile="local", ai_provider="ollama", ai_model="qwen")
    assert await mcp_cli.run_chat(cfg, SafetyPolicy(), quiet=True) == 0
    assert seen == ["check wireless health"]


@pytest.mark.anyio
async def test_terminal_chat_renders_tool_events_without_thought(monkeypatch):
    from hpe_networking_mcp.cli import mcp_cli

    rendered: list[tuple[tuple, dict]] = []

    class FakeConsole:
        def print(self, *args, **kwargs):
            rendered.append((args, kwargs))

    class FakeService:
        metadata = {"provider": "test", "model": "test"}
        total_usage = {}

        async def stream(self, _prompt, *, cancel_event=None):
            yield ReasoningEvent(
                kind="tool_call",
                tool_name="list_clients",
                is_read_only=True,
            )
            yield ReasoningEvent(
                kind="tool_result",
                content="2 clients found",
                tool_name="list_clients",
                allowed=True,
            )
            yield ReasoningEvent(kind="completed", content="done")

    monkeypatch.setattr(mcp_cli, "console", FakeConsole())

    assert await mcp_cli._stream_terminal_request(FakeService(), "inspect") == 0
    output = "\n".join(str(args[0]) for args, _kwargs in rendered if args)
    assert "list_clients" in output
    assert "2 clients found" in output
    assert "thought" not in output.lower()
