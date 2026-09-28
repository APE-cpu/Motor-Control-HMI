import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from core.phasor_decomposition import analyze, find_pairs, load_capture, synthetic_capture
from core.vector_distortion import DistortionParams


def _app():
    return QApplication.instance() or QApplication([])


def _demo():
    return synthetic_capture(DistortionParams(offset_a_pct=4.0, gain_b_pct=5.0, dead_time_pct=3.0))


def test_decomposition_recovers_orders_and_common_mode_line():
    analysis = analyze(_demo(), 0.2, 1.8)
    assert analysis.fe_hz == pytest.approx(50.0, abs=0.01)
    keys = {c.key: c for c in analysis.components}
    assert analysis.components[0].key == "k+1"
    assert keys["k+1"].amp == pytest.approx(1.54, abs=0.02)
    assert keys["k+0"].amp > 0.05 and keys["k-1"].amp > 0.03 and keys["k-5"].amp > 0.03
    assert keys["k+0"].dq_freq_hz == pytest.approx(-50.0, abs=0.1)     # 零偏在 dq 中为 1 倍电频率
    pairs = find_pairs(analysis)
    common = next(p for p in pairs if abs(p.freq_hz - 654.0) < 1.0)
    assert common.linearity > 0.95                                      # 两路共模 → 直线摆动
    assert common.axis_deg == pytest.approx(60.0, abs=3.0)
    assert analysis.residual_pct < 2.0


def test_reconstruction_matches_measured_and_series_is_consistent():
    analysis = analyze(_demo(), 0.5, 1.0, block_s=0.1)
    t = 0.7321
    total = sum(analysis.vectors_at(t))
    assert abs(total - analysis.measured_at(t)) < 0.05
    comp = analysis.components[1]
    assert analysis.series(comp, np.array([t]))[0] == pytest.approx(
        analysis.vectors_at(t, [comp])[0])


def test_load_wide_and_long_capture_formats(tmp_path):
    capture = _demo()
    wide = tmp_path / "RLS辨识数据.csv"
    rows = ["sample_index,time_s,angle_deg,ia_a,ib_a,speed_rpm"]
    angle = np.degrees(capture.theta) % 360.0
    for i in range(0, 4000):
        rows.append(f"{i},{capture.time[i]:.9f},{angle[i]:.5f},{capture.ia[i]:.6f},"
                    f"{capture.ib[i]:.6f},750")
    wide.write_text("﻿" + "\n".join(rows), encoding="utf-8")
    loaded = load_capture(wide)
    assert loaded.rate_hz == pytest.approx(16000.0, rel=1e-3)
    assert loaded.theta is not None and loaded.theta[-1] > loaded.theta[0]   # 已展开
    assert analyze(loaded, 0.0, loaded.time[-1]).fe_hz == pytest.approx(50.0, abs=0.1)

    long_path = tmp_path / "原始数据.csv"
    lines = ["channel,time_s,series,value,sampling_rate_hz,source_filter,display_filter,unit"]
    for i in range(0, 3200):
        t = capture.time[i]
        lines.append(f"phase_current,{t:.6f},Ia,{capture.ia[i]:.6f},16000,,,A")
        lines.append(f"phase_current,{t:.6f},Ib,{capture.ib[i]:.6f},16000,,,A")
        lines.append(f"electrical_angle,{t:.6f},高速电角度,{angle[i]:.4f},16000,,,°")
    long_path.write_text("\n".join(lines), encoding="utf-8")
    loaded = load_capture(long_path)
    assert loaded.theta is not None and loaded.ia.size == 3200

    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_capture(bad)


def test_playback_widget_pairs_toggles_and_plays():
    _app()
    from pages.phasor_playback import PhasorPlayback

    widget = PhasorPlayback(lambda: DistortionParams(offset_a_pct=4.0))
    widget.load_demo()
    analysis = widget.analysis
    assert analysis is not None
    labels = [item["label"] for item in widget._items]
    assert any("654" in label and "直线摆动" in label for label in labels)
    widget._chk_pairs.setChecked(False)
    assert len(widget._items) == len(analysis.components)
    widget.set_component_enabled("k+1", False)
    assert all(item["members"][0].key != "k+1" for item in widget._items)
    widget._view.setCurrentIndex(1)
    widget._btn_play.setChecked(True)
    before = widget._t
    widget._advance()
    assert widget._t != before
    widget.seek(float(analysis.time[-1]) + 10.0)       # 超出窗口也不报错
    widget._btn_play.setChecked(False)
