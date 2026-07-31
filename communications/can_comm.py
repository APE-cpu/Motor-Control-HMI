"""CAN 总线通信封装（python-can）。

默认使用 socketcan / pcan / kvaser 等接口由 interface 参数指定。
"""
from typing import Optional

from .base_comm import BaseComm

try:
    import can
    _CAN_OK = True
except Exception:  # pragma: no cover
    _CAN_OK = False


class CANComm(BaseComm):
    name = "CAN"

    def __init__(self) -> None:
        self._bus = None

    def open(self, channel: str = "can0", bitrate: int = 500_000,
             interface: str = "socketcan", **_) -> bool:
        if not _CAN_OK:
            raise RuntimeError("未安装 python-can，无法使用 CAN 通信")
        import can as _c
        self._bus = _c.interface.Bus(
            channel=channel, bustype=interface, bitrate=int(bitrate)
        )
        return self._bus is not None

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass
        self._bus = None

    def is_open(self) -> bool:
        return self._bus is not None

    def send(self, data: bytes, arbitration_id: int = 0x100) -> int:
        if not self.is_open():
            raise RuntimeError("CAN 未打开")
        if len(data) > 8:
            raise ValueError(
                f"经典CAN单帧最多8字节，拒绝静默截断 {len(data)} 字节；"
                "请使用已定义的分段协议或CAN-FD")
        import can as _c
        msg = _c.Message(arbitration_id=arbitration_id,
                         data=list(data), is_extended_id=False)
        self._bus.send(msg)
        return len(msg.data)

    def recv(self, size: int = 8, timeout: Optional[float] = 1.0) -> bytes:
        if not self.is_open():
            raise RuntimeError("CAN 未打开")
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            return b""
        return bytes(msg.data[:size])

    def recv_can(self, timeout: float = 0.05):
        """读取一帧 CAN，返回 (arbitration_id, data) 或 None。

        与 ZlgCanComm 保持同一接口，供 comm_manager 统一调用。
        """
        if not self.is_open():
            return None
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            return None
        return int(msg.arbitration_id), bytes(msg.data)
