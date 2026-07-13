"""实验会话生命周期管理。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    DeviceProfile, ExperimentSession, ExperimentTemplate, SessionStatus,
    WorkflowStep,
)
from .repository import ExperimentRepository


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class ExperimentSessionManager:
    """同一管理器同一时刻只允许一个运行中的实验。"""

    def __init__(self, root: str | Path) -> None:
        self.repository = ExperimentRepository(root)
        self._active: ExperimentSession | None = None
        self._started_monotonic: float | None = None

    @property
    def active_session(self) -> ExperimentSession | None:
        return self._active

    def create_session(
        self,
        name: str,
        *,
        purpose: str = "",
        operator: str = "",
        data_source: str = "sim",
        software_version: str = "",
        device: DeviceProfile | None = None,
        controller_params: dict[str, Any] | None = None,
        protection_params: dict[str, Any] | None = None,
        notes: str = "",
        template: ExperimentTemplate | None = None,
    ) -> ExperimentSession:
        if not name.strip():
            raise ValueError("实验名称不能为空")
        if self._active is not None:
            raise RuntimeError("已有实验处于活动状态")
        now = datetime.now()
        session = ExperimentSession(
            experiment_id=self.repository.allocate_id(now),
            name=name.strip(),
            purpose=purpose,
            operator=operator,
            data_source=data_source,
            created_at=_iso_now(),
            software_version=software_version,
            device=device,
            controller_params=dict(controller_params or {}),
            protection_params=dict(protection_params or {}),
            notes=notes,
            template_id=template.template_id if template else "",
            template_snapshot=template.to_dict() if template else {},
        )
        self.repository.create(session)
        self._active = session
        return session

    def start(self) -> ExperimentSession:
        session = self._require_active()
        if session.status is not SessionStatus.CREATED:
            raise RuntimeError(f"当前状态不能开始实验: {session.status.value}")
        session.status = SessionStatus.RUNNING
        session.started_at = _iso_now()
        self._started_monotonic = time.monotonic()
        self.repository.save(session)
        self.record_event(
            "session_started", "实验开始",
            {"template_id": session.template_id,
             "template_version": session.template_snapshot.get("version")})
        return session

    def current_workflow_step(self) -> WorkflowStep | None:
        session = self._require_active()
        steps = self._workflow_steps(session)
        if session.workflow_current_index >= len(steps):
            return None
        return steps[session.workflow_current_index]

    def confirm_current_step(self, note: str = "") -> WorkflowStep:
        session = self._require_running()
        step = self.current_workflow_step()
        if step is None:
            raise RuntimeError("实验工作流已经全部完成")
        index = session.workflow_current_index
        session.workflow_current_index += 1
        session.workflow_completed_steps.append(step.step_id)
        self.record_event(
            "workflow_step_confirmed", f"步骤完成：{step.title}",
            {"step_id": step.step_id, "step_index": index,
             "required": step.required, "note": note.strip(),
             "expected_result": step.expected_result,
             "required_runtime_state": step.required_runtime_state})
        return step

    def skip_current_step(self, reason: str) -> WorkflowStep:
        session = self._require_running()
        step = self.current_workflow_step()
        if step is None:
            raise RuntimeError("实验工作流已经全部完成")
        if step.required:
            raise RuntimeError("必做步骤不能跳过")
        if not reason.strip():
            raise ValueError("跳过可选步骤必须填写原因")
        index = session.workflow_current_index
        session.workflow_current_index += 1
        self.record_event(
            "workflow_step_skipped", f"跳过可选步骤：{step.title}",
            {"step_id": step.step_id, "step_index": index,
             "reason": reason.strip()})
        return step

    def workflow_progress(self) -> tuple[int, int]:
        session = self._require_active()
        return session.workflow_current_index, len(self._workflow_steps(session))

    def capture_runtime_context(self, stage: str, context: dict[str, Any]) -> None:
        """冻结报告所需的连接、协议和状态机上下文。"""
        if stage not in {"start", "end"}:
            raise ValueError("运行上下文阶段必须为start或end")
        session = self._require_active()
        session.runtime_context[stage] = dict(context)
        self.repository.save(session)

    def record_marker(self, category: str, message: str,
                      snapshot: dict[str, Any] | None = None,
                      note: str = "") -> None:
        if not category.strip() or not message.strip():
            raise ValueError("事件标记必须包含类别和说明")
        self.record_event(
            "experiment_marker", message.strip(),
            {"category": category.strip(), "note": note.strip(),
             "snapshot": dict(snapshot or {})})

    def update_conclusion(self, experiment_id: str,
                          conclusion: dict[str, Any]) -> ExperimentSession:
        allowed_status = {"pending", "passed", "partial", "failed"}
        status = str(conclusion.get("result_status", "pending"))
        if status not in allowed_status:
            raise ValueError("实验结论状态无效")
        clean = {
            "result_status": status,
            "observations": str(conclusion.get("observations", "")).strip(),
            "anomalies": str(conclusion.get("anomalies", "")).strip(),
            "recommendations": str(conclusion.get("recommendations", "")).strip(),
            "next_plan": str(conclusion.get("next_plan", "")).strip(),
            "updated_at": _iso_now(),
        }
        if self._active is not None and self._active.experiment_id == experiment_id:
            session = self._active
            session.conclusion = clean
            self.record_event("conclusion_updated", "实验结论已更新",
                              {"result_status": status})
            return session
        session = self.repository.load(experiment_id)
        session.conclusion = clean
        self.repository.append_event(experiment_id, {
            "timestamp": _iso_now(), "monotonic_s": None,
            "type": "conclusion_updated", "message": "归档后更新实验结论",
            "details": {"result_status": status},
        })
        session.event_count += 1
        self.repository.save(session)
        return session

    def record_event(self, event_type: str, message: str = "",
                     details: dict[str, Any] | None = None) -> None:
        session = self._require_running()
        event = {
            "timestamp": _iso_now(),
            "monotonic_s": self._elapsed(),
            "type": event_type,
            "message": message,
            "details": details or {},
        }
        self.repository.append_event(session.experiment_id, event)
        session.event_count += 1
        self.repository.save(session)

    def record_telemetry(self, frame: Any, timestamp: str | None = None) -> None:
        session = self._require_running()
        row = {
            "timestamp": timestamp or _iso_now(),
            "monotonic_s": self._elapsed(),
        }
        for field in (
            "speed_actual", "speed_target", "current_actual", "current_target",
            "torque_actual", "torque_target", "angle_actual", "temperature",
            "vdc", "bus_state", "sensor_source", "sensor_quality", "convergence",
            "low_speed_warn", "data_source",
            "fault_code", "fault_text",
        ):
            if isinstance(frame, dict):
                row[field] = frame.get(field, "")
            else:
                row[field] = getattr(frame, field, "")
        written = self.repository.append_telemetry(session.experiment_id, [row])
        session.telemetry_count += written
        self.repository.save(session)

    def complete(self, reason: str = "正常结束") -> ExperimentSession:
        return self._finish(SessionStatus.COMPLETED, reason)

    def abort(self, reason: str) -> ExperimentSession:
        if not reason.strip():
            raise ValueError("中止实验必须提供原因")
        return self._finish(SessionStatus.ABORTED, reason.strip())

    def load(self, experiment_id: str) -> ExperimentSession:
        return self.repository.load(experiment_id)

    def _finish(self, status: SessionStatus, reason: str) -> ExperimentSession:
        session = self._require_running()
        if status is SessionStatus.COMPLETED:
            remaining = self._workflow_steps(session)[session.workflow_current_index:]
            required = [step.title for step in remaining if step.required]
            if required:
                raise RuntimeError("仍有必做步骤未完成：" + "、".join(required))
        # 结束事件必须在状态切换前记录，确保仍满足 running 约束。
        self.record_event("session_completed" if status is SessionStatus.COMPLETED
                          else "session_aborted", reason)
        session.status = status
        session.ended_at = _iso_now()
        session.end_reason = reason
        self.repository.save(session)
        self._active = None
        self._started_monotonic = None
        return session

    def _require_active(self) -> ExperimentSession:
        if self._active is None:
            raise RuntimeError("当前没有活动实验")
        return self._active

    def _require_running(self) -> ExperimentSession:
        session = self._require_active()
        if session.status is not SessionStatus.RUNNING:
            raise RuntimeError("实验尚未开始")
        return session

    def _elapsed(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return round(time.monotonic() - self._started_monotonic, 6)

    @staticmethod
    def _workflow_steps(session: ExperimentSession) -> list[WorkflowStep]:
        return [WorkflowStep.from_dict(item)
                for item in session.template_snapshot.get("steps", [])]
