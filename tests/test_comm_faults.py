import json
import struct

from communications.comm_manager import (
    CommManager, TelemetryFrame, decode_motor_fault_code,
)
from communications.protocol_v2 import MessageType, V2Frame
from config.config import (
    TELEM_FLAG_DRIVER_FAULT, TELEM_FLAG_EMERGENCY_FAULT,
    TELEM_FLAG_LOW_SPEED_WARN, TELEM_FLAG_OVERCURRENT_FAULT, TELEM_FMT,
)
from core import RuntimeState, RuntimeStateMachine


class _OpenDriver:
    def is_open(self):
        return True


def _running_machine():
    machine = RuntimeStateMachine()
    machine.connection_changed(True)
    machine.begin_precheck()
    machine.pass_precheck()
    machine.confirm_started()
    return machine


def test_下位机故障位解析为可读原因():
    comm = CommManager()
    flags = (TELEM_FLAG_LOW_SPEED_WARN | TELEM_FLAG_OVERCURRENT_FAULT |
             TELEM_FLAG_DRIVER_FAULT | TELEM_FLAG_EMERGENCY_FAULT)
    payload = struct.pack(TELEM_FMT, 1000, 1200, 250, 10, 9000, 20,
                          255, 255, flags)

    frame = comm._parse_telemetry_payload(payload)

    assert frame.low_speed_warn is True
    assert frame.fault_code == 0x0E
    assert "过流" in frame.fault_text
    assert "驱动器" in frame.fault_text
    assert "急停" in frame.fault_text


def test_母线和设备故障只上报一次():
    comm = CommManager()
    faults = []
    comm.faultDetected.connect(faults.append)
    frame = TelemetryFrame()
    frame.bus_state = "ov"
    frame.fault_code = TELEM_FLAG_OVERCURRENT_FAULT
    frame.fault_text = "下位机过流保护"

    comm._inspect_frame_fault(frame)
    comm._inspect_frame_fault(frame)

    assert faults == ["直流母线过压跳闸", "下位机过流保护"]


def test_故障信号端到端锁定运行状态():
    comm = CommManager()
    machine = _running_machine()
    comm.faultDetected.connect(machine.lock_fault)

    frame = TelemetryFrame()
    frame.fault_code = TELEM_FLAG_DRIVER_FAULT
    frame.fault_text = "栅极驱动器故障"
    comm._inspect_frame_fault(frame)

    assert machine.state is RuntimeState.FAULT_LOCKED
    assert machine.history[-1].reason == "栅极驱动器故障"


def test_数字孪生母线过压端到端锁定():
    comm = CommManager()
    machine = _running_machine()
    comm.faultDetected.connect(machine.lock_fault)
    frame = TelemetryFrame()
    frame.data_source = "sim"
    frame.bus_state = "ov"

    comm._inspect_frame_fault(frame)

    assert machine.state is RuntimeState.FAULT_LOCKED
    assert machine.history[-1].reason == "直流母线过压跳闸"


def test_连续三次读取异常触发通信故障(monkeypatch):
    comm = CommManager()
    comm._driver = _OpenDriver()
    comm._kind = "RS-485"
    faults = []
    comm.faultDetected.connect(faults.append)

    def fail_read():
        raise OSError("串口校验失败")

    calls = 0

    def stop_after_four(_seconds):
        nonlocal calls
        calls += 1
        if calls >= 4:
            comm._stop.set()

    monkeypatch.setattr(comm, "_read_real_frame", fail_read)
    monkeypatch.setattr("communications.comm_manager.time.sleep", stop_after_four)
    comm._stop.clear()
    comm._poll_loop()

    assert len(faults) == 1
    assert "连续读取异常" in faults[0]


def test_真实设备遥测超时只告警不锁定故障(monkeypatch):
    comm = CommManager()
    comm._driver = _OpenDriver()
    comm._kind = "CAN总线"
    comm._last_valid_at = 0.0
    faults = []
    logs = []
    comm.faultDetected.connect(faults.append)
    comm.logMessage.connect(logs.append)
    monkeypatch.setattr(comm, "_read_real_frame", lambda: None)
    monkeypatch.setattr("communications.comm_manager.time.monotonic", lambda: 3.0)
    monkeypatch.setattr(
        "communications.comm_manager.time.sleep", lambda _seconds: comm._stop.set())
    comm._stop.clear()

    comm._poll_loop()

    assert faults == []
    assert any("遥测连续 2 秒超时" in item for item in logs)


def test_MCSDK故障位解码包含具体名称和未知位():
    text = decode_motor_fault_code(0x0024 | 0x0100)

    assert "母线欠压" in text
    assert "速度反馈故障" in text
    assert "未知故障位0x0100" in text


def test_v2历史故障锁存解析但不触发当前故障锁定():
    comm = CommManager()
    faults = []
    logs = []
    comm.faultDetected.connect(faults.append)
    comm.logMessage.connect(logs.append)
    payload = json.dumps({
        "fault_code": 0,
        "fault_history_code": 0x0040,
        "fault_text": "",
    }).encode("utf-8")

    frame = comm._parse_v2_telemetry(V2Frame(
        MessageType.TELEMETRY, command=0x30, payload=payload))
    comm._inspect_frame_fault(frame)
    comm._inspect_frame_fault(frame)

    assert frame.fault_history_code == 0x0040
    assert "硬件过流" in frame.fault_history_text
    assert faults == []
    assert len([item for item in logs if "历史故障=0x0040" in item]) == 1


def test_v2通用MCSDK文本会被具体故障码替换():
    comm = CommManager()
    payload = json.dumps({
        "fault_code": 0x0010,
        "fault_history_code": 0x0010,
        "fault_text": "MCSDK fault active",
    }).encode("utf-8")

    frame = comm._parse_v2_telemetry(V2Frame(
        MessageType.TELEMETRY, command=0x30, payload=payload))

    assert "启动失败" in frame.fault_text
