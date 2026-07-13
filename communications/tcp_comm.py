"""TCP 服务器通信封装（等待 STM32 客户端连接）。"""
import socket
from typing import Optional

from .base_comm import BaseComm


class TCPComm(BaseComm):
    name = "TCP"

    def __init__(self) -> None:
        self._server: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None

    def open(self, host: str = "0.0.0.0", port: int = 8888, timeout: float = 10.0, **_) -> bool:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, int(port)))
        self._server.listen(1)
        self._server.settimeout(timeout)
        self._conn, _ = self._server.accept()  # 阻塞等待STM32连入
        self._conn.settimeout(0.1)
        return True

    def close(self) -> None:
        for s in (self._conn, self._server):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._conn = self._server = None

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
