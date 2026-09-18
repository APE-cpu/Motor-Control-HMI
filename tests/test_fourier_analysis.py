import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from pages.fourier_page import FourierAnalysisPage, compute_spectrum
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
