import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import decode_frame
from config.config import CMD_RESET_FAULT
from core import RuntimeState, RuntimeStateMachine, TransitionError
from pages.control_page import ControlPage
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


def test_非法转换被拒绝且状态不变():
    machine = RuntimeStateMachine()
    rejected = []
    machine.transitionRejected.connect(rejected.append)

    with pytest.raises(TransitionError, match="设备未就绪"):
        machine.confirm_started()
    assert machine.state is RuntimeState.DISCONNECTED
    assert "当前状态：disconnected" in rejected[0]


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


def test_控制页启动和停机必须经过状态机(tmp_path, monkeypatch):
    app = _app()
    comm = CommManager()
    monkeypatch.setattr(comm, "is_sim_running", lambda: True)
    monkeypatch.setattr(comm, "send_frame", lambda _data: True)
    machine = RuntimeStateMachine()
    _make_ready(machine)
    page = ControlPage(comm, machine)

    page._on_start()
    assert machine.state is RuntimeState.RUNNING
    page._on_stop()
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


def test_READY时下位机故障状态会发送复位而不是启动(monkeypatch):
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

    assert decode_frame(sent[-1])[0] == CMD_RESET_FAULT
    assert machine.state is RuntimeState.READY
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
