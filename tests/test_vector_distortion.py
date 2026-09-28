import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from core.vector_distortion import (
    PRESETS, DistortionParams, diagnose, estimate_params, simulate,
)


def _app():
    return QApplication.instance() or QApplication([])


def test_each_distortion_lands_on_its_theoretical_spectrum_order():
    ideal = simulate(DistortionParams())
    assert ideal.spectrum_pct[1] == pytest.approx(100.0, abs=1e-6)
    assert max(v for k, v in ideal.spectrum_pct.items() if k != 1) < 1e-6
    assert np.allclose(ideal.i_d, 0.0, atol=1e-9) and np.allclose(ideal.i_q, 1.0)

    offset = simulate(DistortionParams(offset_a_pct=5.0)).spectrum_pct
    assert offset[0] > 4.0 and offset[-1] < 0.01
    unbalance = simulate(DistortionParams(gain_b_pct=10.0)).spectrum_pct
    assert unbalance[-1] > 4.0 and unbalance[0] < 0.01
    harmonics = simulate(DistortionParams(h5_pct=6.0, h7_pct=3.0)).spectrum_pct
    assert harmonics[-5] == pytest.approx(6.0, abs=0.01)
    assert harmonics[7] == pytest.approx(3.0, abs=0.01)
    dead = simulate(DistortionParams(dead_time_pct=6.0)).spectrum_pct
    assert dead[-5] == pytest.approx(6.0, abs=0.01)
    assert dead[7] > 0 and dead[-11] > 0 and dead[13] > 0


def test_estimate_recovers_equivalent_parameters_from_geometry():
    truth = DistortionParams(offset_a_pct=3.0, offset_b_pct=-2.0, gain_b_pct=5.0,
                             phase_b_deg=-1.5, dead_time_pct=4.0)
    estimate = estimate_params(simulate(truth, samples=3000).shape)
    assert estimate.offset_a_pct == pytest.approx(3.0, abs=0.2)
    assert estimate.offset_b_pct == pytest.approx(-2.0, abs=0.2)
    assert estimate.gain_b_pct == pytest.approx(5.0, abs=0.4)
    assert estimate.phase_b_deg == pytest.approx(-1.5, abs=0.2)
    assert estimate.dead_time_pct == pytest.approx(4.0, abs=0.4)
    assert estimate_params(None) is None


def test_diagnosis_orders_causes_by_size():
    lines = diagnose(simulate(DistortionParams(offset_a_pct=8.0, gain_b_pct=3.0)).shape)
    assert lines[0].startswith("偏心")
    assert any(line.startswith("椭圆") for line in lines)
    assert "接近正圆" in diagnose(simulate(DistortionParams()).shape)[0]
    assert "无法分解" in diagnose(None)[0]


def test_lab_presets_and_measured_estimate_fill_sliders():
    _app()
    from pages.vector_distortion_lab import VectorDistortionLab

    truth = simulate(DistortionParams(offset_a_pct=4.0, gain_b_pct=6.0), samples=2000)
    lab = VectorDistortionLab(lambda: (1.5 * truth.alpha, 1.5 * truth.beta, "相电流 Clarke"))
    for index, params in enumerate(PRESETS.values()):
        lab._apply_preset(index)
        assert lab.params == params
    lab.apply_measured_estimate()
    assert lab.params.offset_a_pct == pytest.approx(4.0, abs=0.2)
    assert lab.params.gain_b_pct == pytest.approx(6.0, abs=0.4)
    assert "偏心" in lab._diagnosis.text()

    empty = VectorDistortionLab(lambda: ([], [], "相电流 Clarke"))
    empty.refresh_measured()
    assert not empty._btn_fit.isEnabled()
