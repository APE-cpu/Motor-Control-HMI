import os
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from analysis_dynamic import demo_snapshot
from pages.dynamic_analysis_dialog import DynamicAnalysisDialog
from pages.algorithm_validation_page import AlgorithmValidationPage
from pages.fourier_page import FourierAnalysisPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


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
