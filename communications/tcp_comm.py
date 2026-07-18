"""TCP 客户端通信封装（主动连接 STM32 TCP 服务端）。"""
import socket
from typing import Optional

from .base_comm import BaseComm


class TCPComm(BaseComm):
    name = "TCP"

    def __init__(self) -> None:
        self._conn: Optional[socket.socket] = None

    def open(self, host: str = "192.168.1.50", port: int = 5000,
             timeout: float = 10.0, **_) -> bool:
        self.close()
        self._conn = socket.create_connection((host, int(port)), timeout)
        self._conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn.settimeout(0.1)
        return True

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def is_open(self) -> bool:
        return self._conn is not None

    def send(self, data: bytes, **_) -> int:
        if not self.is_open():
            raise RuntimeError("TCP未连接")
        self._conn.sendall(data)
        return len(data)

    def recv(self, size: int = 64, timeout: Optional[float] = None) -> bytes:
        if not self.is_open():
            raise RuntimeError("TCP未连接")
        if timeout is not None:
            self._conn.settimeout(timeout)
        try:
            return self._conn.recv(size)
        except socket.timeout:
            return b""
