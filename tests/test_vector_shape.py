import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.vector_shape import clarke, trajectory_shape


def _circle(n=720, radius=2.0):
    th = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return th, radius * np.cos(th), radius * np.sin(th)


def test_perfect_circle_has_no_distortion():
    _, x, y = _circle()
    shape = trajectory_shape(x, y)
    assert shape.mean_radius == pytest.approx(2.0, rel=1e-6)
    assert shape.eccentric_pct < 0.01
    assert shape.ellipse_pct < 0.01
    assert shape.triangle_pct < 0.01


def test_offset_vector_reads_as_eccentric():
    _, x, y = _circle(radius=2.0)
    shape = trajectory_shape(x + 0.1, y)          # 零偏 0.1 A
    assert shape.eccentric_pct == pytest.approx(5.0, abs=0.1)
    assert shape.ellipse_pct < 0.2


def test_negative_sequence_reads_as_ellipse_with_axis():
    th, _, _ = _circle()
    z = 2.0 * np.exp(1j * th) + 0.06 * np.exp(-1j * th) * np.exp(1j * np.deg2rad(60))
    shape = trajectory_shape(z.real, z.imag)
    assert shape.ellipse_pct == pytest.approx(3.0, abs=0.1)   # I₋/I₊
    assert shape.eccentric_pct < 0.2
    assert shape.ellipse_axis_deg == pytest.approx(30.0, abs=1.0)


def test_negative_second_harmonic_reads_as_triangle():
    th, _, _ = _circle()
    z = 2.0 * np.exp(1j * th) + 0.08 * np.exp(-2j * th)
    shape = trajectory_shape(z.real, z.imag)
    assert shape.triangle_pct == pytest.approx(4.0, abs=0.1)
    assert shape.ellipse_pct < 0.2


def test_partial_arc_or_too_few_points_is_rejected():
    th = np.linspace(0.0, np.pi, 720)
    assert trajectory_shape(np.cos(th), np.sin(th)) is None
    _, x, y = _circle(n=50)
    assert trajectory_shape(x, y) is None


def test_clarke_gain_error_produces_expected_negative_sequence():
    th = np.linspace(0.0, 2.0 * np.pi, 720, endpoint=False)
    ia = 2.0 * np.cos(th)
    ib = 1.03 * 2.0 * np.cos(th - 2.0 * np.pi / 3.0)   # B 相增益 +3%
    shape = trajectory_shape(*clarke(ia, ib))
    assert shape.ellipse_pct == pytest.approx(3.0 / np.sqrt(3.0), abs=0.05)


def test_vector_page_clarke_mode_uses_phase_currents():
    pytest.importorskip("pyqtgraph")
    from PySide6.QtWidgets import QApplication

    from communications.comm_manager import CommManager
    from pages.vector_page import VectorPage

    QApplication.instance() or QApplication([])
    page = VectorPage(CommManager())
    page.show()
    page._chk_enabled.setChecked(True)
    page._timer.stop()
    page._cmb_source.setCurrentIndex(page._cmb_source.findData("clarke"))

    n = 16000
    th = np.linspace(0.0, 4.0 * np.pi, n, endpoint=False)
    columns = {
        "rate_hz": 16000,
        "angle_deg": np.rad2deg(th) % 360.0,
        "iq_a": np.full(n, 2.0),
        "ia_a": 2.0 * np.cos(th) + 0.1,                     # A 相零偏 0.1 A
        "ib_a": 2.0 * np.cos(th - 2.0 * np.pi / 3.0),
    }
    page._on_high_rate_columns(columns)
    xs = np.asarray(page._i_plot._xs)
    assert xs.size == 1000
    assert xs.mean() == pytest.approx(0.1, abs=0.01)      # 零偏 → 圆心右移
    page._update_shape_label()
    # Ia 零偏 0.1 A → αβ 零偏 (0.1, 0.1/√3)，|O| = 0.115 A = 半径的 5.8%
    assert "偏心 5.8%" in page._shape_label.text()

    # 数据流不带 Ia/Ib 时提示，而不是悄悄画 iq 重建的圆
    page._on_clear()
    page._on_high_rate_columns({k: columns[k] for k in ("rate_hz", "angle_deg", "iq_a")})
    assert len(page._i_plot._xs) == 0
    page._update_shape_label()
    assert "不含 Ia/Ib" in page._shape_label.text()

    # 切回 iq 重建：余辉清空，按 iq 画圆
    page._cmb_source.setCurrentIndex(page._cmb_source.findData("iq"))
    assert len(page._i_plot._xs) == 0
    page._on_high_rate_columns(columns)
    assert np.hypot(page._i_plot._xs[-1], page._i_plot._ys[-1]) == pytest.approx(2.0)
    page.close()
    page.deleteLater()
