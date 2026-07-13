import json

from communications.protocol_session import ProtocolSession, ProtocolSessionState
from communications.protocol_v2 import (
    MessageType, ProtocolV2Error, V2StreamDecoder, decode_v2_frame, encode_v2_frame,
)
from communications.v2_virtual_device import V2VirtualDevice, VirtualDeviceState
from config.config import CMD_EMERGENCY_STOP, CMD_RESET_FAULT, CMD_START, CMD_STOP


def _handshake(session=None, device=None):
    session = session or ProtocolSession()
    device = device or V2VirtualDevice()
    hello = encode_v2_frame(session.build_hello())
    responses = device.receive_bytes(hello)
    assert len(responses) == 1
    capabilities = session.handle_frame(decode_v2_frame(responses[0]))
    return session, device, capabilities


def _exchange(session, device, command, payload=b"", now=0.0):
    request = session.build_command(command, payload)
    responses = device.receive_bytes(encode_v2_frame(request), now)
    results = [session.handle_frame(decode_v2_frame(data)) for data in responses]
    return request, responses, results


def test_正常握手启动停止完整字节链路():
    session, device, capabilities = _handshake()
    assert session.state is ProtocolSessionState.READY
    assert device.state is VirtualDeviceState.READY
    assert capabilities.device_id == "VIRTUAL-MOTOR-001"

    _, _, started = _exchange(session, device, CMD_START, b"target=1500")
    assert started[0].success is True
    assert device.state is VirtualDeviceState.RUNNING
    _, _, stopped = _exchange(session, device, CMD_STOP)
    assert stopped[0].success is True
    assert device.state is VirtualDeviceState.READY


def test_HELLO支持任意分包():
    session = ProtocolSession()
    device = V2VirtualDevice()
    raw = encode_v2_frame(session.build_hello())

    assert device.receive_bytes(raw[:3]) == []
    assert device.receive_bytes(raw[3:9]) == []
    responses = device.receive_bytes(raw[9:])

    session.handle_frame(decode_v2_frame(responses[0]))
    assert session.state is ProtocolSessionState.READY


def test_强制NACK返回结构化拒绝原因():
    session, device, _ = _handshake()
    device.nack_next_command(205, "母线未预充")

    _, _, results = _exchange(session, device, CMD_START)

    assert results[0].success is False
    assert results[0].error_code == 205
    assert results[0].message == "母线未预充"
    assert device.state is VirtualDeviceState.READY


def test_应答丢包最终形成命令超时(monkeypatch):
    session, device, _ = _handshake()
    monkeypatch.setattr("communications.protocol_session.time.monotonic", lambda: 10.0)
    device.drop_next_response()
    request, responses, _ = _exchange(session, device, CMD_START)

    assert responses == []
    expired = session.expire_commands(1.0, now=11.1)
    assert expired[0].sequence == request.sequence
    assert expired[0].error_code == -1


def test_延迟应答由虚拟时钟确定性释放():
    session, device, _ = _handshake()
    device.response_delay_s = 0.5
    request = session.build_command(CMD_START)

    assert device.receive_bytes(encode_v2_frame(request), now_s=2.0) == []
    assert device.poll(2.49) == []
    responses = device.poll(2.5)

    assert session.handle_frame(decode_v2_frame(responses[0])).success is True


def test_CRC破坏应答被流解码器拒绝():
    session, device, _ = _handshake()
    device.corrupt_next_response()
    request = session.build_command(CMD_START)
    responses = device.receive_bytes(encode_v2_frame(request))
    decoder = V2StreamDecoder()

    assert decoder.feed(responses[0]) == []
    assert decoder.error_count == 1
    assert request.sequence in session.pending_sequences


def test_设备重启后旧会话命令收到未握手NACK():
    session, device, _ = _handshake()
    device.reboot()

    _, _, results = _exchange(session, device, CMD_START)

    assert results[0].success is False
    assert results[0].error_code == 100
    assert "握手" in results[0].message
    assert device.state is VirtualDeviceState.BOOT


def test_版本不兼容无法完成会话():
    session = ProtocolSession()
    device = V2VirtualDevice(protocol_min=3, protocol_max=4)
    session, device, _ = _handshake(session, device)

    assert session.state is ProtocolSessionState.INCOMPATIBLE
    assert device.handshake_complete is False


def test_故障注入遥测急停与复位路径():
    session, device, _ = _handshake()
    device.inject_fault(0x42, "模拟栅极驱动故障")
    telemetry_raw = device.poll(0.0)
    telemetry = decode_v2_frame(telemetry_raw[0])
    payload = json.loads(telemetry.payload)
    assert telemetry.message_type is MessageType.TELEMETRY
    assert payload["fault_code"] == 0x42
    assert device.state is VirtualDeviceState.FAULT_LOCKED

    _, _, rejected = _exchange(session, device, CMD_START)
    assert rejected[0].error_code == 111
    _, _, reset = _exchange(session, device, CMD_RESET_FAULT)
    assert reset[0].success is True
    assert device.state is VirtualDeviceState.READY
    _, _, emergency = _exchange(session, device, CMD_EMERGENCY_STOP)
    assert emergency[0].success is True
    assert device.state is VirtualDeviceState.FAULT_LOCKED
