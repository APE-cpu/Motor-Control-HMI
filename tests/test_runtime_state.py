import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import time

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import decode_frame
from config.config import CMD_RESET_FAULT, CMD_SET_PARAMS, CMD_START
from core import RuntimeState, RuntimeStateMachine, TransitionError
from pages.control_page import (
    ControlPage, current_filter_alpha_q15, current_filter_cutoff_hz,
    firmware_runtime_payload,
)
from pages.digital_twin_page import DigitalTwinPage
from pages.experiment_page import ExperimentPage


def _app():
    return QApplication.instance() or QApplication([])


def _make_ready(machine):
    machine.connection_changed(True, "测试设备已连接")
    machine.begin_precheck()
    machine.pass_precheck()


def test_完整正常运行状态路径():
    machine = RuntimeStateMachine()
    transitions = []
    machine.stateChanged.connect(
        lambda previous, current, reason: transitions.append((previous, current, reason)))

    _make_ready(machine)
    machine.confirm_started()
    machine.request_stop()
    machine.confirm_stopped()

    assert machine.state is RuntimeState.READY
    assert [item.current for item in machine.history] == [
        RuntimeState.CONNECTED, RuntimeState.PRECHECK, RuntimeState.READY,
        RuntimeState.RUNNING, RuntimeState.STOPPING, RuntimeState.READY,
    ]
    assert len(transitions) == 6


def test_设备离开RUN先进入STOPPING而非直接READY():
    machine = RuntimeStateMachine()
    _make_ready(machine)
    machine.confirm_started()

    machine.observe_device_stopping("MCSDK STOP")

    assert machine.state is RuntimeState.STOPPING
    machine.observe_device_stopped("MCSDK IDLE")
    assert machine.state is RuntimeState.READY


def test_非法转换被拒绝且状态不变():
    machine = RuntimeStateMachine()
    rejected = []
    machine.transitionRejected.connect(rejected.append)

    with pytest.raises(TransitionError, match="设备未就绪"):
        machine.confirm_started()
    assert machine.state is RuntimeState.DISCONNECTED
    assert "当前状态：disconnected" in rejected[0]


def test_START明确NACK撤销旧版本残留RUNNING():
    machine = RuntimeStateMachine()
    _make_ready(machine)
    machine.confirm_started("旧版本错误地提前确认")

    machine.reject_start("设备NACK启动")

    assert machine.state is RuntimeState.READY
    assert machine.history[-1].reason == "设备NACK启动"


def test_START在READY等待NACK时保持READY():
    machine = RuntimeStateMachine()
    _make_ready(machine)

    machine.reject_start("设备NACK启动")

    assert machine.state is RuntimeState.READY


def test_预检失败回到已连接状态():
    machine = RuntimeStateMachine()
    machine.connection_changed(True)
    machine.begin_precheck()
    machine.fail_precheck("传感器未就绪")

    assert machine.state is RuntimeState.CONNECTED
    assert machine.history[-1].reason == "传感器未就绪"


def test_运行中断线锁定且重连不会偷偷清故障():
    machine = RuntimeStateMachine()
    _make_ready(machine)
    machine.confirm_started()
    machine.connection_changed(False, "CAN 总线关闭")

    assert machine.state is RuntimeState.FAULT_LOCKED
    assert machine.is_connected is False
    machine.connection_changed(True, "CAN 已重连")
    assert machine.state is RuntimeState.FAULT_LOCKED
    machine.reset_fault()
    assert machine.state is RuntimeState.CONNECTED


def test_断线状态下故障复位回到未连接():
    machine = RuntimeStateMachine()
    machine.lock_fault("急停")
    machine.reset_fault()
    assert machine.state is RuntimeState.DISCONNECTED


def test_数字孪生页启动和停机必须经过状态机(tmp_path, monkeypatch):
    app = _app()
    comm = CommManager()
    machine = RuntimeStateMachine()
    _make_ready(machine)
    page = DigitalTwinPage(comm, machine)

    page._on_engine_state_changed("running")
    assert machine.state is RuntimeState.RUNNING
    machine.request_stop("测试停止")
    page._on_engine_state_changed("stopped")
    assert machine.state is RuntimeState.READY
    page.close()
    page.deleteLater()
    app.processEvents()


def test_停止命令不被非running界面状态拦截(monkeypatch):
    app = _app()
    comm = CommManager()
    sent = []
    monkeypatch.setattr(comm, "send_frame", lambda data: sent.append(data) or True)
    machine = RuntimeStateMachine()  # DISCONNECTED: 模拟界面状态已丢失
    page = ControlPage(comm, machine)

    page._on_stop()

    assert sent, "安全停机命令必须尝试发送"
    assert machine.state is RuntimeState.DISCONNECTED
    page.close()
    page.deleteLater()
    app.processEvents()


def test_启动前用已停机遥测修复残留RUNNING(monkeypatch):
    app = _app()
    comm = CommManager()
    frame = TelemetryFrame()
    frame.mc_state = 0
    frame.fault_code = 0
    frame.speed_target = 0.0
    frame.speed_actual = 0.0
    comm._latest_frame = frame
    comm._last_valid_at = time.monotonic()
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda _data: True)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    machine.confirm_started("test")
    page = ControlPage(comm, machine)

    page._on_start()

    assert machine.state is RuntimeState.RUNNING
    assert any("修复残留运行状态" in item.reason for item in machine.history)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_过期遥测不能修复残留RUNNING(monkeypatch):
    app = _app()
    comm = CommManager()
    frame = TelemetryFrame()
    frame.mc_state = 0
    frame.fault_code = 0
    frame.speed_target = 0.0
    frame.speed_actual = 0.0
    comm._latest_frame = frame
    comm._last_valid_at = 0.0
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda _data: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    machine.confirm_started("test")
    page = ControlPage(comm, machine)

    page._on_start()

    assert machine.state is RuntimeState.RUNNING
    assert not any("修复残留运行状态" in item.reason for item in machine.history)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_位置三环参数帧不超过固件旧拷贝缓冲():
    payload = firmware_runtime_payload({
        "control_mode": "position_closed",
        "target": 0.0,
        "max_rpm": 4000,
        "max_current_a": 1.887,
        "iq_max": 1.887,
        "kp_spd": 1752.0,
        "ki_spd": 121.0,
        "kp_cur": 2323.0,
        "ki_cur": 2077.0,
        "position_target_deg": 60.0,
        "kp_pos": 8.0,
        "kd_pos": 0.2,
        "kpf_pos": 0.0,
        "position_ff_lpf_hz": 8.0,
        "position_speed_limit_rpm": 300.0,
        "position_accel_limit_rpm_s": 60.0,
        "motor": "永磁同步电机(PMSM)",
        "mode": "位置三环控制",
        "kp": 1752.0,
        "sample_time": 0.002,
    })
    assert len(payload) < 384
    assert b"control_mode=position_closed" in payload
    assert b"position_target_deg=60" in payload
    assert b"kd_pos=0.2" in payload
    assert b"position_accel_limit_rpm_s=60" in payload
    assert b"ki_pos=" not in payload
    assert "永磁".encode("utf-8") not in payload
    assert b"sample_time=" not in payload


def test_电流反馈滤波频率与Q15系数可逆():
    alpha = current_filter_alpha_q15(2500.0)
    assert alpha == 20491
    assert current_filter_cutoff_hz(alpha) == pytest.approx(2500.0, abs=0.1)
    with pytest.raises(ValueError):
        current_filter_alpha_q15(500.0)


def test_控制页可单独下发旁路或IIR设置(monkeypatch):
    app = _app()
    comm = CommManager()
    sent = []
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda data: sent.append(data) or True)
    page = ControlPage(comm)

    assert page._current_filter_mode.currentData() is False
    assert page._current_filter_cutoff.isEnabled() is False
    page._current_filter_mode.setCurrentIndex(
        page._current_filter_mode.findData(True))
    page._current_filter_cutoff.setValue(2500)
    page._on_apply_current_filter()

    command, payload = decode_frame(sent[-1])
    assert command == CMD_SET_PARAMS
    assert b"current_filter_enabled=1" in payload
    assert b"current_filter_alpha_q15=20491" in payload
    assert "等待固件遥测回读" in page._current_filter_status.text()
    feedback = TelemetryFrame()
    feedback.data_source = "real"
    feedback.current_filter_enabled = True
    feedback.current_filter_alpha_q15 = 20491
    page._on_telemetry(feedback)
    assert "Id/Iq参与PI" in page._current_filter_status.text()
    assert "2500 Hz" in page._current_filter_status.text()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_控制页运行中拒绝改变电流反馈滤波(monkeypatch):
    app = _app()
    comm = CommManager()
    sent = []
    frame = TelemetryFrame()
    frame.mc_state = 6
    comm._latest_frame = frame
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda data: sent.append(data) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    page = ControlPage(comm)

    page._on_apply_current_filter()

    assert sent == []
    page.close()
    page.deleteLater()
    app.processEvents()


def test_控制页可下发并回读编码器速度FIFO(monkeypatch):
    app = _app()
    comm = CommManager()
    sent = []
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda data: sent.append(data) or True)
    page = ControlPage(comm)
    page._speed_fifo_depth.setCurrentIndex(
        page._speed_fifo_depth.findData(8))

    page._on_apply_speed_fifo()

    command, payload = decode_frame(sent[-1])
    assert command == CMD_SET_PARAMS
    assert b"speed_fifo_depth=8" in payload
    assert "等待固件遥测回读" in page._speed_fifo_status.text()
    feedback = TelemetryFrame()
    feedback.data_source = "real"
    feedback.speed_fifo_depth = 8
    page._on_telemetry(feedback)
    assert "8点平均" in page._speed_fifo_status.text()
    assert "7 ms群延迟" in page._speed_fifo_status.text()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_位置三环真实v2启动只发START不夹带遥测配置(monkeypatch):
    app = _app()
    comm = CommManager()
    sent = []
    pending = {"count": 0}
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(
        comm, "protocol_status",
        lambda: {"mode": "negotiated-v2", "pending_ack": pending["count"]})

    def send(data):
        sent.append(data)
        pending["count"] += 1
        return False

    monkeypatch.setattr(comm, "send_frame", send)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    page = ControlPage(comm, machine)
    page._select_mode("位置三环控制")
    page._target_position.setValue(60.0)

    page._on_start()

    commands = [decode_frame(data)[0] for data in sent]
    assert commands == [CMD_START]
    start_payload = decode_frame(sent[-1])[1]
    assert b"position_closed" in start_payload
    assert b"position_target_deg=60" in start_payload
    page.close()
    page.deleteLater()
    app.processEvents()


def test_真实v2启动等待ACK期间忽略重复点击(monkeypatch):
    app = _app()
    comm = CommManager()
    calls = []
    pending = {"count": 0}
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(
        comm, "protocol_status",
        lambda: {"mode": "negotiated-v2", "pending_ack": pending["count"]})

    def send(_data):
        calls.append(_data)
        pending["count"] += 1
        return False  # negotiated-v2的正常异步提交语义

    monkeypatch.setattr(comm, "send_frame", send)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    page = ControlPage(comm, machine)

    page._on_start()
    assert machine.state is RuntimeState.READY
    page._on_start()

    assert len(calls) == 1
    assert page._start_command_pending is True
    page.close()
    page.deleteLater()
    app.processEvents()


def test_READY时下位机FAULT_OVER不拦截直接发送启动(monkeypatch):
    """固件START自带FAULT_OVER确认，上位机不再先发复位、不再多一次点击。"""
    app = _app()
    comm = CommManager()
    sent = []
    frame = TelemetryFrame()
    frame.mc_state = 11
    comm._latest_frame = frame
    monkeypatch.setattr(comm, "is_connected", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda data: sent.append(data) or True)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    page = ControlPage(comm, machine)

    page._on_start()

    commands = [decode_frame(data)[0] for data in sent]
    assert CMD_RESET_FAULT not in commands
    assert commands[-1] == CMD_START
    page.close()
    page.deleteLater()
    app.processEvents()


def test_实验页软件预检通过后进入READY(tmp_path, monkeypatch):
    app = _app()
    comm = CommManager()
    monkeypatch.setattr(comm, "is_sim_running", lambda: True)
    machine = RuntimeStateMachine()
    machine.connection_changed(True, "数字孪生已连接")
    snapshot = {
        "device": {"name": "78W PMSM", "sensors": ["QEP"],
                   "extra": {"max_rpm": 3000}},
        "controller_params": {"control_mode": "闭环PI控制",
                              "target_speed_rpm": 1500},
    }
    page = ExperimentPage(
        comm, storage_root=tmp_path / "records",
        snapshot_provider=lambda: snapshot, runtime_state=machine)
    page._source.setCurrentIndex(page._source.findData("sim"))
    page._template_combo.setCurrentIndex(-1)
    page._equipment_combo.setCurrentIndex(-1)

    page._run_precheck()

    assert machine.state is RuntimeState.READY
    assert "预检通过" in page._precheck_detail.text()
    assert page._btn_precheck.isEnabled() is False
    page.shutdown()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_实验页预检发现数据源不匹配(tmp_path, monkeypatch):
    app = _app()
    comm = CommManager()
    monkeypatch.setattr(comm, "is_sim_running", lambda: True)
    monkeypatch.setattr(comm, "is_connected", lambda: False)
    machine = RuntimeStateMachine()
    machine.connection_changed(True, "数字孪生已连接")
    snapshot = {
        "device": {"name": "PMSM", "sensors": ["QEP"],
                   "extra": {"max_rpm": 3000}},
        "controller_params": {"control_mode": "闭环PI控制",
                              "target_speed_rpm": 1500},
    }
    page = ExperimentPage(
        comm, storage_root=tmp_path / "records",
        snapshot_provider=lambda: snapshot, runtime_state=machine)
    # 页面现在默认选择真实设备，但实际连接的是数字孪生。
    page._run_precheck()

    assert machine.state is RuntimeState.CONNECTED
    assert "真实设备" in page._precheck_detail.text()
    page.shutdown()
    page.close()
    page.deleteLater()
    app.processEvents()
