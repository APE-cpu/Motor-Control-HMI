"""RS-232 / RS-485 串口通信封装（pyserial）。

RS-232 与 RS-485 在 PC 侧通常都表现为 COM 串口；485 转换由收发器完成。
"""
from typing import Optional

from .base_comm import BaseComm

try:
    import serial
    _SERIAL_OK = True
except Exception:  # pragma: no cover
    _SERIAL_OK = False


_PARITY_MAP = {
    "无": "N",
    "奇校验": "O",
    "偶校验": "E",
}


class SerialComm(BaseComm):
    name = "Serial"

    def __init__(self) -> None:
        self._ser = None

    def open(self, port: str = "COM1", baudrate: int = 115200,
             bytesize: int = 8, stopbits: str = "1",
             parity: str = "无", timeout: float = 1.0, **_) -> bool:
        if not _SERIAL_OK:
            raise RuntimeError("未安装 pyserial，无法使用串口通信")
        import serial as _s
        self._ser = _s.Serial(
            port=port,
            baudrate=int(baudrate),
            bytesize=int(bytesize),
            stopbits=float(stopbits),
            parity=_PARITY_MAP.get(parity, "N"),
            timeout=float(timeout),
        )
        return self._ser.is_open

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def is_open(self) -> bool:
        return bool(self._ser and self._ser.is_open)

    def send(self, data: bytes) -> int:
        if not self.is_open():
            raise RuntimeError("串口未打开")
        return int(self._ser.write(data))

    def recv(self, size: int = 64, timeout: Optional[float] = None) -> bytes:
        if not self.is_open():
            raise RuntimeError("串口未打开")
        if timeout is not None:
            self._ser.timeout = timeout
        return bytes(self._ser.read(size))

    @staticmethod
    def list_ports() -> list:
        """列出可用 COM 端口名。"""
        if not _SERIAL_OK:
            return []
        try:
            from serial.tools import list_ports
            return [p.device for p in list_ports.comports()]
        except Exception:
            return []
