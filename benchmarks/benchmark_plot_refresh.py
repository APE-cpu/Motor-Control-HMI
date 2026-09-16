"""监控页高速波形刷新基准：模拟可配置 F1 输入下的 UI 批量绘制。"""
from __future__ import annotations

import argparse
import os
import statistics
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from pages.monitor_page import MonitorPage


def make_columns(count: int, offset: int = 0,
                 sample_rate_hz: int = 1000) -> dict:
    indexes = range(offset, offset + count)
    return {
        "count": count,
        "rate_hz": sample_rate_hz,
        "angle_deg": [float(index % 360) for index in indexes],
        "speed_rpm": [1200.0] * count,
        "iq_a": [float((index % 100) - 50) * 0.01 for index in indexes],
        "iqref_a": [0.5] * count,
        "ia_a": [float((index % 80) - 40) * 0.02 for index in indexes],
        "ib_a": [float((40 - index % 80)) * 0.02 for index in indexes],
        "vd_raw": [100.0] * count,
        "vq_raw": [200.0] * count,
        "vbus_v": [48.0] * count,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def measure(refresh_hz: int, seconds: int,
            sample_rate_hz: int = 1000) -> tuple[float, float, float]:
    app = QApplication.instance() or QApplication([])
    page = MonitorPage(CommManager())
    page._timer.stop()
    page.stop_visual_animations()
    frame = TelemetryFrame()
    frame.speed_actual = 1200.0
    frame.speed_target = 1200.0
    frame.current_actual = 0.5
    frame.current_target = 0.5
    frame.vdc = 48.0
    page._latest = frame
    samples_per_refresh = max(1, round(sample_rate_hz / refresh_hz))

    # 先填满 5000 点环形缓冲，测量稳态最坏情况。
    page._last_telemetry_time = time.time()
    page._on_high_rate_telemetry_columns(
        make_columns(5000, sample_rate_hz=sample_rate_hz))
    page._refresh()
    durations = []
    offset = 5000
    for _index in range(refresh_hz * seconds):
        page._last_telemetry_time = time.time()
        page._on_high_rate_telemetry_columns(
            make_columns(samples_per_refresh, offset, sample_rate_hz))
        offset += samples_per_refresh
        started = time.perf_counter()
        page._refresh()
        durations.append(time.perf_counter() - started)
        app.processEvents()
    page.close()
    milliseconds = [duration * 1000.0 for duration in durations]
    return (
        statistics.median(milliseconds),
        percentile(milliseconds, 0.95),
        sum(durations) / seconds * 100.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=3)
    parser.add_argument("--rates", type=int, nargs="+", default=[10, 30])
    parser.add_argument("--sample-rate", type=int, default=1000)
    args = parser.parse_args()
    for rate in args.rates:
        p50, p95, cpu = measure(rate, args.seconds, args.sample_rate)
        budget = 1000.0 / rate
        print(
            f"{rate} Hz UI @ {args.sample_rate} samples/s: "
            f"p50={p50:.2f} ms, p95={p95:.2f} ms, "
            f"budget={budget:.2f} ms, refresh CPU={cpu:.1f}%")


if __name__ == "__main__":
    main()
