import csv
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_PLOT_REFRESH_MS
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


def test_columnar_high_rate_batch_drives_curves_without_sample_dicts():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._last_telemetry_time = time.time()
    page._on_high_rate_telemetry_columns({
        "count": 3,
        "rate_hz": 1000,
        "angle_deg": [10.0, 11.0, 12.0],
        "speed_rpm": [100.0, 101.0, 102.0],
        "iq_a": [0.1, 0.2, 0.3],
        "iqref_a": [0.4, 0.5, 0.6],
        "ia_a": [1.0, 2.0, 3.0],
        "ib_a": [-1.0, -2.0, -3.0],
        "vd_raw": [4.0, 5.0, 6.0],
        "vq_raw": [7.0, 8.0, 9.0],
        "vbus_v": [48.0, 48.0, 48.0],
    })

    page._refresh()

    assert list(page._c_current._buffers["实际 Iq"])[-3:] == [0.1, 0.2, 0.3]
    assert list(page._c_phase_current._buffers["Ia"])[-3:] == [1.0, 2.0, 3.0]
    assert list(page._c_voltage._buffers["Vq"])[-3:] == [7.0, 8.0, 9.0]
    assert list(page._c_angle._buffers["高速电角度"])[-3:] == [
        10.0, 11.0, 12.0]
    assert page._latest_vbus_v == 48.0
    assert not any(page._high_rate_columns.values())
    page.close()


def test_position_loop_data_is_buffered_and_exported(tmp_path):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    frame = TelemetryFrame()
    frame.mc_state = 6
    frame.position_actual_deg = 3.25
    frame.position_target_deg = 5.0
    frame.position_error_deg = 1.75
    frame.position_speed_target_rpm = 14.0
    frame.position_speed_ff_rpm = 0.5
    frame.position_saturated = True
    page._latest = frame
    page._last_telemetry_time = time.time()

    page._refresh()

    assert page._angle_dial._valid is True
    assert page._angle_dial._position_deg == 3.25
    assert page._angle_dial._revs == 3.25 / 360.0
    assert list(page._c_position._buffers["实际位置"])[-1] == 3.25
    assert list(page._c_position._buffers["目标位置"])[-1] == 5.0
    assert list(page._c_position._buffers["位置误差"])[-1] == 1.75
    assert list(page._c_position_speed._buffers["速度给定"])[-1] == 14.0
    assert list(page._c_position_speed._buffers["速度前馈"])[-1] == 0.5
    assert list(page._c_position_state._buffers["速度限幅饱和"])[-1] == 1.0

    output = tmp_path / "position_waveforms.csv"
    page._write_curves_csv(str(output))
    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    exported = {
        (row["channel"], row["series"]): float(row["value"])
        for row in rows
        if row["channel"].startswith("position_")
    }
    assert exported[("position_angle_deg", "实际位置")] == 3.25
    assert exported[("position_angle_deg", "目标位置")] == 5.0
    assert exported[("position_angle_deg", "位置误差")] == 1.75
    assert exported[("position_speed_rpm", "速度给定")] == 14.0
    assert exported[("position_speed_rpm", "速度前馈")] == 0.5
    assert exported[("position_state", "速度限幅饱和")] == 1.0
    page.close()


def test_clear_existing_waveforms_resets_all_curves_and_statistics():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._c_speed.append({"实际": 123.0, "给定": 100.0})
    page._c_position.append({
        "实际位置": 12.0, "目标位置": 10.0, "位置误差": -2.0,
    })
    page._c_rls_R.append({"Rd": 0.5, "Rq": 0.6})
    page._high_rate_samples.append({"angle_deg": 1.0})
    page._latest_rls = {"updates": 10}
    page._stat_speed.feed(123.0)
    page._angle_dial.feed(450.0)

    page._clear_all_curves()

    curves = (
        page._c_speed, page._c_current, page._c_phase_current,
        page._c_torque, page._c_angle, page._c_sensor_q,
        page._c_position, page._c_position_speed, page._c_position_state,
        page._c_voltage, page._c_rls_a1, page._c_rls_L, page._c_rls_R,
    )
    assert all(not curve._times for curve in curves)
    assert not page._high_rate_samples
    assert not any(page._high_rate_columns.values())
    assert page._latest_rls == {}
    assert page._stat_speed._mn == float("inf")
    assert page._stat_speed._mx == float("-inf")
    assert page._stat_speed._max.text() == "最大：--"
    assert page._stat_speed._min.text() == "最小：--"
    assert page._angle_dial._valid is False
    assert page._angle_dial._position_deg == 0.0
    assert page._angle_dial._revs == 0.0
    page.close()


def test_rls_curves_use_the_same_1000_point_spec_as_standard_trends():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()

    assert page._c_rls_a1._times.maxlen == 1000
    assert page._c_rls_L._times.maxlen == 1000
    assert page._c_rls_R._times.maxlen == 1000
    page.close()


def test_hidden_tab_buffers_without_drawing_then_redraws_on_switch(monkeypatch):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._last_telemetry_time = time.time()
    draw_calls = []
    monkeypatch.setattr(page._c_voltage, "_draw",
                        lambda: draw_calls.append(True))
    page._on_high_rate_telemetry_columns({
        "count": 2,
        "rate_hz": 1000,
        "angle_deg": [1.0, 2.0],
        "speed_rpm": [10.0, 11.0],
        "iq_a": [0.1, 0.2],
        "iqref_a": [0.0, 0.0],
        "ia_a": [1.0, 2.0],
        "ib_a": [-1.0, -2.0],
        "vd_raw": [3.0, 4.0],
        "vq_raw": [5.0, 6.0],
        "vbus_v": [48.0, 48.0],
    })

    page._refresh()

    assert list(page._c_voltage._buffers["Vq"])[-2:] == [5.0, 6.0]
    assert draw_calls == []
    page._curve_tabs.setCurrentIndex(2)
    assert draw_calls == [True]
    page.close()


def test_monitor_plot_timer_runs_at_about_30_hz():
    _app()
    page = MonitorPage(CommManager())
    assert page._timer.interval() == MONITOR_PLOT_REFRESH_MS == 33
    page._timer.stop()
    page.close()


def test_filter_panel_controls_each_display_curve_without_touching_raw_data():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._c_phase_current.append_columns({"Ia": [0.0, 1.0, 0.0]}, 0.001)
    raw_before = page._c_phase_current.raw_snapshot("Ia")["values"]

    page._filter_dialog._current_preset()

    assert page._c_current._smooth_n == 16
    assert page._c_phase_current._smooth_n == 16
    assert page._c_voltage._smooth_n == 16
    assert page._c_speed._smooth_n == 1
    assert "3路开启" in page._btn_filter_panel.text()
    assert page._c_phase_current.raw_snapshot("Ia")["values"] == raw_before

    page._filter_dialog._disable_all()
    assert page._c_current._smooth_n == 1
    assert page._c_phase_current._smooth_n == 1
    assert page._c_voltage._smooth_n == 1
    assert "全关" in page._btn_filter_panel.text()
    page.close()
