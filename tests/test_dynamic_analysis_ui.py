import os
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import QSettings

from analysis_dynamic import demo_snapshot
from pages.dynamic_analysis_dialog import DynamicAnalysisDialog
from pages.algorithm_validation_page import AlgorithmValidationPage
from pages.fourier_page import FourierAnalysisPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_palette_settings(app, tmp_path):
    from ui_theme import appearance_manager
    manager = appearance_manager()
    original = manager.settings
    manager.settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.IniFormat)
    yield
    manager.settings = original


def finish(app, widget):
    deadline = time.monotonic()+20
    while widget.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert widget.worker is None
    assert widget.result is not None, widget.status.text()


def test_all_maps_render_and_worker_released(app):
    snapshot, speed = demo_snapshot()
    dialog = DynamicAnalysisDialog("test", snapshot, {"rpm": lambda: speed})
    dialog.show()
    for mode in range(3):
        dialog.mode.setCurrentIndex(mode)
        dialog._run()
        finish(app, dialog)
        assert dialog.colorbar is not None
        assert dialog.export.isEnabled()
    t = np.arange(4000)/2000
    dialog.snapshot = dict(times=t, values=np.where(t>.2, 1-np.exp(-np.maximum(t-.2,0)/.1),0), unit="rpm")
    dialog.mode.setCurrentIndex(3)
    dialog.event.setValue(.2)
    dialog.target.setValue(1)
    dialog._run()
    finish(app, dialog)
    assert dialog.result["metrics"]["rise_s"] > 0
    dialog.close()


def test_fourier_entry_reuses_live_and_csv_sources(app, tmp_path):
    snapshot, speed = demo_snapshot()
    page = FourierAnalysisPage(lambda key: snapshot if key == "phase_ia" else speed,
                               [("Ia", "phase_ia"), ("实际转速", "speed")])
    page._open_dynamic_analysis()
    app.processEvents()
    dialog = page._dynamic_dialogs[0]
    assert "实际转速" in dialog.references
    dialog.close()
    app.processEvents()
    assert not page._dynamic_dialogs
    file = tmp_path / "wave.csv"
    file.write_text("channel,series,time_s,value,sampling_rate_hz\n"+
                    "\n".join(f"current,Ia,{i/1000},{np.sin(i*.1)},1000" for i in range(100)), encoding="utf-8")
    page.load_csv(str(file))
    page._open_dynamic_analysis()
    app.processEvents()
    assert len(page._dynamic_dialogs) == 1
    assert len(page._dynamic_dialogs[0].references) == 1
    page._dynamic_dialogs[0].close()
    app.processEvents()
    page.close()


def test_native_comparison_view_has_actual_results(app):
    from algorithm_replay import native_module
    try:
        native_module()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    page = AlgorithmValidationPage()
    page.source.setCurrentIndex(2)
    page.show()
    page._run()
    finish(app, page)
    assert page.table.item(0, 1).text() != "未启用"
    page.axis.setCurrentIndex(1)
    assert len(page.error.listDataItems()) == 2
    page.close()


def test_no_capture_still_allows_demo(app):
    dialog = DynamicAnalysisDialog("空缓冲", dict(values=[], times=[]))
    dialog.show()
    dialog._demo()
    finish(app, dialog)
    assert dialog.result["resolution"] == pytest.approx(4000/512)
    dialog.close()


def test_fourier_subtabs_can_analyze_irregular_response(app):
    t = np.r_[0., np.cumsum(np.tile([.006, .013, .011], 150))]
    snapshot = dict(times=t, values=np.where(t < .2, 0, 1-np.exp(-np.maximum(t-.2,0)/.15)), unit="rpm")
    page = FourierAnalysisPage(lambda _: snapshot, [("实际转速", "speed")])
    page.show()
    assert page.analysis_tabs.count() == 5
    page.analysis_tabs.setCurrentIndex(4)
    app.processEvents()
    panel = page._dynamic_panels[4]
    assert panel.embedded and not panel.isWindow()
    assert panel.mode.currentIndex() == 3
    panel.event.setValue(.2)
    panel.target.setValue(1)
    panel._run()
    finish(app, panel)
    assert panel.result["metrics"]["rise_s"] > 0
    page.analysis_tabs.setCurrentIndex(1)
    other = page._dynamic_panels[1]
    other._demo()
    finish(app, other)
    for i in range(other.palette.count()):
        other.palette.setCurrentIndex(i)
        assert other.colorbar is not None
    from widgets.analysis_plot_dialog import AnalysisPlotDialog
    popup = AnalysisPlotDialog([("STFT", other.map_plot)], page)
    popup.show()
    app.processEvents()
    assert len(popup.bars) == 1
    popup.accept()
    page.close()
    app.processEvents()


def test_plot_popout_and_replay_visibility(app):
    from algorithm_replay import native_module
    try:
        native_module()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    from widgets.analysis_plot_dialog import AnalysisPlotDialog
    page = AlgorithmValidationPage()
    page._demo()
    finish(app, page)
    assert len(page.current.listDataItems()) == 3
    page.trace_toggles["B"].setChecked(False)
    assert len(page.current.listDataItems()) == 2
    page.current_view.setCurrentIndex(1)
    np.testing.assert_allclose(page.current.listDataItems()[0].getData()[1], 0)
    popup = AnalysisPlotDialog([("电流", page.current), ("误差", page.error)], page)
    popup.show()
    app.processEvents()
    assert len(popup.views[0].listDataItems()) == 2
    popup.accept()
    page.close()


def test_stationary_reference_and_no_step_still_render(app):
    t = np.arange(2000)/1000
    snapshot = dict(times=t, values=np.full(len(t), 800.), unit="rpm")
    references = {"Ia": lambda: dict(snapshot, unit="A"),
                  "speed / 实际": lambda: dict(snapshot, values=t*0)}
    dialog = DynamicAnalysisDialog("speed / 实际", snapshot, references, fixed_mode=2)
    assert dialog.reference.currentText() == "speed / 实际"
    dialog._run()
    finish(app, dialog)
    assert np.isnan(dialog.result["power"]).all()
    assert len(dialog.wave_plot.listDataItems()) == 1
    assert "暂无" in dialog.status.text()
    dialog.mode.setCurrentIndex(3)
    dialog._run()
    finish(app, dialog)
    assert not dialog.result["step_valid"]
    assert len(dialog.map_plot.listDataItems()) == 1
    assert "阶跃指标不适用" in dialog.status.text()
    assert dialog.export.isEnabled()
    dialog.close()


def test_order_heatmap_keeps_stop_gap_blank(app):
    import pyqtgraph as pg
    from scipy.integrate import cumulative_trapezoid
    t = np.arange(5000)/1000
    rpm = np.where((t >= 1) & (t < 2), 1200., np.where((t >= 3) & (t < 4), -1200., 0.))
    cycles = cumulative_trapezoid(rpm/60, t, initial=0)
    snapshot = dict(times=t, values=np.cos(2*np.pi*cycles), unit="A")
    reference = dict(times=t, values=rpm, unit="rpm")
    dialog = DynamicAnalysisDialog("Ia", snapshot, {"speed / 实际": lambda: reference}, fixed_mode=2)
    dialog._run()
    finish(app, dialog)
    img = next(i for i in dialog.map_plot.items if isinstance(i, pg.ImageItem))
    assert np.isnan(img.image[:, img.image.shape[1]//2]).all()
    assert np.isfinite(img.image[:, 1]).any()
    dialog.close()


def test_category_switch_refreshes_existing_panel_and_locates_reference_step(app, tmp_path):
    file = tmp_path / "step.csv"
    t = np.arange(2000)/1000
    lines = ["channel,series,time_s,value,sampling_rate_hz,unit"]
    for channel, label, values, unit in (
            ("speed", "实际", np.where(t < .15, 0., 800*(1-np.exp(-np.maximum(t-.15,0)/.02))), "rpm"),
            ("speed", "给定", np.where(t < .15, 0., 800.), "rpm"),
            ("phase_current", "Ia", np.sin(2*np.pi*50*t), "A")):
        lines += [f"{channel},{label},{time},{value},1000,{unit}" for time, value in zip(t, values)]
    file.write_text("\n".join(lines), encoding="utf-8")
    page = FourierAnalysisPage()
    page.load_csv(str(file))
    page.analysis_tabs.setCurrentIndex(4)
    panel = page._dynamic_panels[4]
    assert panel.source_label == "speed / 实际"
    assert panel.event.value() == pytest.approx(.15)
    assert panel.target.value() == 800
    page._category_combo.setCurrentIndex(page._category_combo.findData("电流"))
    assert panel.source_label == "phase_current / Ia"
    assert panel.snapshot["unit"] == "A"
    # Explicit call also catches exceptions otherwise swallowed by Qt signals.
    page._sync_analysis_source()
    page.close()


@pytest.mark.parametrize("fs", [7.213456, 99.99999, 499.999999])
def test_cwt_frequency_bounds_survive_low_rate_and_rounding(app, fs):
    t = np.arange(1000)/fs
    snapshot = dict(times=t, values=np.sin(2*np.pi*fs*.1*t), sample_rate_hz=fs)
    dialog = DynamicAnalysisDialog("空缓冲", dict(times=[], values=[]), fixed_mode=1,
        source_provider=lambda: ("低速信号", snapshot, {}, None))
    assert 0 < dialog.fmin.value() < dialog.fmax.value() <= fs/2
    dialog._run()
    finish(app, dialog)
    assert np.isfinite(dialog.result["power"]).any()
    assert dialog.result["axis"][-1] <= dialog.metadata["fs"]/2
    dialog.fmax.setValue(dialog.fmin.value()/2)
    assert 0 < dialog.fmin.value() < dialog.fmax.value()
    dialog.close()


def test_cwt_rechecks_frequency_for_selected_interval(app):
    t = np.r_[np.arange(500)/100, 5+np.arange(500)/90]
    snapshot = dict(times=t, values=np.sin(2*np.pi*15*t), sample_rate_hz=100)
    dialog = DynamicAnalysisDialog("变采样间隔", snapshot, fixed_mode=1)
    assert dialog.fmax.value() > 45
    dialog.start.setValue(5)
    dialog._run()
    finish(app, dialog)
    assert dialog.fmax.value() <= dialog.metadata["fs"]/2
    assert np.isfinite(dialog.result["power"]).any()
    dialog.close()


def test_cwt_switch_from_slow_channel_restores_useful_band(app):
    slow_t = np.arange(120)/7.2
    source = ["转矩", dict(times=slow_t, values=np.sin(slow_t)), {}, None]
    dialog = DynamicAnalysisDialog("空缓冲", dict(times=[], values=[]), fixed_mode=1,
                                   source_provider=lambda: tuple(source))
    assert dialog.fmax.value() <= 3.6
    t = np.arange(5000)/16000
    source[:2] = ["Ia", dict(times=t, values=np.sin(2*np.pi*100*t))]
    dialog.refresh_source()
    assert dialog.fmax.value() == 500
    dialog._run()
    finish(app, dialog)
    assert np.isfinite(dialog.result["power"]).any()
    dialog.fmax.setValue(300)
    dialog.refresh_source()
    assert dialog.fmax.value() == 300
    dialog.close()
