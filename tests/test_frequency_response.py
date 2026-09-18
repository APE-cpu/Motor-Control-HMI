import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from pages.frequency_response_page import (
    FrequencyResponsePage, current_loop_frequency_response,
    estimate_welch_frf,
    firmware_current_loop_frequency_response,
)


def _app():
    return QApplication.instance() or QApplication([])


def test_current_loop_bode_matches_exact_bandwidth_design_without_delay():
    resistance = 0.59
    inductance = 0.66e-3
    bandwidth_hz = 700.0
    omega_c = 2.0 * math.pi * bandwidth_hz

    result = current_loop_frequency_response(
        resistance, inductance,
        inductance * omega_c, resistance * omega_c,
        delay_s=0.0, filter_cutoff_hz=None)

    assert result["gain_cross_hz"] == pytest.approx(700.0, rel=2e-3)
    assert result["phase_margin_deg"] == pytest.approx(90.0, abs=0.1)
    assert result["closed_bandwidth_hz"] == pytest.approx(700.0, rel=0.01)
    assert result["resonance_db"] < 0.01


def test_welch_h1_recovers_known_discrete_lowpass_response():
    rng = np.random.default_rng(20260917)
    sample_rate = 2000.0
    count = 16384
    x = rng.normal(size=count)
    alpha = 0.25
    y = np.zeros_like(x)
    for index in range(1, count):
        y[index] = alpha * x[index] + (1.0 - alpha) * y[index - 1]

    result = estimate_welch_frf(x, y, sample_rate, segment_length=1024)
    index = int(np.argmin(np.abs(result["frequency_hz"] - 100.0)))
    omega = 2.0 * math.pi * result["frequency_hz"][index] / sample_rate
    expected = alpha / abs(1.0 - (1.0 - alpha) * np.exp(-1j * omega))

    assert result["magnitude_db"][index] == pytest.approx(
        20.0 * math.log10(expected), abs=0.35)
    assert result["coherence"][index] > 0.98


def test_firmware_model_converts_mcsdk_digits_and_discrete_pi():
    result = firmware_current_loop_frequency_response(
        0.59, 0.66e-3, 16000.0, 24.0, 3.3, 0.01, 8.0,
        2323, 2077, 1024, 16384, 0.5, True, 20491)

    assert result["current_digit_per_amp"] == pytest.approx(1588.751515)
    assert result["amp_per_current_digit"] == pytest.approx(
        3.3 / (65536 * 0.01 * 8.0))
    assert result["volt_per_voltage_digit"] == pytest.approx(
        24.0 / (math.sqrt(3.0) * 32768.0))
    assert result["kp_physical"] == pytest.approx(1.52407349)
    assert result["ki_step_physical"] == pytest.approx(0.08516737)
    assert result["ki_continuous_equivalent"] == pytest.approx(1362.67785)
    assert result["filter_cutoff_hz"] == pytest.approx(2500.0, abs=0.1)
    assert result["phase_margin_deg"] > 70.0
    assert result["ms"] >= 1.0
    assert result["mt"] > 0.0
    assert len(result["sensitivity_db"]) == len(result["frequency_hz"])
    assert np.iscomplexobj(result["loop_complex"])
    assert result["nyquist_min_distance"] > 0.0
    assert 1.0 <= result["nyquist_min_distance_hz"] < 8000.0
    assert result["pole_domain"] == "z"
    assert len(result["poles"]) >= 2
    assert max(abs(result["poles"])) < 1.0
    assert "Thiran" in result["pole_model_note"]


def test_frequency_response_page_shows_formulas_and_margin():
    app = _app()
    page = FrequencyResponsePage()

    assert "G<sub>p</sub>" in page._formula_plant.text()
    assert "C(z)" in page._formula_controller.text()
    assert "T<sub>raw</sub>" in page._formula_loop.text()
    assert page._formula_controller.font().family() == "Cambria Math"
    assert page._formula_controller.font().pointSize() >= 15
    assert "相位裕度" in page._theory_metrics.text()
    assert "闭环−3 dB带宽" in page._theory_metrics.text()
    assert "1 A=1588" in page._theory_metrics.text()
    assert "临界点" in page._nyquist_metrics.text()
    assert page._nyquist_focus.isChecked()
    assert page._theory_plot_tabs.tabText(0) == "参数与公式"
    assert page._theory_plot_tabs.tabText(1) == "波特图"
    assert page._theory_plot_tabs.tabText(2) == "奈奎斯特图"
    assert page._theory_plot_tabs.tabText(3) == "零极点图"
    assert page._pole_table.rowCount() >= 2
    assert "单位圆内" in page._pole_metrics.text()
    assert page._theory_plot_tabs.tabText(4) == "灵敏度 S/T"
    assert page._theory_plot_tabs.tabText(5) == "根轨迹/参数扫描"
    assert "Ms=" in page._sensitivity_metrics.text()
    assert page._locus_table.rowCount() == 31
    assert "采样点稳定范围" in page._locus_metrics.text()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_frequency_response_page_can_sync_actual_firmware_values():
    app = _app()
    page = FrequencyResponsePage(config_provider=lambda: {
        "kp_cur_digit": 2400,
        "ki_cur_digit": 2100,
        "sample_rate_hz": 16000,
        "vbus_v": 23.6,
        "filter_enabled": True,
        "filter_alpha_q15": 20491,
        "source": "测试遥测",
    })

    assert page._kp_digit.value() == 2400
    assert page._ki_digit.value() == 2100
    assert page._vbus.value() == pytest.approx(23.6)
    assert page._feedback_filter.isChecked()
    assert page._filter_alpha_q15.value() == 20491
    assert "测试遥测" in page._config_source.text()
    page.close()
    page.deleteLater()
    app.processEvents()
