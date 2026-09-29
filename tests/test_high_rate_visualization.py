import os
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from communications.native_telemetry import NativeTelemetryProcessor
from pages.power_flow_page import PowerFlowPage
from pages.vector_page import VectorPage, _PG_OK
from config.config import f1_bus_adc


def _app():
    return QApplication.instance() or QApplication([])


@pytest.mark.skipif(not _PG_OK, reason="pyqtgraph未安装")
def test_vector_page_accepts_16khz_columns_and_limits_point_cloud_load():
    _app()
    page = VectorPage(CommManager())
    assert page._chk_enabled.isChecked() is False
    assert page._chk_persist.isChecked() is False
    page.show()
    page._chk_enabled.setChecked(True)
    page._timer.stop()
    count = 160
    page._on_high_rate_columns({
        "rate_hz": 16000,
        "angle_deg": [index * 360.0 / count for index in range(count)],
        "iq_a": [2.0] * count,
    })

    # 16 kHz输入按几何点云上限抽到1 kHz；显示以约30 Hz独立刷新。
    assert len(page._i_plot._xs) == 10
    assert len(page._psi_plot._xs) == 10
    assert page._timer.interval() == 33
    page.hide()
    page._on_high_rate_columns({
        "rate_hz": 16000,
        "angle_deg": [0.0] * count,
        "iq_a": [2.0] * count,
    })
    assert len(page._i_plot._xs) == 10
    assert not page._timer.isActive()
    page.close()
    page.deleteLater()


@pytest.mark.skipif(not _PG_OK, reason="pyqtgraph未安装")
def test_vector_page_uses_native_trail_for_cpp_f1_columns():
    _app()
    comm = CommManager()
    assert comm._native_vector_trail is not None
    comm.is_connected = lambda: True
    page = VectorPage(comm)
    page.show()
    page._chk_enabled.setChecked(True)
    page._timer.stop()
    processor = NativeTelemetryProcessor()
    processor.set_f1_rate_hz(16000)
    sample = struct.pack(
        "<IHHhhhhhhhhhHH", 42, 73, 32768, 500, 2000, -321, 1990, 25,
        1200, -600, -1000, 6000,
        f1_bus_adc(24.0), 0xF130)
    assert processor.ingest(0xF1, sample * 160)
    columns = processor.drain_f1_columns(vector_trail=comm._native_vector_trail)
    page._on_high_rate_columns(columns)
    page._refresh()

    assert page._native_stream_active
    assert len(page._i_plot._xs) == 0  # C++ 保存轨迹，未写入 Python deque
    assert len(page._i_plot._scatter.data) == 10
    assert len(page._psi_plot._scatter.data) == 10
    page.hide()
    assert processor.ingest(0xF1, sample * 16)
    processor.drain_f1_columns(vector_trail=comm._native_vector_trail)
    assert len(comm._native_vector_trail.snapshot()["current_x"]) == 10
    page.close()
    page.deleteLater()


def test_power_flow_page_estimates_real_machine_power_from_f0_and_f1():
    _app()
    comm = CommManager()
    comm.is_connected = lambda: True
    page = PowerFlowPage(comm)
    assert page._chk_enabled.isChecked() is False
    page._chk_enabled.setChecked(True)
    page._timer.stop()
    frame = TelemetryFrame()
    frame.speed_actual = 600.0
    frame.torque_actual = 0.2
    frame.current_actual = 2.0
    frame.vdc = 48.0
    frame.bus_state = "normal"
    page._on_telemetry(frame)
    page._on_high_rate_columns({
        "iq_a": [2.0] * 32,
        "vq_raw": [16384.0] * 32,
        "vbus_v": [48.0] * 32,
    })

    powers, source = page._estimate_real_powers()
    page._refresh()

    assert powers["inv"] > powers["em"] > 0.0
    assert powers["cu"] > 0.0
    assert "F1 Vq·Iq" in source
    assert "真机估算" in page._source_label.text()
    assert list(page._curve._buffers["电源输入"])[-1] > 0.0
    page.close()
    page.deleteLater()
