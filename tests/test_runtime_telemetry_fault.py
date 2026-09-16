import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

from PySide6.QtWidgets import QApplication

from communications.comm_manager import TelemetryFrame
from communications.protocol_session import CommandResult
from communications.protocol_v2 import MessageType, V2Frame
from config.config import CMD_STOP
from core import RuntimeState, RuntimeStateMachine
from main_window import MainWindow


def _app():
    return QApplication.instance() or QApplication([])


def test_保护故障码立即离开RUNNING():
    _app()
    # Avoid full MainWindow UI construction cost where possible — still need wiring.
    win = MainWindow(enable_training=False)
    rs = win.runtime_state
    rs.connection_changed(True, "t")
    rs.begin_precheck()
    rs.pass_precheck()
    rs.confirm_started("t")
    assert rs.state is RuntimeState.RUNNING

    frame = TelemetryFrame()
    frame.mc_state = 6  # still RUN briefly after latch
    frame.fault_code = 0x8000
    frame.fault_text = "V2 runaway/reverse-speed trip"
    frame.speed_actual = -200.0
    frame.speed_target = 1000.0
    win._device_run_seen = True
    win._on_runtime_telemetry(frame)

    assert rs.state is RuntimeState.FAULT_LOCKED
    assert "runaway" in rs.history[-1].reason.lower() or "0x8000" in rs.history[-1].reason or "V2" in rs.history[-1].reason


def _running_window():
    win = MainWindow(enable_training=False)
    rs = win.runtime_state
    rs.connection_changed(True, "t")
    rs.begin_precheck()
    rs.pass_precheck()
    rs.confirm_started("t")
    win._device_run_seen = True
    return win, rs


def test_STOP遥测只进入STOPPING直到IDLE连续确认():
    _app()
    win, rs = _running_window()
    stopping = TelemetryFrame()
    stopping.mc_state = 8
    stopping.speed_actual = 0.0
    stopping.speed_target = 0.0
    stopping.stop_reason = 3
    stopping.stop_command = 0x10
    stopping.stop_rx_age_ms = 15001
    stopping.stop_run_ms = 18420

    win._on_runtime_telemetry(stopping)

    assert rs.state is RuntimeState.STOPPING
    assert "15秒看门狗" in rs.history[-1].reason
    assert "15001 ms" in rs.history[-1].reason

    idle = TelemetryFrame()
    idle.mc_state = 0
    idle.speed_actual = 0.0
    idle.speed_target = 0.0
    idle.stop_reason = 3
    for _ in range(2):
        win._on_runtime_telemetry(idle)
        assert rs.state is RuntimeState.STOPPING
    win._on_runtime_telemetry(idle)
    assert rs.state is RuntimeState.READY
    win.close()


def test_STOP_ACK只确认命令接受不提前READY():
    _app()
    win, rs = _running_window()

    win._on_v2_command_result(CommandResult(
        sequence=31, command=CMD_STOP, success=True))

    assert rs.state is RuntimeState.STOPPING
    assert "ACK接受停机" in rs.history[-1].reason
    win.close()


def test_停机诊断字段通过JSON解析保留():
    win = MainWindow(enable_training=False)
    payload = json.dumps({
        "mc_state": 8,
        "stop_reason": 8,
        "stop_command": 0x20,
        "stop_rx_age_ms": 73,
        "stop_run_ms": 5421,
    }).encode("utf-8")

    parsed = win.comm_manager._parse_v2_telemetry(V2Frame(
        MessageType.TELEMETRY, command=0x30, payload=payload))

    assert parsed.stop_reason == 8
    assert parsed.stop_command == 0x20
    assert parsed.stop_rx_age_ms == 73
    assert parsed.stop_run_ms == 5421
    win.close()
