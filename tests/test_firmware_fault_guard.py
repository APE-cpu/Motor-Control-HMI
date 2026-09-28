"""固件栈护栏/自动复位（v2_fault_guard）在上位机侧的解析与提示。"""
import json

from communications.comm_manager import (
    FIRMWARE_STACK_BYTES, CommManager, decode_motor_fault_code, describe_firmware_fault,
)
from communications.protocol_v2 import MessageType, V2Frame


def test_system_reset_bit_and_fault_description():
    assert "上次异常复位" in decode_motor_fault_code(0x2000)
    assert "未知故障位" not in decode_motor_fault_code(0x2004)
    text = describe_firmware_fault(0x12, 0x08001234)
    assert "MemManage" in text and "栈溢出" in text and "0x08001234" in text
    assert "未记录" in describe_firmware_fault(0, 0)


def test_short_keys_parse_and_history_log_includes_fault_pc():
    comm = CommManager()
    logs = []
    comm.logMessage.connect(logs.append)
    payload = json.dumps({
        "fault_code": 0, "fault_history_code": 0x2000, "fault_text": "",
        "stk": 1480, "lft": 0x11, "lpc": 0x080093D0,
    }).encode("utf-8")
    frame = comm._parse_v2_telemetry(V2Frame(MessageType.TELEMETRY, command=0x30,
                                             payload=payload))
    assert (frame.stack_peak_bytes, frame.last_fault_type, frame.last_fault_pc) == \
        (1480, 0x11, 0x080093D0)
    comm._inspect_frame_fault(frame)
    history = [item for item in logs if "历史故障=0x2000" in item]
    assert len(history) == 1 and "HardFault" in history[0] and "0x080093D0" in history[0]
    assert any(f"1480/{FIRMWARE_STACK_BYTES}" in item for item in logs)


def test_stack_peak_logged_on_growth_and_warns_above_75_percent():
    comm = CommManager()
    logs = []
    comm.logMessage.connect(logs.append)
    for peak in (1400, 1500, 1700, 3200):
        comm._report_stack_peak(peak)
    assert len(logs) == 3                      # 1500 距上次不足 256 字节，不重复记
    assert logs[-1].startswith("[警告]") and logs[0].startswith("[诊断]")


def test_old_firmware_without_new_fields_still_parses():
    comm = CommManager()
    frame = comm._parse_v2_telemetry(V2Frame(
        MessageType.TELEMETRY, command=0x30,
        payload=json.dumps({"fault_code": 0, "fault_history_code": 0}).encode()))
    assert frame.stack_peak_bytes == 0 and frame.last_fault_type == 0
