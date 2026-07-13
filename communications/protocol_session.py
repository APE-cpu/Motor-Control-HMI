"""v2 协议握手、能力协商与命令 ACK 跟踪。"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .protocol_v2 import (
    MessageType, ProtocolV2Error, V2Frame, V2_VERSION, decode_nack_payload,
)


class ProtocolSessionState(str, Enum):
    IDLE = "idle"
    HELLO_SENT = "hello_sent"
    READY = "ready"
    INCOMPATIBLE = "incompatible"


@dataclass
class DeviceCapabilities:
    device_id: str
    firmware_version: str
    hardware_version: str = ""
    protocol_min: int = V2_VERSION
    protocol_max: int = V2_VERSION
    commands: list[int] = field(default_factory=list)
    telemetry_fields: list[str] = field(default_factory=list)
    max_payload: int = 4096

    def to_payload(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes) -> "DeviceCapabilities":
        try:
            data: dict[str, Any] = json.loads(payload.decode("utf-8"))
            return cls(**data)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ProtocolV2Error("CAPABILITIES payload 无效") from exc


@dataclass(frozen=True)
class CommandResult:
    sequence: int
    command: int
    success: bool
    error_code: int = 0
    message: str = ""


class ProtocolSession:
    """单设备 v2 会话；不负责 I/O，只生成与消费 V2Frame。"""

    def __init__(self, address: int = 1, client_name: str = "Motor-Control-HMI") -> None:
        self.address = address
        self.client_name = client_name
        self.state = ProtocolSessionState.IDLE
        self.capabilities: DeviceCapabilities | None = None
        self.negotiated_version: int | None = None
        self.incompatible_reason = ""
        self.session_lost_reason = ""
        self._next_sequence = 1
        self._pending: dict[int, tuple[int, float]] = {}

    @property
    def pending_sequences(self) -> tuple[int, ...]:
        return tuple(self._pending)

    def build_hello(self) -> V2Frame:
        payload = json.dumps({
            "client": self.client_name,
            "protocol_min": V2_VERSION,
            "protocol_max": V2_VERSION,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = V2Frame(MessageType.HELLO, address=self.address,
                        sequence=self._allocate_sequence(), payload=payload)
        self.state = ProtocolSessionState.HELLO_SENT
        return frame

    def build_command(self, command: int, payload: bytes = b"") -> V2Frame:
        if self.state is not ProtocolSessionState.READY:
            raise ProtocolV2Error("协议握手尚未完成")
        if self.capabilities and self.capabilities.commands and \
                command not in self.capabilities.commands:
            raise ProtocolV2Error(f"设备未声明支持命令 0x{command:02X}")
        sequence = self._allocate_sequence()
        self._pending[sequence] = (command, time.monotonic())
        return V2Frame(MessageType.COMMAND, command, payload,
                       self.address, sequence, self.negotiated_version or V2_VERSION)

    def build_heartbeat(self) -> V2Frame:
        if self.state is not ProtocolSessionState.READY:
            raise ProtocolV2Error("协议会话未就绪")
        if self.has_pending_command(0):
            raise ProtocolV2Error("已有心跳等待应答")
        sequence = self._allocate_sequence()
        self._pending[sequence] = (0, time.monotonic())
        return V2Frame(MessageType.HEARTBEAT, address=self.address,
                       sequence=sequence,
                       version=self.negotiated_version or V2_VERSION)

    def has_pending_command(self, command: int) -> bool:
        return any(item[0] == command for item in self._pending.values())

    def invalidate(self, reason: str) -> list[CommandResult]:
        """会话丢失：清理全部待应答命令并返回明确失败结果。"""
        self.session_lost_reason = reason
        self.state = ProtocolSessionState.IDLE
        self.negotiated_version = None
        results = [
            CommandResult(sequence, command, False, -3, reason)
            for sequence, (command, _sent_at) in self._pending.items()
        ]
        self._pending.clear()
        return results

    def handle_frame(self, frame: V2Frame) -> DeviceCapabilities | CommandResult | None:
        if frame.address != self.address:
            return None
        if frame.message_type is MessageType.CAPABILITIES:
            return self._handle_capabilities(frame)
        if frame.message_type not in (MessageType.ACK, MessageType.NACK):
            return None
        pending = self._pending.pop(frame.sequence, None)
        if pending is None:
            return None  # 迟到或重复应答，不误配给其它命令
        command, _sent_at = pending
        if command != frame.command:
            return CommandResult(frame.sequence, command, False, -2, "ACK 命令字不匹配")
        if frame.message_type is MessageType.ACK:
            return CommandResult(frame.sequence, command, True)
        error_code, message = decode_nack_payload(frame.payload)
        return CommandResult(frame.sequence, command, False, error_code, message)

    def expire_commands(self, timeout_s: float, now: float | None = None) -> list[CommandResult]:
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须大于 0")
        current = time.monotonic() if now is None else now
        expired = []
        for sequence, (command, sent_at) in list(self._pending.items()):
            if current - sent_at >= timeout_s:
                del self._pending[sequence]
                expired.append(CommandResult(
                    sequence, command, False, -1, f"命令应答超时（{timeout_s:g}s）"))
        return expired

    def _handle_capabilities(self, frame: V2Frame) -> DeviceCapabilities:
        if self.state is not ProtocolSessionState.HELLO_SENT:
            raise ProtocolV2Error("未发送 HELLO 却收到 CAPABILITIES")
        capabilities = DeviceCapabilities.from_payload(frame.payload)
        low = max(V2_VERSION, capabilities.protocol_min)
        high = min(V2_VERSION, capabilities.protocol_max)
        if low > high:
            self.state = ProtocolSessionState.INCOMPATIBLE
            self.incompatible_reason = (
                f"协议版本不兼容：上位机={V2_VERSION}，设备="
                f"{capabilities.protocol_min}..{capabilities.protocol_max}")
            return capabilities
        self.capabilities = capabilities
        self.negotiated_version = high
        self.session_lost_reason = ""
        self.state = ProtocolSessionState.READY
        return capabilities

    def _allocate_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence = 1 if sequence >= 0xFFFF else sequence + 1
        return sequence
