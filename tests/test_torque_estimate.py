import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol_v2 import MessageType, V2Frame
from config.config import TORQUE_CONSTANT_NM_PER_A as KT
from core.torque_estimate import F1TorqueEstimator, torque_from_iq


def _app():
    return QApplication.instance() or QApplication([])


def test_estimator_averages_16khz_iq_into_500hz_torque_across_batches():
    est = F1TorqueEstimator(kt=0.035)
    first = est.push([2.0] * 100, 16000)
    assert est.block_samples == 32
    assert est.interval_s == pytest.approx(0.002)
    assert first == pytest.approx([0.07] * 3)          # 100 点 → 3 块，余 4 点
    second = est.push([2.0] * 28, 16000)                # 4 + 28 = 32 凑满一块
    assert second == pytest.approx([0.07])


def test_estimator_averages_out_ripple_inside_block():
    est = F1TorqueEstimator(kt=0.035)
    iq = [2.0 + (0.3 if i % 2 else -0.3) for i in range(32)]
    assert est.push(iq, 16000) == pytest.approx([0.07])


def test_estimator_rate_change_drops_partial_block():
    est = F1TorqueEstimator(kt=1.0)
    assert est.push([5.0] * 10, 16000) == []
    # 1 kHz → 每 2 点一块；16 kHz 留下的 10 点余数不能混进来
    assert est.push([1.0] * 4, 1000) == pytest.approx([1.0, 1.0])
    assert est.interval_s == pytest.approx(0.002)
    est.push([], 200)
    assert est.block_samples == 1                       # 低于 500 Hz 时逐点输出


def test_v2_telemetry_torque_is_computed_on_host_not_taken_from_firmware():
    comm = CommManager()
    payload = json.dumps({"current_actual": 2.0, "current_target": 2.5,
                          "torque_actual": 9.9, "torque_target": 9.9})
    frame = comm._parse_v2_telemetry(
        V2Frame(MessageType.TELEMETRY, payload=payload.encode("utf-8")))
    assert frame.torque_actual == pytest.approx(2.0 * KT)
    assert frame.torque_target == pytest.approx(2.5 * KT)
    assert torque_from_iq(2.0) == pytest.approx(2.0 * KT)


def test_monitor_torque_curve_uses_f1_iq_blocks():
    _app()
    from pages.monitor_page import MonitorPage

    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._latest.current_actual = 2.0
    page._last_telemetry_time = time.time()
    count = 64
    page._on_high_rate_telemetry_columns({
        "count": count, "rate_hz": 16000,
        "angle_deg": [float(i) for i in range(count)],
        "speed_rpm": [800.0] * count,
        "iq_a": [2.0] * count, "iqref_a": [2.0] * count,
        "ia_a": [0.0] * count, "ib_a": [0.0] * count,
        "vd_raw": [0.0] * count, "vq_raw": [0.0] * count,
        "vbus_v": [24.0] * count,
    })
    page._refresh()

    torque = list(page._c_torque._buffers["实际"])
    assert torque == pytest.approx([2.0 * KT] * 2)       # 64 点 → 2 个 500 Hz 转矩点
    assert page._c_torque._sample_rate_hz == pytest.approx(500.0)
    assert page._torque_curve_source == "f1"
    assert page._torque_display == pytest.approx(2.0 * KT)
    page.close()


def test_monitor_torque_falls_back_to_one_point_per_f0_frame():
    _app()
    from pages.monitor_page import MonitorPage

    page = MonitorPage(CommManager())
    page._timer.stop()
    frame = TelemetryFrame()
    frame.speed_actual = 800.0
    frame.current_actual = 2.0
    frame.torque_actual = 2.0 * KT
    page._on_telemetry(frame)
    page._refresh()
    page._refresh()                                       # 同一 F0 帧不重复写点
    assert list(page._c_torque._buffers["实际"]) == pytest.approx([2.0 * KT])
    assert page._torque_curve_source == "f0"
    page.close()
