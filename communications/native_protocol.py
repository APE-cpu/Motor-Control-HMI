"""协议 v2 原生解码适配层。

上层只依赖 ``feed/error_count`` 这一稳定接口。原生扩展可用时负责 CRC、
寻帧和流重同步；未构建扩展时自动回退到已有 Python 实现。
"""
from __future__ import annotations

import os
from typing import Protocol

from .protocol_v2 import MessageType, V2Frame, V2StreamDecoder


class ProtocolStreamDecoder(Protocol):
    @property
    def error_count(self) -> int: ...

    def feed(self, chunk: bytes) -> list[V2Frame]: ...


try:
    import motor_core_cpp as _native
except (ImportError, OSError) as exc:  # 扩展是可选组件，打包 lite 版仍可运行。
    _native = None
    _native_import_error = str(exc)
else:
    _native_import_error = ""


class NativeV2StreamDecoder:
    """把 C++ 的紧凑元组结果转换为现有不可变 ``V2Frame``。"""

    backend = "cpp"

    def __init__(self) -> None:
        if _native is None:
            raise RuntimeError(f"C++ 协议核心不可用：{_native_import_error}")
        self._decoder = _native.V2StreamDecoder()

    @property
    def error_count(self) -> int:
        return int(self._decoder.error_count)

    @property
    def buffered_bytes(self) -> int:
        return int(self._decoder.buffered_bytes)

    def feed(self, chunk: bytes) -> list[V2Frame]:
        return [
            V2Frame(
                version=int(version), address=int(address),
                sequence=int(sequence), message_type=MessageType(raw_type),
                command=int(command), payload=bytes(payload),
            )
            for version, address, sequence, raw_type, command, payload
            in self._decoder.feed(bytes(chunk))
        ]


def native_protocol_available() -> bool:
    return _native is not None


def native_protocol_diagnostics() -> dict[str, object]:
    return {
        "available": native_protocol_available(),
        "version": getattr(_native, "__version__", "") if _native else "",
        "import_error": _native_import_error,
    }


def create_v2_stream_decoder() -> ProtocolStreamDecoder:
    """按环境选择解码器；默认自动使用 C++，失败时保持 Python 可用。"""
    mode = os.getenv("MOTOR_HMI_NATIVE_PROTOCOL", "auto").strip().lower()
    if mode not in {"auto", "native", "python"}:
        raise ValueError(
            "MOTOR_HMI_NATIVE_PROTOCOL 只能是 auto、native 或 python")
    if mode != "python" and native_protocol_available():
        return NativeV2StreamDecoder()
    if mode == "native":
        raise RuntimeError(f"已强制使用 C++ 协议核心，但加载失败：{_native_import_error}")
    decoder = V2StreamDecoder()
    decoder.backend = "python"
    return decoder
