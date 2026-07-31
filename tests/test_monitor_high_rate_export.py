import csv
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from pages.monitor_page import MonitorPage


def _app():
    return QApplication.instance() or QApplication([])


def test_high_rate_iq_drives_current_curve_and_csv(tmp_path):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._last_telemetry_time = time.time()

    page._on_high_rate_telemetry({
        "angle_deg": 12.5, "iq_a": 0.125, "iqref_a": 0.0,
        "ia_a": 0.2, "ib_a": -0.1,
    })
    page._on_high_rate_telemetry({
        "angle_deg": 13.0, "iq_a": -0.25, "iqref_a": 0.0,
        "ia_a": -0.3, "ib_a": 0.15,
    })
    page._refresh()

    assert page._c_current._times.maxlen == 5000
    assert page._c_angle._times.maxlen == 5000
    assert list(page._c_current._buffers["实际 Iq"])[-2:] == [0.125, -0.25]
    assert list(page._c_current._buffers["给定 Iq"])[-2:] == [0.0, 0.0]
    assert list(page._c_phase_current._buffers["Ia"])[-2:] == [0.2, -0.3]
    assert list(page._c_phase_current._buffers["Ib"])[-2:] == [-0.1, 0.15]
    assert list(page._c_angle._buffers["高速电角度"])[-2:] == [12.5, 13.0]

    output = tmp_path / "waveforms.csv"
    page._write_curves_csv(str(output))
    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    iq_rows = [row for row in rows
               if row["channel"] == "iq_current" and row["series"] == "实际 Iq"]
    assert [float(row["value"]) for row in iq_rows[-2:]] == [0.125, -0.25]
    ia_rows = [row for row in rows
               if row["channel"] == "phase_current" and row["series"] == "Ia"]
    assert [float(row["value"]) for row in ia_rows[-2:]] == [0.2, -0.3]
    page.close()
