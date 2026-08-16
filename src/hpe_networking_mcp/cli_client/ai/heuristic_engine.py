"""Offline deterministic AI reasoning engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    AiResponse,
    AiStreamChunk,
    ChatMessage,
    MessageRole,
    ToolCallRequest,
)
from hpe_networking_mcp.pipeline.clients import hardware_specs
from hpe_networking_mcp.pipeline.reasoning import (
    create_troubleshooting_plan,
    decompose_query,
    format_architecture_recommendation_markdown,
    format_migration_plan_markdown,
    format_troubleshooting_report,
    plan_migration,
    synthesize_architecture,
)


class HeuristicReasoningEngine(AiBackend):
    """Local, deterministic reasoning engine with zero external API dependencies."""

    @property
    def name(self) -> str:
        return "heuristic-expert"

    @property
    def provider(self) -> str:
        return "heuristic"

    @property
    def model(self) -> str:
        return "rules"

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AiResponse:
        user_msg = next((m.content for m in reversed(messages) if m.role == MessageRole.USER), "")
        q = user_msg.lower()

        # 1. Hardware Specs query
        hw_model = hardware_specs.detect_hardware_query(user_msg)
        if hw_model:
            content = hardware_specs.format_hardware_specs_markdown(hw_model)
            return AiResponse(
                content=content,
                thought_trace=(
                    f"Matched hardware specification model '{hw_model}' "
                    "from authoritative datasheets."
                ),
                finish_reason="stop",
            )

        # 2. Troubleshooting query
        tb_keywords = [
            "troubleshoot",
            "issue",
            "down",
            "error",
            "fail",
            "slow",
            "cant connect",
            "flap",
            "loop",
            "poe",
        ]
        if any(k in q for k in tb_keywords):
            plan = create_troubleshooting_plan(user_msg)
            report = format_troubleshooting_report(plan)
            tool_calls = []
            if plan.steps and plan.steps[0].tool_name:
                tool_calls.append(
                    ToolCallRequest(
                        call_id="call-diag-0",
                        tool_name=plan.steps[0].tool_name,
                        arguments=plan.steps[0].tool_args,
                    )
                )
            return AiResponse(
                content=report,
                thought_trace=(
                    f"Classified problem as {plan.category.value}. Formulated "
                    f"{len(plan.steps)} diagnostic steps and {len(plan.hypotheses)} "
                    "root-cause hypotheses."
                ),
                tool_calls=tool_calls,
                finish_reason="tool_calls" if tool_calls else "stop",
            )

        # 3. Migration query
        mg_keywords = [
            "migrate",
            "migration",
            "procurve to cx",
            "cisco to cx",
            "aos-s to cx",
            "translate",
        ]
        if any(k in q for k in mg_keywords):
            vendor = "cisco" if "cisco" in q else "aos-s"
            plan_dict = plan_migration(vendor)
            content = format_migration_plan_markdown(plan_dict)
            return AiResponse(
                content=content,
                thought_trace=(
                    f"Synthesized {vendor.upper()} to AOS-CX migration blueprint "
                    "with syntax translation."
                ),
                finish_reason="stop",
            )

        # 4. Architecture / Design query
        ds_keywords = [
            "design",
            "architect",
            "bom",
            "spine-leaf",
            "evpn",
            "fabric",
            "campus design",
        ]
        if any(k in q for k in ds_keywords):
            env_type = "datacenter" if any(k in q for k in ["dc", "fabric", "evpn"]) else "campus"
            rec = synthesize_architecture(environment=env_type)
            content = format_architecture_recommendation_markdown(rec)
            return AiResponse(
                content=content,
                thought_trace=(
                    "Generated architecture recommendation and hardware Bill of Materials (BOM)."
                ),
                finish_reason="stop",
            )

        # 5. General query plan
        plan = decompose_query(user_msg)
        tool_calls = []
        for i, act in enumerate(plan.actions[:2]):
            tool_calls.append(
                ToolCallRequest(
                    call_id=f"call-act-{i}",
                    tool_name=act.tool_name,
                    arguments=act.arguments,
                )
            )

        return AiResponse(
            content=(
                f"Plan formulated: {plan.intent_summary} (Complexity: {plan.estimated_complexity})"
            ),
            thought_trace=f"Decomposed query into {len(plan.actions)} planned actions.",
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def complete_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[AiStreamChunk]:
        resp = await self.complete(messages, tools, system_prompt)
        yield AiStreamChunk(
            delta_content=resp.content,
            thought_content=resp.thought_trace,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )
