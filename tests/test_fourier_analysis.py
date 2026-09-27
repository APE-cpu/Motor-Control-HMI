import math
import os
import csv
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from pages.fourier_page import (
    FourierAnalysisPage,
    OrderLmsDialog,
    compute_spectrum,
    compute_order_lms,
)
from pages.monitor_page import MonitorPage
from widgets.trend_curve import TrendCurve


def _app():
    return QApplication.instance() or QApplication([])


def test_fft_recovers_frequency_and_peak_amplitude():
    sample_rate = 16000.0
    count = 4096
    frequency = 250.0  # 恰好落在 FFT 频点上
    time_s = np.arange(count) / sample_rate
    values = 2.0 * np.sin(2.0 * np.pi * frequency * time_s) + 0.35

    result = compute_spectrum(values, sample_rate, "hann")

    assert math.isclose(result["fundamental_hz"], frequency, abs_tol=1e-9)
    assert math.isclose(result["fundamental_amplitude"], 2.0, rel_tol=2e-3)
    assert result["thd_percent"] < 0.05
    assert math.isclose(result["dc"], 0.35, abs_tol=1e-9)


def test_fft_reports_dc_ripple_metrics_without_using_thd_semantics():
    sample_rate = 16000.0
    count = 16000
    time_s = np.arange(count) / sample_rate
    values = 10.0 + 0.5 * np.sin(2.0 * np.pi * 200.0 * time_s)

    result = compute_spectrum(values, sample_rate, "hann")

    assert math.isclose(result["dc"], 10.0, rel_tol=1e-9)
    assert math.isclose(result["fundamental_hz"], 200.0, abs_tol=1e-9)
    assert math.isclose(result["peak_to_peak"], 1.0, rel_tol=1e-9)
    assert math.isclose(result["rms"], 0.5 / math.sqrt(2), rel_tol=1e-6)
    assert math.isclose(
        result["ripple_factor_percent"], 5.0 / math.sqrt(2), rel_tol=1e-6)


def test_fft_can_show_dc_without_mistaking_window_leakage_for_fundamental():
    sample_rate = 16000.0
    count = 4096
    frequency = 250.0
    time_s = np.arange(count) / sample_rate
    values = 10.0 + 0.5 * np.sin(2.0 * np.pi * frequency * time_s)

    result = compute_spectrum(
        values, sample_rate, "hann", remove_dc=False)

    assert result["remove_dc"] is False
    assert math.isclose(result["amplitudes"][0], 10.0, rel_tol=1e-9)
    assert math.isclose(result["fundamental_hz"], frequency, abs_tol=1e-9)
    assert math.isclose(result["fundamental_amplitude"], 0.5, rel_tol=2e-3)


def test_curve_replaces_realtime_stats_with_processing_annotation():
    _app()
    curve = TrendCurve("相电流", {"Ia": "#fff"}, buffer_size=128)
    curve.set_source_processing(
        "无滤波；16 kHz FOC每周期原始点", 16000)
    curve.append_columns({"Ia": [0.0, 1.0, 0.0, -1.0]}, 1 / 16000)
    curve.set_smoothing(3)

    assert curve._stats_label.isHidden()
    assert "16 kHz" in curve._processing_label.text()
    assert "3点居中移动平均（仅显示）" in curve._processing_label.text()
    snapshot = curve.raw_snapshot("Ia")
    assert snapshot["values"] == [0.0, 1.0, 0.0, -1.0]
    assert snapshot["sample_rate_hz"] == 16000.0
    assert "FFT仍使用原始缓冲" in snapshot["display_filter"]
    curve.close()


def test_fourier_page_accepts_current_buffer_provider():
    _app()
    sample_rate = 1000.0
    values = np.sin(2 * np.pi * 50 * np.arange(1000) / sample_rate).tolist()

    def provider(key):
        assert key == "ia"
        return {
            "values": values,
            "sample_rate_hz": sample_rate,
            "source_processing": "无滤波",
            "display_filter": "FFT使用原始缓冲",
            "unit": "A",
        }

    page = FourierAnalysisPage(provider, [("相电流 Ia", "ia")])
    page._analyze_selected()

    assert "50.000 Hz" in page._metric_fund.text()
    assert "无滤波" in page._processing.text()
    assert "50.000 Hz" in page._peak_summary.text()
    page.close()


def test_stationary_frame_snapshots_use_mcsdk_clarke_and_actuation_angle():
    class RawCurve:
        def __init__(self, columns):
            self.columns = columns

        def raw_snapshot(self, name):
            return {
                "values": self.columns[name], "times": [0.0, 0.001],
                "sample_rate_hz": 1000.0, "source_processing": "F1原始点",
                "display_filter": "FFT使用原始缓冲",
            }

    monitor = SimpleNamespace(
        _c_phase_current=RawCurve({"Ia": [1.0, 0.0], "Ib": [0.0, 1.0]}),
        _c_voltage=RawCurve({"Vd": [1.0, 1.0], "Vq": [2.0, 2.0]}),
        _voltage_angles_deg=[0.0, 90.0],
        _high_rate_voltage_is_applied=True,
    )
    ibeta = MonitorPage.fourier_snapshot(monitor, "i_beta")
    valpha = MonitorPage.fourier_snapshot(monitor, "v_alpha")
    vbeta = MonitorPage.fourier_snapshot(monitor, "v_beta")

    assert ibeta["values"] == pytest.approx(
        [-1 / math.sqrt(3), -2 / math.sqrt(3)])
    assert valpha["values"] == pytest.approx([2.0, 1.0])
    assert vbeta["values"] == pytest.approx([1.0, -2.0])
    assert "执行角" in valpha["source_processing"]


def test_fourier_harmonic_switches_use_speed_and_electrical_angle():
    _app()
    rate = 1000.0
    times = np.arange(2000) / rate
    signals = {
        "phase_ia": np.sin(2 * np.pi * 50 * times),
        "speed": np.full(times.size, 600.0),
        "angle": (40.0 * 360.0 * times) % 360.0,
    }

    def provider(key):
        return {
            "times": times.tolist(), "values": signals[key].tolist(),
            "sample_rate_hz": rate, "unit": "A", "analysis_kind": "ac",
        }

    page = FourierAnalysisPage(provider, [
        ("相电流 Ia", "phase_ia"), ("实际转速", "speed"),
        ("高速电角度", "angle")])
    page._category_combo.setCurrentIndex(
        page._category_combo.findData("电流"))
    page._show_speed_harmonics.setChecked(True)
    page._show_angle_harmonics.setChecked(True)
    page._analyze_selected()

    assert page._harmonic_hz["speed"] == pytest.approx(10.0)
    assert page._harmonic_hz["angle"] == pytest.approx(40.0, rel=1e-3)
    for order in (1, 2, 3):
        assert page._harmonic_lines[("speed", order)].value() == pytest.approx(10 * order)
        assert page._harmonic_lines[("angle", order)].value() == pytest.approx(40 * order, rel=1e-3)
        assert page._harmonic_lines[("speed", order)].isVisible()
    assert page._speed_harmonic_order.maximum() == 50  # Nyquist 限制
    assert page._angle_harmonic_order.maximum() == 12
    page._speed_harmonic_order.setValue(8)
    page._angle_harmonic_order.setValue(6)
    assert page._harmonic_lines[("speed", 8)].value() == pytest.approx(80)
    assert page._harmonic_lines[("angle", 6)].value() == pytest.approx(240, rel=1e-3)
    assert page._harmonic_lines[("speed", 8)].isVisible()
    page._max_freq.setValue(100)
    assert page._speed_harmonic_order.maximum() == 10
    assert page._angle_harmonic_order.maximum() == 2
    assert not page._harmonic_lines[("angle", 3)].isVisible()
    assert all(not line.isVisible() or line.value() <= 100
               for line in page._harmonic_lines.values())
    page._show_speed_harmonics.setChecked(False)
    assert not page._harmonic_lines[("speed", 1)].isVisible()
    assert page._harmonic_lines[("angle", 1)].isVisible()
    page.close()


def test_csv_sources_are_grouped_by_file_and_signal_category(tmp_path):
    _app()
    path = tmp_path / "capture.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("channel", "time_s", "series", "value",
                         "sampling_rate_hz", "unit"))
        for index in range(64):
            t = index / 1000.0
            writer.writerow(("speed", t, "实际", 600.0, 1000, "rpm"))
            writer.writerow(("electrical_angle", t, "高速电角度",
                             (40 * 360 * t) % 360, 1000, "°"))
            writer.writerow(("stationary_current", t, "Iα",
                             math.sin(2 * math.pi * 50 * t), 1000, "A"))
    page = FourierAnalysisPage(lambda _key: {}, [("相电流 Ia", "phase_ia")])
    page.load_csv(str(path))

    assert page._source_combo.currentData() == "loaded"
    assert page._file_combo.currentData() == str(path.resolve())
    assert page._category_combo.count() == 3
    page._category_combo.setCurrentIndex(
        page._category_combo.findData("电流"))
    assert page._signal_combo.count() == 1
    assert "Iα" in page._signal_combo.currentText()
    page._show_speed_harmonics.setChecked(True)
    page._show_angle_harmonics.setChecked(True)
    page._analyze_selected()
    assert page._last_result["sample_count"] == 64
    assert "交流量模式" in page._processing.text()
    assert page._harmonic_hz["speed"] == pytest.approx(10.0)
    assert page._harmonic_hz["angle"] == pytest.approx(40.0, rel=1e-3)
    page.load_csv(str(path))
    assert len([item for item in page._signal_entries
                if item["file"] == str(path.resolve())]) == 3
    page.close()


def test_fourier_page_displays_full_input_and_highlights_selected_time_segment():
    _app()
    sample_rate = 500.0
    count = 2048
    times = (10.0 + np.arange(count) / sample_rate).tolist()
    values = np.sin(2 * np.pi * 8.0 * np.arange(count) / sample_rate).tolist()
    page = FourierAnalysisPage(lambda _key: {
        "times": times,
        "values": values,
        "sample_rate_hz": sample_rate,
        "source_processing": "F1速度按500 Hz更新节拍抽取",
        "display_filter": "FFT使用原始缓冲",
        "unit": "rpm",
        "analysis_kind": "dc",
    }, [("实际转速", "speed")])
    page._points_combo.setCurrentIndex(1)  # 最新1024点

    page._analyze_selected()

    full_x, full_y = page._input_full_curve.getData()
    selected_x, selected_y = page._input_selected_curve.getData()
    assert len(full_x) == len(full_y) == 2048
    assert len(selected_x) == len(selected_y) == 1024
    assert selected_x[0] == pytest.approx(times[1024])
    assert selected_x[-1] == pytest.approx(times[-1])
    assert "FFT区间" in page._metric_time.text()
    assert "2.048 s" in page._metric_time.text()
    assert "蓝色区间" in page._input_plot.titleLabel.text
    page.close()


def test_fourier_page_recomputes_fft_from_dragged_blue_interval():
    _app()
    sample_rate = 500.0
    duration_s = 8.0
    times = np.arange(int(sample_rate * duration_s)) / sample_rate
    values = np.where(
        times < 4.0,
        np.sin(2 * np.pi * 8.0 * times),
        np.sin(2 * np.pi * 20.0 * times),
    )
    page = FourierAnalysisPage(lambda _key: {
        "times": times.tolist(),
        "values": values.tolist(),
        "sample_rate_hz": sample_rate,
        "source_processing": "测试信号",
        "display_filter": "无",
        "unit": "rpm",
        "analysis_kind": "dc",
    }, [("实际转速", "speed")])
    page._analyze_selected()

    page._selection_region.setRegion((4.0, 8.0))
    page._analyze_dragged_region()

    assert page._use_plot_interval.isChecked()
    assert page._last_result["sample_count"] == 2000
    assert page._last_result["fundamental_hz"] == pytest.approx(20.0)
    selected_x, _ = page._input_selected_curve.getData()
    assert selected_x[0] == pytest.approx(4.0)
    assert selected_x[-1] == pytest.approx(7.998)
    assert "t=4.000～7.998 s" in page._metric_time.text()
    page._open_order_lms()
    assert len(page._order_lms_dialog._values) == 2000
    assert page._order_lms_dialog._times[0] == pytest.approx(4.0)
    page._order_lms_dialog.close()
    page.close()


def test_fourier_point_shortcut_exits_dragged_interval_mode():
    _app()
    sample_rate = 500.0
    values = np.sin(2 * np.pi * 8 * np.arange(4096) / sample_rate)
    page = FourierAnalysisPage(lambda _key: {
        "values": values.tolist(),
        "sample_rate_hz": sample_rate,
        "unit": "rpm",
    }, [("实际转速", "speed")])
    page._analyze_selected()
    page._use_plot_interval.setChecked(True)

    page._points_combo.setCurrentIndex(1)

    assert not page._use_plot_interval.isChecked()
    assert page._selection_region.movable
    page.close()


def test_fourier_page_auto_mode_uses_dc_ripple_presentation():
    _app()
    sample_rate = 1000.0
    values = (24.0 + 0.2 * np.sin(
        2 * np.pi * 100 * np.arange(1000) / sample_rate)).tolist()

    page = FourierAnalysisPage(lambda _key: {
        "values": values,
        "sample_rate_hz": sample_rate,
        "source_processing": "无滤波",
        "display_filter": "FFT使用原始缓冲",
        "unit": "V",
        "analysis_kind": "dc",
    }, [("母线电压", "vdc")])
    page._analyze_selected()

    assert "主振荡：100.000 Hz" in page._metric_fund.text()
    assert "平均值：24" in page._metric_amp.text()
    assert "纹波率" in page._metric_thd.text()
    assert "不计算THD" in page._processing.text()
    page.close()


def test_order_lms_removes_selected_mechanical_orders_and_preserves_dc():
    sample_rate = 500.0
    count = 4096
    mean_speed_rpm = 500.0
    mechanical_hz = mean_speed_rpm / 60.0
    time_s = np.arange(count) / sample_rate
    values = (
        mean_speed_rpm
        + 14.0 * np.sin(2 * np.pi * mechanical_hz * time_s + 0.35)
        + 6.0 * np.sin(2 * np.pi * 2 * mechanical_hz * time_s - 0.7)
        + 2.0 * np.sin(2 * np.pi * 3 * mechanical_hz * time_s + 1.1)
    )

    result = compute_order_lms(
        values, sample_rate, mechanical_hz, orders=(1, 2, 3),
        step_size=0.08, epochs=4)

    assert result["base_frequency_hz"] == pytest.approx(mechanical_hz)
    assert np.mean(result["filtered_values"]) == pytest.approx(
        np.mean(values), abs=1e-9)
    assert result["before_rms"] > 10.0
    assert result["after_rms"] < 0.25
    assert result["attenuation_percent"] > 97.0
    amplitudes = {item["order"]: item["amplitude"]
                  for item in result["order_components"]}
    assert amplitudes[1] == pytest.approx(14.0, rel=0.02)
    assert amplitudes[2] == pytest.approx(6.0, rel=0.03)
    assert amplitudes[3] == pytest.approx(2.0, rel=0.08)


def test_order_lms_rejects_orders_above_nyquist():
    values = np.sin(2 * np.pi * 8.0 * np.arange(1000) / 100.0)

    with pytest.raises(ValueError, match="Nyquist"):
        compute_order_lms(
            values, 100.0, 20.0, orders=(1, 2, 3),
            step_size=0.05, epochs=2)


def test_order_lms_keeps_an_unselected_frequency_in_the_residual():
    sample_rate = 500.0
    count = 4096
    time_s = np.arange(count) / sample_rate
    base_hz = 500.0 / 60.0
    unrelated_hz = 12.0
    values = (
        500.0
        + 12.0 * np.sin(2 * np.pi * base_hz * time_s)
        + 3.0 * np.sin(2 * np.pi * unrelated_hz * time_s + 0.2)
    )

    result = compute_order_lms(
        values, sample_rate, base_hz, orders=(1,),
        step_size=0.05, epochs=4)
    unrelated_spectrum = compute_spectrum(
        3.0 * np.sin(2 * np.pi * unrelated_hz * time_s + 0.2),
        sample_rate, "hann", True)
    residual_spectrum = compute_spectrum(
        result["filtered_values"], sample_rate, "hann", True)

    assert residual_spectrum["fundamental_hz"] == pytest.approx(
        unrelated_hz, abs=sample_rate / count)
    assert residual_spectrum["fundamental_amplitude"] == pytest.approx(
        unrelated_spectrum["fundamental_amplitude"], rel=0.01)


def test_fourier_page_exposes_order_lms_without_adding_a_fixed_panel():
    _app()
    page = FourierAnalysisPage(lambda _key: {
        "values": [500.0] * 128,
        "sample_rate_hz": 500.0,
        "unit": "rpm",
        "analysis_kind": "dc",
    }, [("实际转速", "speed")])

    assert page._order_lms_btn.text() == "阶次 LMS…"
    assert not hasattr(page, "_order_lms_panel")
    page.close()


def test_order_lms_dialog_can_hide_each_time_and_spectrum_curve():
    _app()
    sample_rate = 500.0
    times = np.arange(256) / sample_rate
    values = 500.0 + np.sin(2 * np.pi * 8.0 * times)
    dialog = OrderLmsDialog(
        "实际转速", times, values, sample_rate, "rpm")

    pairs = (
        (dialog._show_raw, dialog._raw_curve),
        (dialog._show_learned, dialog._learned_curve),
        (dialog._show_filtered, dialog._filtered_curve),
        (dialog._show_before_spectrum, dialog._before_spectrum),
        (dialog._show_after_spectrum, dialog._after_spectrum),
    )
    for checkbox, curve in pairs:
        assert checkbox.isChecked()
        assert curve.isVisible()
        checkbox.setChecked(False)
        assert not curve.isVisible()
        checkbox.setChecked(True)
        assert curve.isVisible()
    dialog.close()
