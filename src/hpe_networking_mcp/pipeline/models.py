"""Core data models for the migration hpe_networking_mcp.pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    UNMANAGED = "unmanaged"
    CLASSIC_CENTRAL = "classic_central"
    AOS8 = "aos8"


class HardwareSeries(str, Enum):
    AOS_CX = "aos_cx"
    AOS_S = "aos_s"


class TargetAccount(str, Enum):
    SAME = "same"
    NEW = "new"


class Persona(str, Enum):
    ACCESS_SWITCH = "access_switch"
    CORE_SWITCH = "core_switch"
    AGGREGATION_SWITCH = "aggregation_switch"

    def to_api_value(self) -> str:
        return {
            Persona.ACCESS_SWITCH: "ACCESS_SWITCH",
            Persona.CORE_SWITCH: "CORE_SWITCH",
            Persona.AGGREGATION_SWITCH: "AGG_SWITCH",
        }[self]


class FirmwareAction(str, Enum):
    UPGRADE = "upgrade"
    SKIP = "skip"  # AOS-S cannot run AOS 10


class StageStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class OverallStatus(str, Enum):
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# DeviceRecord — one row from the input CSV
# ---------------------------------------------------------------------------


@dataclass
class DeviceRecord:
    # Required
    serial_number: str
    source_type: SourceType
    hardware_series: HardwareSeries
    target_account: TargetAccount
    target_site: str
    target_group: str
    persona: Persona
    firmware_target: str

    # Optional
    mac_address: Optional[str] = None
    notes: Optional[str] = None

    # Populated during pipeline execution (not from CSV)
    firmware_action: FirmwareAction = FirmwareAction.UPGRADE
    needs_site_create: bool = False
    site_id: Optional[str] = None
    controller_serial: Optional[str] = None  # AOS 8 only
    glp_device_id: Optional[str] = None
    current_firmware: Optional[str] = None
    model: Optional[str] = None
    scope_id: Optional[str] = None
    vlan_config_file: Optional[str] = None
    vlan_interface_config_file: Optional[str] = None


# ---------------------------------------------------------------------------
# StageResult — what a stage returns
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    status: StageStatus
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def success(cls, **data: Any) -> StageResult:
        return cls(status=StageStatus.SUCCESS, data=data)

    @classmethod
    def failed(cls, error: str, **data: Any) -> StageResult:
        return cls(status=StageStatus.FAILED, error=error, data=data)

    @classmethod
    def skipped(cls, reason: str = "") -> StageResult:
        return cls(status=StageStatus.SKIPPED, data={"reason": reason})


# ---------------------------------------------------------------------------
# AccountContext — credentials + clients for one Central account
# ---------------------------------------------------------------------------


@dataclass
class AccountContext:
    label: str  # "source" or "target"
    base_url: str
    client_id: str
    client_secret: str
    glp_workspace_id: str = ""
    glp_token_url: str = "https://sso.common.cloud.hpe.com/as/token.oauth2"
    glp_base_url: str = "https://global.api.greenlake.hpe.com"

    # Populated lazily by src/hpe_networking_mcp/pipeline/clients modules
    central_client: Any = field(default=None, repr=False)
    glp_client: Any = field(default=None, repr=False)
    mcp_client: Any = field(default=None, repr=False)
    global_scope_id: Optional[str] = None
    device_profiles_created: bool = False


# ---------------------------------------------------------------------------
# PipelineRun — top-level run metadata
# ---------------------------------------------------------------------------


@dataclass
class PipelineRun:
    run_id: str
    input_file: str
    dry_run: bool = False
    total_devices: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
