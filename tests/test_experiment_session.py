import csv
import json

import pytest

from communications.comm_manager import CommManager, TelemetryFrame
from experiments import (
    DeviceProfile,
    ExperimentSessionManager,
    SessionStatus,
)


def _manager(tmp_path):
    return ExperimentSessionManager(tmp_path / "experiments")


def _create(manager):
    return manager.create_session(
        "转速阶跃实验",
        purpose="验证转速跟踪性能",
        operator="tester",
        software_version="1.5.0",
        device=DeviceProfile(
            name="78W PMSM",
            rated_power_w=78.0,
            dc_bus_voltage_v=48.0,
            sensors=["QEP", "Hall"],
        ),
        controller_params={"mode": "PI", "kp": 1.0, "ki": 0.1},
        protection_params={"over_current_a": 5.0},
    )


def test_创建实验生成完整目录和快照(tmp_path):
    manager = _manager(tmp_path)
    session = _create(manager)
    directory = manager.repository.session_dir(session.experiment_id)

    assert session.experiment_id.startswith("EXP-")
    assert session.status is SessionStatus.CREATED
    assert (directory / "experiment.json").is_file()
    assert (directory / "device_snapshot.json").is_file()
    assert (directory / "controller_params.json").is_file()
    assert (directory / "protection_params.json").is_file()
    assert (directory / "screenshots").is_dir()
    assert (directory / "report").is_dir()

    device = json.loads((directory / "device_snapshot.json").read_text("utf-8"))
    assert device["name"] == "78W PMSM"
    assert device["rated_power_w"] == 78.0


def test_同一天实验编号递增且不重复(tmp_path):
    first_manager = _manager(tmp_path)
    first = _create(first_manager)
    # 新管理器模拟应用重启，编号仍从磁盘现状继续分配。
    second_manager = _manager(tmp_path)
    second = _create(second_manager)

    assert first.experiment_id.endswith("-001")
    assert second.experiment_id.endswith("-002")


def test_记录遥测和事件后正常结束并可重载(tmp_path):
    manager = _manager(tmp_path)
    session = _create(manager)
    manager.start()

    frame = TelemetryFrame()
    frame.speed_actual = 1498.0
    frame.speed_target = 1500.0
    frame.current_actual = 1.25
    frame.vdc = 47.8
    frame.fault_code = 0x02
    frame.fault_text = "测试故障位"
    frame.data_source = "sim"
    manager.record_telemetry(frame, timestamp="2026-07-12T12:00:00+08:00")
    manager.record_event("target_changed", "目标转速调整", {"rpm": 1500})
    completed = manager.complete()

    assert completed.status is SessionStatus.COMPLETED
    assert completed.telemetry_count == 1
    assert completed.event_count == 3  # 开始、自定义事件、结束
    assert completed.ended_at is not None
    assert manager.active_session is None

    loaded = manager.load(session.experiment_id)
    assert loaded == completed
    directory = manager.repository.session_dir(session.experiment_id)
    with open(directory / "telemetry.csv", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert float(rows[0]["speed_actual"]) == 1498.0
    assert rows[0]["data_source"] == "sim"
    assert float(rows[0]["fault_code"]) == 2
    assert rows[0]["fault_text"] == "测试故障位"

    events = [json.loads(line) for line in
              (directory / "events.jsonl").read_text("utf-8").splitlines()]
    assert [event["type"] for event in events] == [
        "session_started", "target_changed", "session_completed"
    ]


def test_异常中止保留已记录数据(tmp_path):
    manager = _manager(tmp_path)
    session = _create(manager)
    manager.start()
    manager.record_telemetry({"speed_actual": 800, "data_source": "sim"})
    aborted = manager.abort("通信超时")

    assert aborted.status is SessionStatus.ABORTED
    assert aborted.end_reason == "通信超时"
    directory = manager.repository.session_dir(session.experiment_id)
    assert (directory / "telemetry.csv").stat().st_size > 0
    assert "session_aborted" in (directory / "events.jsonl").read_text("utf-8")


def test_不允许同时创建两个活动实验(tmp_path):
    manager = _manager(tmp_path)
    _create(manager)

    with pytest.raises(RuntimeError, match="已有实验"):
        manager.create_session("另一个实验")


def test_非法生命周期操作被拒绝(tmp_path):
    manager = _manager(tmp_path)
    _create(manager)

    with pytest.raises(RuntimeError, match="尚未开始"):
        manager.complete()
    with pytest.raises(RuntimeError, match="尚未开始"):
        manager.record_telemetry({"speed_actual": 0})

    manager.start()
    with pytest.raises(RuntimeError, match="当前状态不能开始"):
        manager.start()
    with pytest.raises(ValueError, match="必须提供原因"):
        manager.abort("  ")


def test_实验名称和编号路径校验(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="名称不能为空"):
        manager.create_session("  ")
    with pytest.raises(ValueError, match="无效实验编号"):
        manager.load("../../outside")


def test_MotorSim遥测可以完成最小实验闭环(tmp_path):
    experiment = _manager(tmp_path)
    session = _create(experiment)
    experiment.start()

    comm = CommManager()
    comm._motor_sim.start(1200.0)
    for _ in range(3):
        experiment.record_telemetry(comm._make_simulated_frame())
    experiment.complete("数字孪生冒烟实验完成")

    loaded = experiment.load(session.experiment_id)
    assert loaded.status is SessionStatus.COMPLETED
    assert loaded.telemetry_count == 3
    telemetry = experiment.repository.session_dir(session.experiment_id) / "telemetry.csv"
    assert len(telemetry.read_text("utf-8-sig").splitlines()) == 4


def test_历史实验倒序枚举且隔离损坏记录(tmp_path):
    first_manager = _manager(tmp_path)
    first = _create(first_manager)
    first_manager.start()
    first_manager.complete()

    second_manager = _manager(tmp_path)
    second = _create(second_manager)
    second_manager.start()
    second_manager.abort("测试中止")

    broken = tmp_path / "experiments" / "2026" / "EXP-20260101-999"
    broken.mkdir(parents=True)
    (broken / "experiment.json").write_text("{broken", encoding="utf-8")

    sessions = second_manager.repository.list_sessions()
    assert [item.experiment_id for item in sessions] == [
        second.experiment_id, first.experiment_id
    ]
    assert second_manager.repository.list_sessions(limit=1)[0].experiment_id == \
        second.experiment_id
    with pytest.raises(ValueError, match="不能为负数"):
        second_manager.repository.list_sessions(limit=-1)
