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
