"""版本化设备组合档案仓库。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import EquipmentProfile


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def default_78w_equipment() -> EquipmentProfile:
    return EquipmentProfile(
        profile_id="DEV-BUILTIN-78W-R001",
        family_id="DEV-BUILTIN-78W",
        revision=1,
        name="野火骄阳 F407 + 78W PMSM 实验平台",
        motor_type="PMSM",
        rated_power_w=78.0,
        nominal_bus_voltage_v=24.0,
        inverter="野火电机驱动板（电机接口1）",
        controller="野火 STM32F407 骄阳开发板",
        sensors=["增量式编码器(QEP接口1)", "驱动板NTC温度传感器",
                 "母线电压采样", "相电流采样"],
        expected_device_id="EBF-F407-JIAOYANG-PMSM-001",
        safety_limits={
            "max_rpm": 4000.0, "max_bus_voltage_v": 30.0,
            "max_current_a": 4.49, "max_temperature_c": 80.0,
        },
        notes="实验室默认真机组合：24 V母线、驱动板电机接口1、QEP接口1。",
        created_at=_iso_now(),
        built_in=True,
    )


class EquipmentProfileRepository:
    """每个修订一个JSON文件；旧修订不会被新保存覆盖。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_profiles(self) -> list[EquipmentProfile]:
        profiles = [default_78w_equipment()]
        for path in self.root.glob("DEV-*-R*.json"):
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    profile = EquipmentProfile.from_dict(json.load(stream))
                if not profile.built_in:
                    profiles.append(profile)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        profiles[1:] = sorted(
            profiles[1:], key=lambda item: (item.name, -item.revision))
        return profiles

    def load(self, profile_id: str) -> EquipmentProfile:
        if profile_id == "DEV-BUILTIN-78W-R001":
            return default_78w_equipment()
        self._validate_profile_id(profile_id)
        with open(self.root / f"{profile_id}.json", "r", encoding="utf-8") as stream:
            return EquipmentProfile.from_dict(json.load(stream))

    def create_revision(self, name: str, *, family_id: str = "", **values) -> EquipmentProfile:
        if not name.strip():
            raise ValueError("设备档案名称不能为空")
        family = family_id.strip() or f"DEV-{uuid4().hex[:10].upper()}"
        self._validate_family_id(family)
        revisions = [item.revision for item in self.list_profiles()
                     if item.family_id == family]
        revision = max(revisions, default=0) + 1
        profile = EquipmentProfile(
            profile_id=f"{family}-R{revision:03d}", family_id=family,
            revision=revision, name=name.strip(), created_at=_iso_now(), **values)
        self._write(profile)
        return profile

    def delete(self, profile_id: str) -> None:
        if profile_id == "DEV-BUILTIN-78W-R001":
            raise ValueError("内置设备档案不能删除")
        self._validate_profile_id(profile_id)
        (self.root / f"{profile_id}.json").unlink()

    def _write(self, profile: EquipmentProfile) -> None:
        path = self.root / f"{profile.profile_id}.json"
        if path.exists():
            raise ValueError("设备档案修订已存在，不能覆盖")
        temp = path.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(profile.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    @staticmethod
    def _validate_family_id(family_id: str) -> None:
        if not re.fullmatch(r"DEV-[A-Z0-9-]{6,48}", family_id):
            raise ValueError(f"无效设备系列编号: {family_id}")

    @classmethod
    def _validate_profile_id(cls, profile_id: str) -> None:
        match = re.fullmatch(r"(DEV-[A-Z0-9-]{6,48})-R\d{3}", profile_id)
        if not match:
            raise ValueError(f"无效设备档案编号: {profile_id}")
        cls._validate_family_id(match.group(1))
