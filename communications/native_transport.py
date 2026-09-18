"""C++ TCP 遥测接收器的 Python 边界。

原生线程负责 socket 接收、v2 解码、F1–F4 解析和有界排队，不触碰 Qt
对象。F1 高频路径用列式批量跨越 C++/Python 边界，不构造逐样本字典。
"""
from __future__ import annotations

import os

from .protocol_v2 import MessageType, V2Frame
from .tcp_comm import _prefer_local_ip


try:
    import motor_core_cpp as _native
except (ImportError, OSError) as exc:
    _native = None
    _native_import_error = str(exc)
else:
    _native_import_error = ""


def native_tcp_transport_available() -> bool:
    return _native is not None and hasattr(_native, "TcpV2Receiver")


def native_tcp_transport_mode() -> str:
    mode = os.getenv("MOTOR_HMI_NATIVE_TRANSPORT", "auto").strip().lower()
    if mode not in {"auto", "native", "python"}:
        raise ValueError(
            "MOTOR_HMI_NATIVE_TRANSPORT 只能是 auto、native 或 python")
    if mode == "native" and not native_tcp_transport_available():
        raise RuntimeError(
            f"已强制使用 C++ TCP 接收器，但加载失败：{_native_import_error}")
    return mode


def native_tcp_transport_enabled() -> bool:
    return (native_tcp_transport_mode() != "python" and
            native_tcp_transport_available())


class NativeTcpV2Receiver:
    backend = "cpp-tcp"

    def __init__(self, max_queue_frames: int = 8192,
                 max_f1_samples: int = 131072) -> None:
        if not native_tcp_transport_available():
            raise RuntimeError(f"C++ TCP 接收器不可用：{_native_import_error}")
        self._receiver = _native.TcpV2Receiver(
            max(1, int(max_queue_frames)), max(1, int(max_f1_samples)))
        self.bound_local = ""

    def start(self, host: str, port: int, *, local_host: str = "",
              timeout_s: float = 2.0) -> None:
        host = str(host).strip()
        port = int(port)
        if not host or not 1 <= port <= 65535:
            raise ValueError("TCP 主机或端口无效")
        selected_local = _prefer_local_ip(host, local_host)
        try:
            self._receiver.start(
                host, port, selected_local, max(0.05, float(timeout_s)))
            self.bound_local = selected_local
        except RuntimeError:
            if not selected_local:
                raise
            self._receiver.start(host, port, "", max(0.05, float(timeout_s)))
            self.bound_local = ""

    def stop(self) -> None:
        self._receiver.stop()

    def drain(self, max_frames: int = 512) -> list[V2Frame]:
        return [
            V2Frame(
                version=int(version), address=int(address),
                sequence=int(sequence), message_type=MessageType(raw_type),
                command=int(command), payload=bytes(payload),
            )
            for version, address, sequence, raw_type, command, payload
            in self._receiver.drain(max(1, int(max_frames)))
        ]

    def drain_f1(self, max_samples: int = 8192) -> list[dict]:
        return list(self._receiver.drain_f1(max(1, int(max_samples))))

    def drain_f1_columns(self, max_samples: int = 8192) -> dict:
        return dict(self._receiver.drain_f1_columns(
            max(1, int(max_samples))))

    def drain_f2(self, max_samples: int = 512) -> list[dict]:
        return list(self._receiver.drain_f2(max(1, int(max_samples))))

    def drain_f3(self, max_samples: int = 512) -> list[dict]:
        return list(self._receiver.drain_f3(max(1, int(max_samples))))

    def drain_bursts(self, max_bursts: int = 1) -> list[dict]:
        return list(self._receiver.drain_bursts(max(1, int(max_bursts))))

    def set_f1_rate_hz(self, rate_hz: int) -> None:
        self._receiver.set_f1_rate_hz(max(1, int(rate_hz)))

    def set_rls_coefficients_si(self, enabled: bool) -> None:
        self._receiver.set_rls_coefficients_si(bool(enabled))

    def set_host_rls_enabled(self, enabled: bool, *, reset: bool = True) -> None:
        self._receiver.set_host_rls_enabled(bool(enabled), bool(reset))

    @property
    def host_rls_enabled(self) -> bool:
        return bool(self._receiver.host_rls_enabled)

    def set_telemetry_processing_enabled(self, enabled: bool) -> None:
        self._receiver.set_telemetry_processing_enabled(bool(enabled))

    def reset_burst(self) -> None:
        self._receiver.reset_burst()

    def stats(self) -> dict[str, object]:
        return dict(self._receiver.stats())
