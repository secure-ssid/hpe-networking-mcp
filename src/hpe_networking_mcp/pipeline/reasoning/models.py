"""Data models for AI reasoning, troubleshooting, migration, and network architecture planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProblemCategory(str, Enum):
    WIRELESS_CLIENT_AUTH = "wireless_client_auth"
    WIRED_PORT_HEALTH = "wired_port_health"
    POE_BUDGET = "poe_budget"
    DHCP_IP_AM = "dhcp_ip_am"
    STP_TOPOLOGY_LOOP = "stp_topology_loop"
    ROAMING_RF_HEALTH = "roaming_rf_health"
    GATEWAY_VPNC_CLUSTER = "gateway_vpnc_cluster"
    FIRMWARE_COMPLIANCE = "firmware_compliance"
    GENERAL_NETWORK_HEALTH = "general_network_health"


@dataclass
class PlanStep:
    step_id: str
    title: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    status: StepStatus = StepStatus.PENDING
    result_summary: str = ""
    raw_output: Any = None


@dataclass
class RemediationAction:
    action_id: str
    title: str
    description: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    is_destructive: bool = False
    dry_run_supported: bool = True
    suggested_cli_commands: list[str] = field(default_factory=list)


@dataclass
class RootCauseHypothesis:
    hypothesis_id: str
    category: ProblemCategory
    confidence_score: float  # 0.0 to 1.0
    summary: str
    evidence: list[str] = field(default_factory=list)
    remediation_actions: list[RemediationAction] = field(default_factory=list)


@dataclass
class ReasoningPlan:
    plan_id: str
    goal: str
    category: ProblemCategory
    target_device: str | None = None
    target_site: str | None = None
    target_client: str | None = None
    steps: list[PlanStep] = field(default_factory=list)
    hypotheses: list[RootCauseHypothesis] = field(default_factory=list)
    synthesized_analysis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "category": self.category.value,
            "target_device": self.target_device,
            "target_site": self.target_site,
            "target_client": self.target_client,
            "steps": [
                {
                    "step_id": s.step_id,
                    "title": s.title,
                    "tool_name": s.tool_name,
                    "tool_args": s.tool_args,
                    "rationale": s.rationale,
                    "status": s.status.value,
                    "result_summary": s.result_summary,
                }
                for s in self.steps
            ],
            "hypotheses": [
                {
                    "hypothesis_id": h.hypothesis_id,
                    "category": h.category.value,
                    "confidence_score": round(h.confidence_score, 2),
                    "summary": h.summary,
                    "evidence": h.evidence,
                    "remediation_actions": [
                        {
                            "action_id": r.action_id,
                            "title": r.title,
                            "description": r.description,
                            "tool_name": r.tool_name,
                            "tool_args": r.tool_args,
                            "is_destructive": r.is_destructive,
                            "suggested_cli_commands": r.suggested_cli_commands,
                        }
                        for r in h.remediation_actions
                    ],
                }
                for h in self.hypotheses
            ],
            "synthesized_analysis": self.synthesized_analysis,
        }
