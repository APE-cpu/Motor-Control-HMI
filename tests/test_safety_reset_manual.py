import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QStackedWidget

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import CMD_START
from main_window import MainWindow
from pages.control_page import ControlPage
from pages.power_flow_page import PowerFlowPage
from pages.vector_page import VectorPage, _PG_OK


def _app():
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_电流限幅进入数字孪生与实验保护快照():
    _app()
    comm = CommManager()
    page = ControlPage(comm)
    page._current_limit.setValue(2.5)
    page._rated_temperature.setValue(65.0)
    comm.configure_motor_current_limit(2.5)

    snapshot = page.experiment_snapshot()
    assert comm.motor_sim_params().i_max == pytest.approx(2.5)
    assert snapshot["protection_params"]["max_current_a"] == pytest.approx(2.5)
    assert snapshot["device"]["extra"]["rated_operating_temperature_c"] == 65.0
    page.close()
    page.deleteLater()


def test_仿真启动保持静止且停止后完整复位():
    _app()
    comm = CommManager()
    frames = []
    comm.telemetryReceived.connect(frames.append)
    comm.start_simulation()
    assert _wait_until(lambda: len(frames) >= 1)
    assert comm._motor_sim.enabled is False
    assert frames[-1].speed_actual == 0.0

    comm.configure_motor_current_limit(1.75)
    assert comm.send_frame(encode_frame(CMD_START, b"target=3000"))
    assert _wait_until(lambda: abs(comm._motor_sim.iq_ref) > 0.1)
    assert abs(comm._motor_sim.iq_ref) <= 1.75

    comm.stop_simulation()
    reset = frames[-1]
    assert reset.speed_actual == reset.angle_actual == reset.current_actual == 0.0
    assert comm._motor_sim.enabled is False
    assert comm._motor_sim.speed_rpm == 0.0
    assert comm._motor_sim.angle_deg == 0.0
    assert comm.motor_sim_trace() == []


@pytest.mark.skipif(not _PG_OK, reason="pyqtgraph未安装")
def test_矢量页在数据源停止后清空并停止演示旋转():
    _app()
    comm = CommManager()
    page = VectorPage(comm)
    page._i_plot.append(1.0, 2.0)
    page._psi_plot.append(0.1, 0.2)
    page._spin = 2.0

    page._refresh()

    assert list(page._i_plot._xs) == []
    assert list(page._psi_plot._xs) == []
    assert page._spin == 0.0
    page.close()
    page.deleteLater()


def test_功率页显示计算式且系统导航提供说明书(tmp_path, monkeypatch):
    _app()
    power_page = PowerFlowPage(CommManager())
    texts = "\n".join(label.text() for label in power_page.findChildren(QLabel))
    assert "P_inv = 3/2" in texts
    assert "P_brake = V_dc²" in texts
    frame = TelemetryFrame()
    frame.powers = {
        "supply": 100.0, "loss_src": 5.0, "inv": 90.0,
        "brake": 0.0, "cu": 10.0, "em": 78.0,
        "fric": 30.0, "kinetic": 48.0,
    }
    power_page._on_telemetry(frame)
    power_page._refresh()
    assert "驱动" in power_page._calculation._direction.text()
    assert "+5.00 W" in power_page._calculation._bus_balance.text()
    assert "+0.000 W" in power_page._calculation._mech_balance.text()
    power_page.close()
    power_page.deleteLater()

    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts))
    window = MainWindow(enable_training=False)
    assert window.stack.indexOf(window.manual_page) == 11
    assert "数字孪生实验标准流程" in window.manual_page._browser.toPlainText()
    window.close()
    window.deleteLater()


def test_快速仿真一键进入运行且主页面小屏可滚动(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts))
    window = MainWindow(enable_training=False)
    monitor = window.monitor_page

    monitor._on_quick_sim()
    assert window.runtime_state.state.value == "running"
    assert window.comm_manager.is_sim_running() is True
    assert window.comm_manager._motor_sim.enabled is True

    wrapper = QStackedWidget.widget(window.stack, 0)
    assert isinstance(wrapper, QScrollArea)
    assert wrapper.widget() is monitor
    assert monitor.minimumWidth() == 980

    monitor._on_stop()
    assert window.runtime_state.state.value == "ready"
    monitor._on_toggle_sim()
    assert window.runtime_state.state.value == "disconnected"
    window.close()
    window.deleteLater()


def test_监控页温度补充额定偏差且角度区显示不混叠的电频率(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts))
    window = MainWindow(enable_training=False)
    window.control_page._pole_pairs.setValue(4)
    window.control_page._rated_temperature.setValue(60.0)
    frame = TelemetryFrame()
    frame.angle_actual = 30.0
    frame.speed_actual = 1500.0
    frame.temperature = 70.0
    window.monitor_page._on_telemetry(frame)
    window.monitor_page._refresh()

    assert "100.00" in window.monitor_page._electrical_frequency._value.text()
    assert "60.0" in window.monitor_page._temperature.rated.text()
    assert "+10.0" in window.monitor_page._temperature.delta.text()
    assert "偏高" in window.monitor_page._temperature.status.text()
    assert not hasattr(window.monitor_page, "_angle_raw")
    assert not hasattr(window.monitor_page, "_angle_electrical")
    window.close()
    window.deleteLater()
