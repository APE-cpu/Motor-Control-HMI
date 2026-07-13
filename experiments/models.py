"""实验领域模型。

模型只依赖 Python 标准库，确保未来可被 GUI、命令行工具和数据回放器共同使用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass
class DeviceProfile:
    """一次实验所使用设备的可追溯快照。"""

    name: str
    motor_type: str = "PMSM"
    rated_power_w: float | None = None
    dc_bus_voltage_v: float | None = None
    inverter: str = ""
    controller: str = ""
    sensors: list[str] = field(default_factory=list)
    firmware_version: str = ""
    protocol_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceProfile":
        return cls(**data)


@dataclass
class EquipmentProfile:
    """可复用且带修订号的电机平台组合档案。"""

    profile_id: str
    family_id: str
    revision: int
    name: str
    motor_type: str = "PMSM"
    rated_power_w: float | None = None
    nominal_bus_voltage_v: float | None = None
    inverter: str = ""
    controller: str = ""
    sensors: list[str] = field(default_factory=list)
    expected_device_id: str = ""
    expected_hardware_version: str = ""
    expected_firmware_prefix: str = ""
    safety_limits: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    created_at: str = ""
    built_in: bool = False

    def __post_init__(self) -> None:
        self.profile_id = self.profile_id.strip()
        self.family_id = self.family_id.strip()
        self.name = self.name.strip()
        if not self.profile_id or not self.family_id or not self.name:
            raise ValueError("设备档案必须包含编号、系列编号和名称")
        if int(self.revision) < 1:
            raise ValueError("设备档案修订号必须大于0")
        self.revision = int(self.revision)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EquipmentProfile":
        return cls(**data)


@dataclass
class WorkflowStep:
    """实验模板中的单个人工引导步骤，不直接执行危险控制命令。"""

    step_id: str
    title: str
    instruction: str = ""
    expected_result: str = ""
    required: bool = True
    required_runtime_state: str = ""

    def __post_init__(self) -> None:
        self.step_id = self.step_id.strip()
        self.title = self.title.strip()
        if not self.step_id or not self.title:
            raise ValueError("实验步骤必须包含step_id和标题")
        if self.required_runtime_state and self.required_runtime_state not in {
                "disconnected", "connected", "precheck", "ready", "running",
                "stopping", "fault_locked"}:
            raise ValueError("步骤要求了未知运行状态")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        return cls(**data)


@dataclass
class ExperimentTemplate:
    """可复用实验方案；开始实验时会完整冻结到会话。"""

    template_id: str
    name: str
    purpose: str = ""
    data_source: str = "sim"
    device_defaults: dict[str, Any] = field(default_factory=dict)
    safety_limits: dict[str, Any] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)
    version: int = 1
    updated_at: str = ""
    built_in: bool = False

    def __post_init__(self) -> None:
        self.template_id = self.template_id.strip()
        self.name = self.name.strip()
        if not self.template_id or not self.name:
            raise ValueError("实验模板必须包含template_id和名称")
        if self.data_source not in {"sim", "real"}:
            raise ValueError("模板数据源必须为sim或real")
        seen = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"步骤编号重复: {step.step_id}")
            seen.add(step.step_id)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentTemplate":
        values = dict(data)
        values["steps"] = [WorkflowStep.from_dict(item)
                           for item in values.get("steps", [])]
        return cls(**values)


@dataclass
class ExperimentSession:
    """单次实验的元数据；大体量遥测不嵌入该对象。"""

    experiment_id: str
    name: str
    purpose: str = ""
    operator: str = ""
    data_source: str = "sim"
    status: SessionStatus = SessionStatus.CREATED
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    end_reason: str = ""
    notes: str = ""
    software_version: str = ""
    device: DeviceProfile | None = None
    controller_params: dict[str, Any] = field(default_factory=dict)
    protection_params: dict[str, Any] = field(default_factory=dict)
    template_id: str = ""
    template_snapshot: dict[str, Any] = field(default_factory=dict)
    workflow_current_index: int = 0
    workflow_completed_steps: list[str] = field(default_factory=list)
    runtime_context: dict[str, Any] = field(default_factory=dict)
    conclusion: dict[str, Any] = field(default_factory=dict)
    telemetry_count: int = 0
    event_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentSession":
        values = dict(data)
        values["status"] = SessionStatus(values["status"])
        device = values.get("device")
        if device is not None:
            values["device"] = DeviceProfile.from_dict(device)
        return cls(**values)
