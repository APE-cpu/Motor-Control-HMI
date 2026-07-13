"""实验通信协议 v2：版本、地址、序号、CRC16 与消息类型。

v2 使用独立双字节帧头，不改变 protocol.py 的 v1 帧格式，可在迁移期并存。
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


V2_MAGIC = b"\xA5\x5A"
V2_VERSION = 2
V2_TAIL = 0x7E
V2_MAX_PAYLOAD = 4096
_HEADER = struct.Struct("<2sBBHBBH")
V2_MIN_FRAME_SIZE = _HEADER.size + 3  # CRC16 + tail


class MessageType(IntEnum):
    COMMAND = 1
    ACK = 2
    NACK = 3
    HELLO = 4
    CAPABILITIES = 5
    TELEMETRY = 6
    HEARTBEAT = 7


class ProtocolV2Error(ValueError):
    pass


@dataclass(frozen=True)
class V2Frame:
    message_type: MessageType
    command: int = 0
    payload: bytes = b""
    address: int = 1
    sequence: int = 0
    version: int = V2_VERSION


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE：poly=0x1021, init=0xFFFF。"""
    crc = initial
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else \
                (crc << 1) & 0xFFFF
    return crc


def encode_v2_frame(frame: V2Frame) -> bytes:
    payload = bytes(frame.payload)
    if len(payload) > V2_MAX_PAYLOAD:
        raise ProtocolV2Error(f"payload 超过 {V2_MAX_PAYLOAD} 字节")
    for name, value, maximum in (
        ("version", frame.version, 0xFF), ("address", frame.address, 0xFF),
        ("sequence", frame.sequence, 0xFFFF), ("command", frame.command, 0xFF),
    ):
        if not 0 <= int(value) <= maximum:
            raise ProtocolV2Error(f"{name} 超出范围")
    try:
        msg_type = MessageType(frame.message_type)
    except ValueError as exc:
        raise ProtocolV2Error("未知消息类型") from exc
    header = _HEADER.pack(
        V2_MAGIC, int(frame.version), int(frame.address), int(frame.sequence),
        int(msg_type), int(frame.command), len(payload))
    crc = crc16_ccitt(header[2:] + payload)
    return header + payload + struct.pack("<H", crc) + bytes([V2_TAIL])


def decode_v2_frame(data: bytes) -> V2Frame:
    if len(data) < V2_MIN_FRAME_SIZE:
        raise ProtocolV2Error("帧长度不足")
    magic, version, address, sequence, raw_type, command, length = \
        _HEADER.unpack_from(data)
    if magic != V2_MAGIC:
        raise ProtocolV2Error("帧头错误")
    if length > V2_MAX_PAYLOAD:
        raise ProtocolV2Error("payload 长度越界")
    expected = _HEADER.size + length + 3
    if len(data) != expected:
        raise ProtocolV2Error("长度字段与实际帧长不一致")
    if data[-1] != V2_TAIL:
        raise ProtocolV2Error("帧尾错误")
    payload = data[_HEADER.size:_HEADER.size + length]
    expected_crc = struct.unpack_from("<H", data, _HEADER.size + length)[0]
    actual_crc = crc16_ccitt(data[2:_HEADER.size] + payload)
    if actual_crc != expected_crc:
        raise ProtocolV2Error("CRC16 校验失败")
    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise ProtocolV2Error(f"未知消息类型: {raw_type}") from exc
    return V2Frame(
        version=version, address=address, sequence=sequence,
        message_type=message_type, command=command, payload=payload)


def make_ack(request: V2Frame, payload: bytes = b"") -> V2Frame:
    return V2Frame(MessageType.ACK, request.command, payload,
                   request.address, request.sequence)


def make_nack(request: V2Frame, error_code: int, message: str) -> V2Frame:
    payload = json.dumps({"error_code": int(error_code), "message": message},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return V2Frame(MessageType.NACK, request.command, payload,
                   request.address, request.sequence)


def decode_nack_payload(payload: bytes) -> tuple[int, str]:
    try:
        data: dict[str, Any] = json.loads(payload.decode("utf-8"))
        return int(data["error_code"]), str(data.get("message", ""))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ProtocolV2Error("NACK payload 无效") from exc


class V2StreamDecoder:
    """从串口/TCP 任意分包流中提取 v2 帧，并跳过噪声或损坏帧。"""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.error_count = 0

    def feed(self, chunk: bytes) -> list[V2Frame]:
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(V2_MAGIC)
            if start < 0:
                # 保留可能是双字节帧头第一个字节的末尾。
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(V2_MAGIC[:1]) else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < _HEADER.size:
                break
            length = struct.unpack_from("<H", self.buffer, 8)[0]
            if length > V2_MAX_PAYLOAD:
                self.error_count += 1
                del self.buffer[0]
                continue
            total = _HEADER.size + length + 3
            if len(self.buffer) < total:
                break
            candidate = bytes(self.buffer[:total])
            try:
                frames.append(decode_v2_frame(candidate))
                del self.buffer[:total]
            except ProtocolV2Error:
                self.error_count += 1
                del self.buffer[0]
        return frames
