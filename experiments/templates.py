"""实验方案模板仓库与内置工作流。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import ExperimentTemplate, WorkflowStep


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def default_78w_template() -> ExperimentTemplate:
    return ExperimentTemplate(
        template_id="TPL-BUILTIN-78W-BASELINE",
        name="野火 78W PMSM 真机基础运行实验",
        purpose="验证真机通信、1000 rpm转速跟踪、电流波动、停机过程和数据归档完整性",
        data_source="real",
        device_defaults={
            "name": "78W PMSM", "motor_type": "PMSM",
            "rated_power_w": 78.0, "dc_bus_voltage_v": 24.0,
        },
        safety_limits={
            "max_rpm": 4000.0, "max_bus_voltage_v": 30.0,
            "max_current_a": 4.49, "max_temperature_c": 80.0,
        },
        steps=[
            WorkflowStep("S01", "核对实验配置",
                         "核对设备、数据源、控制方式、目标值和保护参数。",
                         "配置与本次实验目的一致"),
            WorkflowStep("S02", "确认接线与安全状态",
                         "确认急停可用、24 V母线极性正确，电机接驱动板接口1，QEP接接口1。",
                         "真机接线和供电状态适合本次实验"),
            WorkflowStep("S03", "执行运行预检",
                         "在设备运行状态机区域执行预检并处理全部失败项。",
                         "状态机进入READY", True, "ready"),
            WorkflowStep("S04", "执行实验动作",
                         "按照实验目的启动、调整目标或施加计划内扰动。",
                         "设备响应符合预期且没有保护触发", True, "running"),
            WorkflowStep("S05", "观察并标记现象",
                         "观察转速、电流、母线、温度和异常现象，填写步骤备注。",
                         "关键现象已记录"),
            WorkflowStep("S06", "正常停机并确认安全",
                         "执行正常停机，确认运行状态退出且关键量回落。",
                         "设备已安全停止", True, "ready"),
            WorkflowStep("S07", "补充实验结论",
                         "在备注中写明是否达到目的、异常点和下一步。",
                         "结论信息足以支持后续复盘"),
        ],
        updated_at=_iso_now(),
        built_in=True,
    )


class ExperimentTemplateRepository:
    """单文件单模板存储；内置模板始终可用且不能删除。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_templates(self) -> list[ExperimentTemplate]:
        templates = [default_78w_template()]
        for path in self.root.glob("TPL-*.json"):
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    template = ExperimentTemplate.from_dict(json.load(stream))
                if not template.built_in:
                    templates.append(template)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        templates[1:] = sorted(templates[1:], key=lambda item: item.name)
        return templates

    def load(self, template_id: str) -> ExperimentTemplate:
        if template_id == "TPL-BUILTIN-78W-BASELINE":
            return default_78w_template()
        self._validate_id(template_id)
        with open(self.root / f"{template_id}.json", "r", encoding="utf-8") as stream:
            return ExperimentTemplate.from_dict(json.load(stream))

    def save(self, template: ExperimentTemplate) -> ExperimentTemplate:
        if template.built_in:
            raise ValueError("内置模板不能覆盖")
        if not template.template_id:
            template.template_id = f"TPL-{uuid4().hex[:12].upper()}"
        self._validate_id(template.template_id)
        template.updated_at = _iso_now()
        template.version = max(1, int(template.version))
        path = self.root / f"{template.template_id}.json"
        temp = path.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(template.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return template

    def create(self, name: str, **kwargs) -> ExperimentTemplate:
        template = ExperimentTemplate(
            template_id=f"TPL-{uuid4().hex[:12].upper()}", name=name, **kwargs)
        return self.save(template)

    def delete(self, template_id: str) -> None:
        if template_id == "TPL-BUILTIN-78W-BASELINE":
            raise ValueError("内置模板不能删除")
        self._validate_id(template_id)
        (self.root / f"{template_id}.json").unlink()

    @staticmethod
    def _validate_id(template_id: str) -> None:
        if not re.fullmatch(r"TPL-[A-Z0-9-]{6,64}", template_id):
            raise ValueError(f"无效模板编号: {template_id}")
