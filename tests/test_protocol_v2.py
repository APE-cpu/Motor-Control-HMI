import pytest

from communications.protocol import encode_frame
from communications.protocol_session import (
    DeviceCapabilities, ProtocolSession, ProtocolSessionState,
)
from communications.protocol_v2 import (
    MessageType, ProtocolV2Error, V2Frame, V2StreamDecoder, crc16_ccitt,
    decode_v2_frame, encode_v2_frame, make_ack, make_nack,
)


def test_CRC16标准校验向量():
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_v2帧编解码往返():
    original = V2Frame(
        MessageType.COMMAND, command=0x20, payload="目标=1500".encode(),
        address=7, sequence=513)
    encoded = encode_v2_frame(original)

    assert decode_v2_frame(encoded) == original
    assert encoded[:2] == b"\xA5\x5A"
    assert encoded != encode_frame(0x20, original.payload)


@pytest.mark.parametrize("mutation", ["header", "payload", "crc", "tail", "truncate"])
def test_v2损坏帧被拒绝(mutation):
    raw = bytearray(encode_v2_frame(
        V2Frame(MessageType.COMMAND, 0x10, b"target=1500", sequence=3)))
    if mutation == "header":
        raw[0] ^= 0xFF
    elif mutation == "payload":
        raw[10] ^= 0x01
    elif mutation == "crc":
        raw[-3] ^= 0x01
    elif mutation == "tail":
        raw[-1] ^= 0x01
    else:
        raw.pop()
    with pytest.raises(ProtocolV2Error):
        decode_v2_frame(bytes(raw))


def test_payload和字段范围限制():
    with pytest.raises(ProtocolV2Error, match="payload"):
        encode_v2_frame(V2Frame(MessageType.COMMAND, payload=b"x" * 4097))
    with pytest.raises(ProtocolV2Error, match="address"):
        encode_v2_frame(V2Frame(MessageType.COMMAND, address=256))


def test_流解码支持噪声分包粘包和坏帧恢复():
    first = encode_v2_frame(V2Frame(MessageType.HEARTBEAT, sequence=1))
    broken = bytearray(encode_v2_frame(V2Frame(MessageType.COMMAND, 2, b"bad", sequence=2)))
    broken[-3] ^= 0xFF
    last = encode_v2_frame(V2Frame(MessageType.TELEMETRY, payload=b"ok", sequence=3))
    decoder = V2StreamDecoder()

    assert decoder.feed(b"noise" + first[:5]) == []
    frames = decoder.feed(first[5:] + bytes(broken) + last)

    assert [frame.sequence for frame in frames] == [1, 3]
    assert decoder.error_count == 1


def test_ACK与NACK保留原命令和序号():
    request = V2Frame(MessageType.COMMAND, 0x10, sequence=42, address=2)
    ack = make_ack(request)
    nack = make_nack(request, 7, "保护未复位")

    assert ack.message_type is MessageType.ACK
    assert ack.sequence == 42 and ack.command == 0x10
    assert decode_v2_frame(encode_v2_frame(nack)) == nack


def test_握手成功后才能发送设备支持的命令():
    session = ProtocolSession(address=3)
    with pytest.raises(ProtocolV2Error, match="握手"):
        session.build_command(0x10)
    hello = session.build_hello()
    caps = DeviceCapabilities(
        device_id="CTRL-001", firmware_version="1.0.0",
        commands=[0x10, 0x11], telemetry_fields=["speed", "current"])
    result = session.handle_frame(V2Frame(
        MessageType.CAPABILITIES, payload=caps.to_payload(),
        address=3, sequence=hello.sequence))

    assert result == caps
    assert session.state is ProtocolSessionState.READY
    assert session.negotiated_version == 2
    command = session.build_command(0x10, b"target=1000")
    assert command.sequence in session.pending_sequences
    ack_result = session.handle_frame(make_ack(command))
    assert ack_result.success is True
    assert command.sequence not in session.pending_sequences
    with pytest.raises(ProtocolV2Error, match="未声明支持"):
        session.build_command(0x12)


def test_协议版本不兼容时拒绝进入READY():
    session = ProtocolSession()
    hello = session.build_hello()
    caps = DeviceCapabilities(
        device_id="OLD", firmware_version="0.1", protocol_min=3, protocol_max=4)
    session.handle_frame(V2Frame(
        MessageType.CAPABILITIES, payload=caps.to_payload(), sequence=hello.sequence))

    assert session.state is ProtocolSessionState.INCOMPATIBLE
    assert "不兼容" in session.incompatible_reason
    with pytest.raises(ProtocolV2Error, match="握手"):
        session.build_command(0x10)


def test_NACK和命令超时形成明确结果(monkeypatch):
    clock = iter([10.0, 20.0])
    monkeypatch.setattr("communications.protocol_session.time.monotonic", lambda: next(clock))
    session = ProtocolSession()
    hello = session.build_hello()
    caps = DeviceCapabilities("CTRL", "1.0", commands=[0x10, 0x11])
    session.handle_frame(V2Frame(
        MessageType.CAPABILITIES, payload=caps.to_payload(), sequence=hello.sequence))

    rejected = session.build_command(0x10)
    nack_result = session.handle_frame(make_nack(rejected, 12, "状态不允许启动"))
    assert nack_result.success is False
    assert nack_result.error_code == 12
    assert nack_result.message == "状态不允许启动"

    waiting = session.build_command(0x11)
    expired = session.expire_commands(5.0, now=30.0)
    assert expired[0].sequence == waiting.sequence
    assert expired[0].error_code == -1
    assert "超时" in expired[0].message


def test_迟到或重复ACK不会误配其它命令():
    session = ProtocolSession()
    hello = session.build_hello()
    caps = DeviceCapabilities("CTRL", "1.0", commands=[0x10])
    session.handle_frame(V2Frame(
        MessageType.CAPABILITIES, payload=caps.to_payload(), sequence=hello.sequence))
    command = session.build_command(0x10)

    assert session.handle_frame(make_ack(command)).success is True
    assert session.handle_frame(make_ack(command)) is None


def test_心跳超时可独立放宽(monkeypatch):
    session = ProtocolSession()
    session.state = ProtocolSessionState.READY
    session.negotiated_version = 2
    monkeypatch.setattr(
        "communications.protocol_session.time.monotonic", lambda: 10.0)
    command = session.build_command(0x10)
    heartbeat = session.build_heartbeat()

    expired = session.expire_commands(
        1.0, now=11.1, heartbeat_timeout_s=3.0)
    assert [item.sequence for item in expired] == [command.sequence]
    assert heartbeat.sequence in session.pending_sequences

    expired = session.expire_commands(
        1.0, now=13.1, heartbeat_timeout_s=3.0)
    assert [item.sequence for item in expired] == [heartbeat.sequence]


def test_心跳和会话失效清理全部待命令(monkeypatch):
    monkeypatch.setattr("communications.protocol_session.time.monotonic", lambda: 10.0)
    session = ProtocolSession()
    hello = session.build_hello()
    caps = DeviceCapabilities("CTRL", "1.0", commands=[0x10])
    session.handle_frame(V2Frame(
        MessageType.CAPABILITIES, payload=caps.to_payload(), sequence=hello.sequence))
    heartbeat = session.build_heartbeat()
    command = session.build_command(0x10)

    invalidated = session.invalidate("设备重启")

    assert session.state is ProtocolSessionState.IDLE
    assert session.session_lost_reason == "设备重启"
    assert session.pending_sequences == ()
    assert {result.sequence for result in invalidated} == {
        heartbeat.sequence, command.sequence
    }
    with pytest.raises(ProtocolV2Error, match="握手"):
        session.build_command(0x10)
