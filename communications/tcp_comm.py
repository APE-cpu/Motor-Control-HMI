"""TCP 客户端通信封装（主动连接 STM32 TCP 服务端）。"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional

from .base_comm import BaseComm


def _prefer_local_ip(host: str, preferred: str = "") -> str:
    """Pick a local IPv4 that can reach the board without TUN/proxy tunnels.

    Lab PCs often have WLAN + Ethernet on the same 192.168.1.0/24, plus a
    Meta/Clash TUN (198.18.x).  Default routing may send the TCP connect via
    the wrong interface and break HELLO/ACK.  Prefer an explicit bind, then a
    same-subnet non-tunnel address, then OS default.
    """
    preferred = (preferred or "").strip()
    if preferred:
        return preferred
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if target.version != 4:
        return ""

    candidates: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            # Skip common tunnel / fake-ip ranges.
            if ip.startswith("198.18.") or ip.startswith("100."):
                continue
            candidates.append(ip)
    except OSError:
        candidates = []

    # Same subnet first (board is usually 192.168.1.50/24).
    for ip in candidates:
        try:
            if target in ipaddress.ip_network(f"{ip}/24", strict=False):
                return ip
        except ValueError:
            continue
    return candidates[0] if candidates else ""


class TCPComm(BaseComm):
    name = "TCP"

    def __init__(self) -> None:
        self._conn: Optional[socket.socket] = None
        self._bound_local = ""

    def open(self, host: str = "192.168.1.50", port: int = 5000,
             timeout: float = 10.0, local_host: str = "", **_) -> bool:
        self.close()
        local_ip = _prefer_local_ip(host, local_host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(float(timeout))
        try:
            if local_ip:
                # Port 0 = ephemeral; forces the correct NIC out the door.
                sock.bind((local_ip, 0))
                self._bound_local = local_ip
            sock.connect((host, int(port)))
        except OSError:
            sock.close()
            # Fallback: OS routing (keeps old behaviour if bind failed).
            if local_ip:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(float(timeout))
                try:
                    sock.connect((host, int(port)))
                    self._bound_local = ""
                except OSError:
                    sock.close()
                    raise
            else:
                raise
        sock.settimeout(0.1)
        self._conn = sock
        return True

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._bound_local = ""

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
            data = self._conn.recv(size)
            if data == b"":
                self.close()
                raise ConnectionError("TCP对端已正常关闭连接")
            return data
        except socket.timeout:
            return b""
