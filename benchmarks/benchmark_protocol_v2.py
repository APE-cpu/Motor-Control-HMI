"""可重复的 v2 流解码吞吐基准。

默认模拟 F1 遥测：22 字节载荷、16 KiB 一次读取。结果包含 V2Frame 对象创建，
因此反映 ``CommManager`` 实际获得已解码帧之前的成本。
"""
from __future__ import annotations

import argparse
import gc
import statistics
import time

from communications.native_protocol import (
    NativeV2StreamDecoder, native_protocol_available,
)
from communications.protocol_v2 import (
    MessageType, V2Frame, V2StreamDecoder, encode_v2_frame,
)


def make_wire(frame_count: int, payload_size: int) -> bytes:
    payload = bytes(index & 0xFF for index in range(payload_size))
    return b"".join(
        encode_v2_frame(V2Frame(
            MessageType.TELEMETRY, command=0xF1, payload=payload,
            sequence=index & 0xFFFF,
        ))
        for index in range(frame_count)
    )


def measure(factory, wire: bytes, frame_count: int, chunk_size: int,
            repeats: int) -> tuple[float, float]:
    durations = []
    for _ in range(repeats):
        decoder = factory()
        decoded = 0
        gc.collect()
        started = time.perf_counter()
        for offset in range(0, len(wire), chunk_size):
            decoded += len(decoder.feed(wire[offset:offset + chunk_size]))
        durations.append(time.perf_counter() - started)
        assert decoded == frame_count
        assert decoder.error_count == 0
    median = statistics.median(durations)
    return frame_count / median, len(wire) / median / (1024 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=50_000)
    parser.add_argument("--payload", type=int, default=22)
    parser.add_argument("--chunk", type=int, default=16 * 1024)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    wire = make_wire(args.frames, args.payload)

    python_fps, python_mib = measure(
        V2StreamDecoder, wire, args.frames, args.chunk, args.repeats)
    print(f"python: {python_fps:,.0f} frames/s, {python_mib:.2f} MiB/s")

    if not native_protocol_available():
        print("native: unavailable")
        return
    native_fps, native_mib = measure(
        NativeV2StreamDecoder, wire, args.frames, args.chunk, args.repeats)
    print(f"native: {native_fps:,.0f} frames/s, {native_mib:.2f} MiB/s")
    print(f"speedup: {native_fps / python_fps:.2f}x")


if __name__ == "__main__":
    main()
