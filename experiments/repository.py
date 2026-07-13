"""基于目录的实验持久化仓库。"""
from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import ExperimentSession


TELEMETRY_FIELDS = (
    "timestamp",
    "monotonic_s",
    "speed_actual",
    "speed_target",
    "current_actual",
    "current_target",
    "torque_actual",
    "torque_target",
    "angle_actual",
    "temperature",
    "vdc",
    "bus_state",
    "sensor_source",
    "sensor_quality",
    "convergence",
    "low_speed_warn",
    "fault_code",
    "fault_text",
    "data_source",
)


class ExperimentRepository:
    """保存和读取实验文件；所有写操作在进程内串行化。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def allocate_id(self, now: datetime | None = None) -> str:
        """按日期分配可读且不重复的 EXP-YYYYMMDD-NNN 编号。"""
        current = now or datetime.now()
        day = current.strftime("%Y%m%d")
        year_dir = self.root / current.strftime("%Y")
        year_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"EXP-{day}-"
        with self._lock:
            numbers = []
            for path in year_dir.glob(f"{prefix}*"):
                try:
                    numbers.append(int(path.name.removeprefix(prefix)))
                except ValueError:
                    continue
            return f"{prefix}{max(numbers, default=0) + 1:03d}"

    def session_dir(self, experiment_id: str) -> Path:
        self._validate_id(experiment_id)
        try:
            year = experiment_id.split("-")[1][:4]
        except IndexError as exc:  # pragma: no cover - protected by validation
            raise ValueError("无效实验编号") from exc
        return self.root / year / experiment_id

    def create(self, session: ExperimentSession) -> Path:
        directory = self.session_dir(session.experiment_id)
        with self._lock:
            try:
                directory.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise ValueError(f"实验编号已存在: {session.experiment_id}") from exc
            (directory / "screenshots").mkdir()
            (directory / "report").mkdir()
            self.save(session)
            self._write_json(directory / "device_snapshot.json",
                             session.device.to_dict() if session.device else {})
            self._write_json(directory / "controller_params.json", session.controller_params)
            self._write_json(directory / "protection_params.json", session.protection_params)
            self._write_json(directory / "template_snapshot.json",
                             session.template_snapshot)
        return directory

    def save(self, session: ExperimentSession) -> None:
        with self._lock:
            self._write_json(self.session_dir(session.experiment_id) / "experiment.json",
                             session.to_dict())

    def load(self, experiment_id: str) -> ExperimentSession:
        path = self.session_dir(experiment_id) / "experiment.json"
        with open(path, "r", encoding="utf-8") as stream:
            return ExperimentSession.from_dict(json.load(stream))

    def list_sessions(self, limit: int | None = None) -> list[ExperimentSession]:
        """按创建时间倒序列出有效实验；单个损坏目录不会阻断其它记录。"""
        sessions = []
        with self._lock:
            metadata_files = list(self.root.glob("*/EXP-*/experiment.json"))
        for path in metadata_files:
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    sessions.append(ExperimentSession.from_dict(json.load(stream)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError, KeyError):
                continue
        sessions.sort(key=lambda item: (item.created_at, item.experiment_id),
                      reverse=True)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit 不能为负数")
            return sessions[:limit]
        return sessions

    def append_event(self, experiment_id: str, event: dict[str, Any]) -> None:
        path = self.session_dir(experiment_id) / "events.jsonl"
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock, open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def read_events(self, experiment_id: str) -> list[dict[str, Any]]:
        """读取结构化事件；损坏的单行被隔离，不影响整份实验报告。"""
        path = self.session_dir(experiment_id) / "events.jsonl"
        if not path.exists():
            return []
        events = []
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except json.JSONDecodeError:
                    continue
        return events

    def append_telemetry(self, experiment_id: str,
                         rows: Iterable[dict[str, Any]]) -> int:
        path = self.session_dir(experiment_id) / "telemetry.csv"
        count = 0
        with self._lock:
            new_file = not path.exists() or path.stat().st_size == 0
            with open(path, "a", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=TELEMETRY_FIELDS,
                                        extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in TELEMETRY_FIELDS})
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
        return count

    def read_telemetry(self, experiment_id: str,
                       max_points: int | None = None) -> list[dict[str, Any]]:
        """读取历史遥测并转换数值；坏单元格置空，结构损坏的行跳过。"""
        if max_points is not None and max_points < 2:
            raise ValueError("max_points 必须至少为 2")
        path = self.session_dir(experiment_id) / "telemetry.csv"
        if not path.exists():
            return []
        numeric = set(TELEMETRY_FIELDS) - {
            "timestamp", "bus_state", "sensor_source", "low_speed_warn",
            "fault_text", "data_source",
        }
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                return []
            for raw in reader:
                if None in raw:  # 列数超过表头，无法可靠对应字段
                    continue
                row: dict[str, Any] = {}
                for key in TELEMETRY_FIELDS:
                    value = raw.get(key, "")
                    if key in numeric:
                        try:
                            row[key] = float(value) if value != "" else None
                        except (TypeError, ValueError):
                            row[key] = None
                    elif key == "low_speed_warn":
                        row[key] = str(value).lower() in {"1", "true", "yes"}
                    else:
                        row[key] = value
                rows.append(row)
        if max_points is not None and len(rows) > max_points:
            last = len(rows) - 1
            indices = [round(i * last / (max_points - 1)) for i in range(max_points)]
            rows = [rows[index] for index in indices]
        return rows

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    @staticmethod
    def _validate_id(experiment_id: str) -> None:
        parts = experiment_id.split("-")
        if (len(parts) != 3 or parts[0] != "EXP" or
                len(parts[1]) != 8 or not parts[1].isdigit() or
                len(parts[2]) != 3 or not parts[2].isdigit()):
            raise ValueError(f"无效实验编号: {experiment_id}")
