import math
import struct

import pytest

from communications.native_telemetry import (
    NativeTelemetryProcessor, native_telemetry_available,
)


pytestmark = pytest.mark.skipif(
    not native_telemetry_available(), reason="尚未构建 C++ 遥测解析器")


def test_F1原生解析兼容12_16_22字节格式():
    processor = NativeTelemetryProcessor()
    processor.set_f1_rate_hz(16000)
    compact = struct.pack("<IHhhh", 10, 32768, -20, 100, 90)
    phase = struct.pack("<IHhhhhh", 11, 16384, 21, 101, 91, -300, 250)
    voltage = struct.pack(
        "<IHhhhhhhhH", 12, 8192, 22, 102, 92,
        -301, 251, -1200, 1300, 48)

    assert processor.ingest(0xF1, compact)
    assert processor.ingest(0xF1, phase)
    assert processor.ingest(0xF1, voltage)
    samples = processor.drain_f1()

    assert [sample["tick_ms"] for sample in samples] == [10, 11, 12]
    assert all(sample["rate_hz"] == 16000 for sample in samples)
    assert samples[0]["angle_deg"] == pytest.approx(180.0)
    assert "ia_a" not in samples[0]
    assert samples[1]["ia_a"] == pytest.approx(-300 * 0.000629)
    assert "vd_raw" not in samples[1]
    assert samples[2]["vd_raw"] == -1200
    assert samples[2]["vq_raw"] == 1300
    assert samples[2]["vbus_v"] == 48.0


def test_F1批量解析与环形队列丢旧留新():
    processor = NativeTelemetryProcessor(max_f1_samples=3)
    payload = b"".join(
        struct.pack("<IHhhhhh", index, index, index, index, index, index, -index)
        for index in range(5)
    )

    assert processor.ingest(0xF1, payload)
    samples = processor.drain_f1()
    stats = processor.stats()

    assert [sample["tick_ms"] for sample in samples] == [2, 3, 4]
    assert stats["f1_frames"] == 1
    assert stats["f1_samples"] == 5
    assert stats["dropped_samples"] == 2


def test_F1非法长度计入解析错误且不产生样本():
    processor = NativeTelemetryProcessor()
    assert processor.ingest(0xF1, b"bad")
    assert processor.drain_f1() == []
    assert processor.stats()["parse_errors"] == 1


def test_F2原生解析标定窗口和VDDA():
    processor = NativeTelemetryProcessor()
    base = struct.pack(
        "<IHHHHBHHHH", 2000, 16400, 16390, 32800, 32780,
        4, 2600, 2610, 2620, 5249)
    calibration = struct.pack("<HHHHH", 16380, 16420, 16370, 16410, 3295)

    assert processor.ingest(0xF2, base + calibration)
    sample = processor.drain_f2()[0]

    assert sample["tick_ms"] == 2000
    assert sample["sector"] == 4
    assert sample["cal_adc1_pp"] == 40
    assert sample["cal_adc2_pp"] == 40
    assert sample["vdda_v"] == pytest.approx(3.295)
    assert sample["adc1_v"] == pytest.approx(16400 * 3.3 / 32768.0)


def test_F3原生解析SI系数并反解电机参数():
    processor = NativeTelemetryProcessor()
    processor.set_rls_coefficients_si(True)
    theta_d = [0.9, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0]
    theta_q = [0.8, 0.0, 0.0, 0.0, 0.0, 0.04, 0.0]
    payload = struct.pack(
        "<IIff14f", 123, 456, 0.125, 99.0, *(theta_d + theta_q))

    assert processor.ingest(0xF3, payload)
    sample = processor.drain_f3()[0]

    assert sample["tick_ms"] == 123
    assert sample["updates"] == 456
    assert sample["innov_rms_a"] == pytest.approx(0.125)
    assert math.isnan(sample["innov_rms_digit"])
    assert sample["b_dd0_si"] == pytest.approx(0.05)
    assert sample["b_qq0_si"] == pytest.approx(0.04)
    assert sample["ld_mh"] == pytest.approx(1.25)
    assert sample["lq_mh"] == pytest.approx(1.5625)


def test_F2_F3非法长度均计入解析错误():
    processor = NativeTelemetryProcessor()
    assert processor.ingest(0xF2, b"bad")
    assert processor.ingest(0xF3, b"bad")
    stats = processor.stats()
    assert stats["f2_frames"] == 1
    assert stats["f3_frames"] == 1
    assert stats["parse_errors"] == 2


def _f4_chunk(total, start, samples):
    return struct.pack("<HHH", total, start, len(samples)) + b"".join(
        struct.pack("<hhH", ia, ib, angle)
        for ia, ib, angle in samples
    )


def test_F4乱序分块和重复块在Cpp中重组一次():
    processor = NativeTelemetryProcessor()
    tail = _f4_chunk(5, 3, [(13, -13, 300), (14, -14, 400)])
    head = _f4_chunk(5, 0, [(10, -10, 0), (11, -11, 100), (12, -12, 200)])

    assert processor.ingest(0xF4, tail)
    assert processor.ingest(0xF4, tail)
    assert processor.drain_bursts() == []
    assert processor.ingest(0xF4, head)
    captures = processor.drain_bursts()

    assert len(captures) == 1
    assert captures[0] == {
        "n": 5,
        "ia": [10, 11, 12, 13, 14],
        "ib": [-10, -11, -12, -13, -14],
        "ang": [0, 100, 200, 300, 400],
    }
    assert processor.stats()["completed_bursts"] == 1


def test_F4越界块被拒绝且可由后续合法抓取恢复():
    processor = NativeTelemetryProcessor()
    assert processor.ingest(0xF4, _f4_chunk(2, 1, [(1, 2, 3), (4, 5, 6)]))
    assert processor.stats()["parse_errors"] == 1
    processor.reset_burst()
    assert processor.ingest(0xF4, _f4_chunk(1, 0, [(7, 8, 9)]))
    assert processor.drain_bursts()[0]["ia"] == [7]
