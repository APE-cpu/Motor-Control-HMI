"""F1/F4 原生数据处理器边界。"""
from __future__ import annotations

import os


try:
    import motor_core_cpp as _native
except (ImportError, OSError) as exc:
    _native = None
    _native_import_error = str(exc)
else:
    _native_import_error = ""


def native_telemetry_available() -> bool:
    return _native is not None and hasattr(_native, "TelemetryProcessor")


def native_telemetry_mode() -> str:
    mode = os.getenv("MOTOR_HMI_NATIVE_TELEMETRY", "auto").strip().lower()
    if mode not in {"auto", "native", "python"}:
        raise ValueError(
            "MOTOR_HMI_NATIVE_TELEMETRY 只能是 auto、native 或 python")
    if mode == "native" and not native_telemetry_available():
        raise RuntimeError(
            f"已强制使用 C++ 遥测解析器，但加载失败：{_native_import_error}")
    return mode


def native_telemetry_enabled() -> bool:
    return (native_telemetry_mode() != "python" and
            native_telemetry_available())


class NativeTelemetryProcessor:
    backend = "cpp"

    def __init__(self, max_f1_samples: int = 131072,
                 max_bursts: int = 4) -> None:
        if not native_telemetry_available():
            raise RuntimeError(f"C++遥测解析器不可用：{_native_import_error}")
        self._processor = _native.TelemetryProcessor(
            max(1, int(max_f1_samples)), max(1, int(max_bursts)))

    def ingest(self, command: int, payload: bytes) -> bool:
        return bool(self._processor.ingest(int(command) & 0xFF, bytes(payload)))

    def set_f1_rate_hz(self, rate_hz: int) -> None:
        self._processor.set_f1_rate_hz(max(1, int(rate_hz)))

    def set_rls_coefficients_si(self, enabled: bool) -> None:
        self._processor.set_rls_coefficients_si(bool(enabled))

    def set_host_rls_enabled(self, enabled: bool, *, reset: bool = True) -> None:
        self._processor.set_host_rls_enabled(bool(enabled), bool(reset))

    @property
    def host_rls_enabled(self) -> bool:
        return bool(self._processor.host_rls_enabled)

    def drain_f1(self, max_samples: int = 8192) -> list[dict]:
        return list(self._processor.drain_f1(max(1, int(max_samples))))

    def drain_f1_columns(self, max_samples: int = 8192) -> dict:
        return dict(self._processor.drain_f1_columns(
            max(1, int(max_samples))))

    def drain_f2(self, max_samples: int = 512) -> list[dict]:
        return list(self._processor.drain_f2(max(1, int(max_samples))))

    def drain_f3(self, max_samples: int = 512) -> list[dict]:
        return list(self._processor.drain_f3(max(1, int(max_samples))))

    def drain_bursts(self, max_bursts: int = 1) -> list[dict]:
        return list(self._processor.drain_bursts(max(1, int(max_bursts))))

    def stats(self) -> dict[str, int]:
        return dict(self._processor.stats())

    def reset(self) -> None:
        self._processor.reset()

    def reset_burst(self) -> None:
        self._processor.reset_burst()
