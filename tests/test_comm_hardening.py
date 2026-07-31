import struct

import pytest

from communications.can_comm import CANComm
from communications.comm_manager import CommManager
from communications.protocol import encode_frame
from communications.protocol_session import ProtocolSession, ProtocolSessionState
from communications.protocol_v2 import MessageType, V2Frame, encode_v2_frame
from config.config import CMD_TELEMETRY, TELEM_FMT


def _telemetry_frame(speed: int) -> bytes:
    payload = struct.pack(
        TELEM_FMT, speed, 0, 0, 0, 0, 20, 255, 255, 0)
    return encode_frame(CMD_TELEMETRY, payload)


def test_CAN拒绝静默截断超长帧():
    comm = CANComm()
    comm._bus = object()
    with pytest.raises(ValueError, match="最多8字节"):
        comm.send(b"123456789")


def test_串口假帧头校验失败后能重同步到真实帧():
    comm = CommManager()
    valid = _telemetry_frame(1234)
    # 假头声称长度3且校验错误，真实帧紧随其后。
    comm._rx_buf.extend(b"\xAA\x99\x03\x01\x02\x03\x00\x55" + valid)
    frame = comm._try_extract_serial_frame()
    assert frame is not None
    assert frame.speed_actual == 1234


def test_串口缓冲一次解析全部完整帧并返回最新值():
    comm = CommManager()
    comm._rx_buf.extend(_telemetry_frame(100) + _telemetry_frame(200))
    frame = comm._try_extract_serial_frame()
    assert frame.speed_actual == 200
    assert not comm._rx_buf


def test_正常序号永不分配保留值FFFF():
    session = ProtocolSession()
    session._next_sequence = 0xFFFE
    first = session._allocate_sequence()
    second = session._allocate_sequence()
    assert first == 0xFFFE
    assert second == 1


def test_在途心跳可取得原序号用于幂等重发():
    session = ProtocolSession()
    session.state = ProtocolSessionState.READY
    heartbeat = session.build_heartbeat()
    assert session.pending_sequence_for(0) == heartbeat.sequence


def test_v2畸形数值字段被拒绝而不是传入UI():
    comm = CommManager()
    frame = V2Frame(
        MessageType.TELEMETRY,
        payload=b'{"speed_actual":"err"}',
    )
    with pytest.raises(Exception, match="speed_actual"):
        comm._parse_v2_telemetry(frame)
