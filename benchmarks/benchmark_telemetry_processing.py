"""比较 Python 与 C++ 的 F1 载荷解析、物理量换算与批量导出。"""
from __future__ import annotations

import argparse
import gc
import statistics
import struct
import time

from communications.native_telemetry import (
    NativeTelemetryProcessor, native_telemetry_available,
)


_CURRENT_SCALE = 0.000629
_ANGLE_SCALE = 360.0 / 65536.0


def make_payload(samples_per_frame: int) -> bytes:
    return b"".join(
        struct.pack(
            "<IHhhhhhhhH", index, index & 0xFFFF, index % 3000,
            index % 1000, index % 900, index % 800, -(index % 700),
            index % 600, -(index % 500), 48,
        )
        for index in range(samples_per_frame)
    )


def parse_python(payload: bytes, rate_hz: int) -> list[dict]:
    samples = []
    for offset in range(0, len(payload), 22):
        values = struct.unpack("<IHhhhhhhhH", payload[offset:offset + 22])
        samples.append({
            "tick_ms": values[0],
            "rate_hz": rate_hz,
            "angle_deg": values[1] * _ANGLE_SCALE,
            "speed_rpm": float(values[2]),
            "iq_a": values[3] * _CURRENT_SCALE,
            "iqref_a": values[4] * _CURRENT_SCALE,
            "ia_a": values[5] * _CURRENT_SCALE,
            "ib_a": values[6] * _CURRENT_SCALE,
            "vd_raw": float(values[7]),
            "vq_raw": float(values[8]),
            "vbus_v": float(values[9]),
        })
    return samples


def measure_python(payload: bytes, frames: int, repeats: int) -> float:
    durations = []
    expected = frames * (len(payload) // 22)
    for _ in range(repeats):
        gc.collect()
        count = 0
        started = time.perf_counter()
        for _index in range(frames):
            count += len(parse_python(payload, 16000))
        durations.append(time.perf_counter() - started)
        assert count == expected
    return expected / statistics.median(durations)


def measure_native(payload: bytes, frames: int,
                   repeats: int, *, columnar: bool = False) -> tuple[float, float]:
    ingest_durations = []
    end_to_end_durations = []
    expected = frames * (len(payload) // 22)
    for _ in range(repeats):
        processor = NativeTelemetryProcessor(max_f1_samples=expected + 1)
        processor.set_f1_rate_hz(16000)
        gc.collect()
        started = time.perf_counter()
        for _index in range(frames):
            assert processor.ingest(0xF1, payload)
        ingested = time.perf_counter()
        exported = (
            processor.drain_f1_columns(expected + 1)
            if columnar else processor.drain_f1(expected + 1))
        finished = time.perf_counter()
        ingest_durations.append(ingested - started)
        end_to_end_durations.append(finished - started)
        actual = int(exported["count"]) if columnar else len(exported)
        assert actual == expected
    return (
        expected / statistics.median(ingest_durations),
        expected / statistics.median(end_to_end_durations),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=10_000)
    parser.add_argument("--samples-per-frame", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    payload = make_payload(args.samples_per_frame)

    python_rate = measure_python(payload, args.frames, args.repeats)
    print(f"python: {python_rate:,.0f} samples/s")
    if not native_telemetry_available():
        print("native: unavailable")
        return
    native_ingest_rate, native_end_to_end_rate = measure_native(
        payload, args.frames, args.repeats)
    _column_ingest_rate, native_column_rate = measure_native(
        payload, args.frames, args.repeats, columnar=True)
    print(f"native core: {native_ingest_rate:,.0f} samples/s")
    print(f"native + Python dict export: {native_end_to_end_rate:,.0f} samples/s")
    print(f"native + column export: {native_column_rate:,.0f} samples/s")
    print(f"core speedup: {native_ingest_rate / python_rate:.2f}x")
    print(f"UI-compatible throughput ratio: "
          f"{native_end_to_end_rate / python_rate:.2f}x")
    print(f"columnar throughput ratio: {native_column_rate / python_rate:.2f}x")


if __name__ == "__main__":
    main()
