import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QGroupBox

from communications.comm_manager import CommManager
from main_window import MainWindow
from pages.monitor_page import MonitorPage, _EnergyOrb


def _app():
    return QApplication.instance() or QApplication([])


def test_monitor_uses_remote_three_state_motor_background():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._orb._timer.stop()

    titles = [box.title() for box in page.findChildren(QGroupBox)]
    assert any(title.startswith("传感器状态") for title in titles)
    assert "统计（最大/最小）" in titles
    assert isinstance(page._orb, _EnergyOrb)
    assert set(page._orb._pixmaps) == {"stopped", "running", "fault"}
    page.close()


def test_motor_background_accepts_stopped_running_and_fault_states():
    _app()
    orb = _EnergyOrb()
    orb._timer.stop()
    for state, rpm in (("stopped", 0.0), ("running", 1200.0),
                       ("fault", 0.0)):
        orb.set_state(state, rpm)
        assert orb._state == state
        assert orb._rpm == rpm
    orb.close()


def test_hidden_main_window_switches_without_native_transition_objects():
    _app()
    window = MainWindow(enable_training=False)
    window.nav.select_page(1)

    assert window.stack.currentIndex() == 1
    assert getattr(window, "_page_overlay", None) is None
    assert getattr(window, "_page_transition_group", None) is None
    window.close()


def test_visible_main_window_can_close_during_page_transition():
    app = _app()
    window = MainWindow(enable_training=False)
    window.show()
    app.processEvents()
    window.nav.select_page(1)
    assert window.stack.currentIndex() == 1

    window.close()
    window.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
