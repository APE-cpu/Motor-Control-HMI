"""可脚本化的 v2 虚拟下位机，用于无功率级协议与故障测试。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from config.config import (
    CMD_EMERGENCY_STOP, CMD_RESET_FAULT, CMD_SET_PARAMS, CMD_SET_SENSOR,
    CMD_START, CMD_STOP, CMD_TELEMETRY,
)

from .protocol_session import DeviceCapabilities
from .protocol_v2 import (
    MessageType, ProtocolV2Error, V2Frame, V2StreamDecoder, encode_v2_frame,
    make_ack, make_nack,
)


class VirtualDeviceState(str, Enum):
    BOOT = "boot"
    READY = "ready"
    RUNNING = "running"
    FAULT_LOCKED = "fault_locked"


@dataclass
class _ScheduledResponse:
    due_s: float
    order: int
    data: bytes


class V2VirtualDevice:
    """纯内存虚拟设备。

    receive_bytes()/poll() 使用调用方提供的虚拟时间，因此延迟和超时测试快速且可重复。
    """

    def __init__(
        self,
        *,
        address: int = 1,
        device_id: str = "VIRTUAL-MOTOR-001",
        firmware_version: str = "sim-2.0",
        hardware_version: str = "virtual",
        protocol_min: int = 2,
        protocol_max: int = 2,
    ) -> None:
        self.address = address
        self.capabilities = DeviceCapabilities(
            device_id=device_id,
            firmware_version=firmware_version,
            hardware_version=hardware_version,
            protocol_min=protocol_min,
            protocol_max=protocol_max,
            commands=[CMD_START, CMD_STOP, CMD_EMERGENCY_STOP, CMD_RESET_FAULT,
                      CMD_SET_PARAMS, CMD_SET_SENSOR],
            telemetry_fields=[
                "speed_actual", "speed_target", "current_actual", "temperature",
                "vdc", "bus_state", "fault_code", "fault_text",
            ],
        )
        self.state = VirtualDeviceState.BOOT
        self.handshake_complete = False
        self.decoder = V2StreamDecoder()
        self.command_log: list[V2Frame] = []
        self.response_delay_s = 0.0
        self._drop_responses = 0
        self._corrupt_responses = 0
        self._next_nack: tuple[int, str] | None = None
        self._scheduled: list[_ScheduledResponse] = []
        self._schedule_order = 0
        self._telemetry_sequence = 1
        self.fault_code = 0
        self.fault_text = ""

    def receive_bytes(self, data: bytes, now_s: float = 0.0) -> list[bytes]:
        for frame in self.decoder.feed(data):
            response = self._handle_frame(frame)
            if response is not None:
                self._schedule(response, now_s)
        return self.poll(now_s)

    def poll(self, now_s: float) -> list[bytes]:
        ready = [item for item in self._scheduled if item.due_s <= now_s]
        self._scheduled = [item for item in self._scheduled if item.due_s > now_s]
        ready.sort(key=lambda item: (item.due_s, item.order))
        return [item.data for item in ready]

    def drop_next_response(self, count: int = 1) -> None:
        if count < 1:
            raise ValueError("count 必须至少为 1")
        self._drop_responses += count

    def corrupt_next_response(self, count: int = 1) -> None:
        if count < 1:
            raise ValueError("count 必须至少为 1")
        self._corrupt_responses += count

    def nack_next_command(self, error_code: int, message: str) -> None:
        self._next_nack = (int(error_code), message)

    def inject_fault(self, error_code: int, message: str,
                     now_s: float = 0.0) -> None:
        self.fault_code = int(error_code)
        self.fault_text = message
        self.state = VirtualDeviceState.FAULT_LOCKED
        if self.handshake_complete:
            self._schedule(self._telemetry_frame(), now_s)

    def reboot(self) -> None:
        """模拟设备掉电重启：握手、运行状态、调度响应与故障全部清空。"""
        self.state = VirtualDeviceState.BOOT
        self.handshake_complete = False
        self.decoder = V2StreamDecoder()
        self._scheduled.clear()
        self._next_nack = None
        self.fault_code = 0
        self.fault_text = ""

    def emit_telemetry(self, values: dict | None = None,
                       now_s: float = 0.0) -> list[bytes]:
        if not self.handshake_complete:
            return []
        self._schedule(self._telemetry_frame(values), now_s)
        return self.poll(now_s)

    def _handle_frame(self, frame: V2Frame) -> V2Frame | None:
        if frame.address != self.address:
            return None
        if frame.message_type is MessageType.HELLO:
            return self._handle_hello(frame)
        if frame.message_type is MessageType.HEARTBEAT:
            return make_ack(frame) if self.handshake_complete else \
                make_nack(frame, 100, "尚未完成协议握手")
        if frame.message_type is not MessageType.COMMAND:
            return None
        self.command_log.append(frame)
        if not self.handshake_complete:
            return make_nack(frame, 100, "尚未完成协议握手")
        if frame.command not in self.capabilities.commands:
            return make_nack(frame, 101, "设备不支持该命令")
        if self._next_nack is not None:
            code, message = self._next_nack
            self._next_nack = None
            return make_nack(frame, code, message)
        if frame.command == CMD_EMERGENCY_STOP:
            self.state = VirtualDeviceState.FAULT_LOCKED
            self.fault_code = 1
            self.fault_text = "虚拟设备急停锁定"
            return make_ack(frame)
        if frame.command == CMD_RESET_FAULT:
            if self.state is not VirtualDeviceState.FAULT_LOCKED:
                return make_nack(frame, 110, "设备当前没有锁定故障")
            self.state = VirtualDeviceState.READY
            self.fault_code = 0
            self.fault_text = ""
            return make_ack(frame)
        if self.state is VirtualDeviceState.FAULT_LOCKED:
            return make_nack(frame, 111, "保护尚未复位")
        if frame.command == CMD_START:
            if self.state is not VirtualDeviceState.READY:
                return make_nack(frame, 112, "设备不在 READY")
            self.state = VirtualDeviceState.RUNNING
        elif frame.command == CMD_STOP:
            if self.state is not VirtualDeviceState.RUNNING:
                return make_nack(frame, 113, "设备不在 RUNNING")
            self.state = VirtualDeviceState.READY
        return make_ack(frame)

    def _handle_hello(self, frame: V2Frame) -> V2Frame:
        try:
            hello = json.loads(frame.payload.decode("utf-8"))
            client_min = int(hello["protocol_min"])
            client_max = int(hello["protocol_max"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return make_nack(frame, 102, "HELLO payload 无效")
        overlap = max(client_min, self.capabilities.protocol_min) <= \
            min(client_max, self.capabilities.protocol_max)
        self.handshake_complete = overlap
        self.state = VirtualDeviceState.READY if overlap else VirtualDeviceState.BOOT
        return V2Frame(
            MessageType.CAPABILITIES, payload=self.capabilities.to_payload(),
            address=self.address, sequence=frame.sequence)

    def _telemetry_frame(self, values: dict | None = None) -> V2Frame:
        payload = {
            "speed_actual": 0.0,
            "speed_target": 0.0,
            "current_actual": 0.0,
            "temperature": 25.0,
            "vdc": 24.0,
            "bus_state": "normal",
            "fault_code": self.fault_code,
            "fault_text": self.fault_text,
            "device_state": self.state.value,
        }
        payload.update(values or {})
        if self.fault_code:
            payload["fault_code"] = self.fault_code
            payload["fault_text"] = self.fault_text
        payload["device_state"] = self.state.value
        sequence = self._telemetry_sequence
        self._telemetry_sequence = 1 if sequence >= 0xFFFF else sequence + 1
        return V2Frame(
            MessageType.TELEMETRY, command=CMD_TELEMETRY,
            payload=json.dumps(payload, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"),
            address=self.address, sequence=sequence)

    def _schedule(self, frame: V2Frame, now_s: float) -> None:
        if self._drop_responses:
            self._drop_responses -= 1
            return
        data = bytearray(encode_v2_frame(frame))
        if self._corrupt_responses:
            self._corrupt_responses -= 1
            data[-3] ^= 0x01  # 破坏 CRC16 低字节
        self._schedule_order += 1
        self._scheduled.append(_ScheduledResponse(
            now_s + self.response_delay_s, self._schedule_order, bytes(data)))
