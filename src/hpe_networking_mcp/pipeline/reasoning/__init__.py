"""HPE Networking MCP Reasoning & AI Decision Engine."""

from __future__ import annotations

from hpe_networking_mcp.pipeline.reasoning.migration_planner import (
    format_migration_plan_markdown,
    plan_migration,
)
from hpe_networking_mcp.pipeline.reasoning.models import (
    PlanStep,
    ProblemCategory,
    ReasoningPlan,
    RemediationAction,
    RootCauseHypothesis,
    StepStatus,
)
from hpe_networking_mcp.pipeline.reasoning.network_architect import (
    ArchitectureRecommendation,
    format_architecture_recommendation_markdown,
    synthesize_architecture,
)
from hpe_networking_mcp.pipeline.reasoning.query_planner import (
    PlannedAction,
    QueryExecutionPlan,
    decompose_query,
)
from hpe_networking_mcp.pipeline.reasoning.troubleshooting_reasoner import (
    classify_troubleshooting_intent,
    create_troubleshooting_plan,
    extract_target_entities,
    format_troubleshooting_report,
)

__all__ = [
    "ArchitectureRecommendation",
    "PlanStep",
    "PlannedAction",
    "ProblemCategory",
    "QueryExecutionPlan",
    "ReasoningPlan",
    "RemediationAction",
    "RootCauseHypothesis",
    "StepStatus",
    "classify_troubleshooting_intent",
    "create_troubleshooting_plan",
    "decompose_query",
    "extract_target_entities",
    "format_architecture_recommendation_markdown",
    "format_migration_plan_markdown",
    "format_troubleshooting_report",
    "plan_migration",
    "synthesize_architecture",
]
