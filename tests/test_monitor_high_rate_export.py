import csv
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_PLOT_REFRESH_MS
from pages.monitor_page import MonitorPage, _WaveformSaveWorker
from rls_offline import RlsCaptureBuffer, load_rls_capture_csv


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
    voltage_scale = 48.0 / (3.0 ** 0.5 * 32768.0)
    assert list(page._c_voltage._buffers["Vq"])[-3:] == pytest.approx(
        [7.0 * voltage_scale, 8.0 * voltage_scale,
         9.0 * voltage_scale])
    assert list(page._c_angle._buffers["高速电角度"])[-3:] == [
        10.0, 11.0, 12.0]
    assert page._latest_vbus_v == 48.0
    assert not any(page._high_rate_columns.values())
    page.close()


def test_F1_40监控曲线优先显示PWM重构电压():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._last_telemetry_time = time.time()
    page._on_high_rate_telemetry_columns({
        "count": 2, "rate_hz": 16000,
        "angle_deg": [0.0, 1.0], "speed_rpm": [100.0, 100.0],
        "iq_a": [0.1, 0.1], "iqref_a": [0.1, 0.1],
        "ia_a": [0.1, 0.1], "ib_a": [-0.05, -0.05],
        "vd_raw": [30000.0, 30000.0], "vq_raw": [30000.0, 30000.0],
        "vbus_v": [24.0, 24.0],
        "vd_applied_v": [1.25, 1.5], "vq_applied_v": [2.5, 2.75],
        "applied_voltage_source_direct": True,
    })

    page._refresh()

    assert list(page._c_voltage._buffers["Vd"])[-2:] == [1.25, 1.5]
    assert list(page._c_voltage._buffers["Vq"])[-2:] == [2.5, 2.75]
    assert page.fourier_snapshot("vq")["unit"] == "V"
    page.close()


def test_RLS原始同步帧可导出并重新读取(tmp_path):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._on_high_rate_telemetry_columns({
        "count": 3,
        "rate_hz": 16000,
        "tick_ms": [100, 100, 100],
        "angle_deg": [10.0, 11.0, 12.0],
        "speed_rpm": [500.0, 501.0, 502.0],
        "iq_a": [0.1, 0.2, 0.3],
        "iqref_a": [0.4, 0.5, 0.6],
        "ia_a": [1.0, 2.0, 3.0],
        "ib_a": [-1.0, -2.0, -3.0],
        "vd_raw": [4.0, 5.0, 6.0],
        "vq_raw": [7.0, 8.0, 9.0],
        "vbus_v": [24.0, 24.1, 24.2],
    })

    output = tmp_path / "RLS辨识数据.csv"
    page._write_rls_capture_csv(str(output))
    loaded = load_rls_capture_csv(str(output))

    assert loaded["count"] == 3
    assert loaded["rate_hz"] == 16000
    assert list(loaded["sample_index"]) == [0, 1, 2]
    assert list(loaded["tick_ms"]) == [100, 100, 100]
    assert list(loaded["iq_a"]) == pytest.approx([0.1, 0.2, 0.3])
    assert list(loaded["vq_raw"]) == pytest.approx([7.0, 8.0, 9.0])
    assert list(loaded["vbus_v"]) == pytest.approx([24.0, 24.1, 24.2])
    page.close()


def test_RLS新版捕获保留固件Park直接Id(tmp_path):
    buffer = RlsCaptureBuffer(max_seconds=1.0)
    columns = {
        "count": 2, "rate_hz": 16000, "tick_ms": [1, 1],
        "angle_deg": [10.0, 11.0], "speed_rpm": [500.0, 501.0],
        "iq_a": [1.0, 1.1], "id_a": [-0.02, -0.03],
        "id_source_direct": True, "iqref_a": [1.0, 1.1],
        "sample_seq": [65535, 0], "sequence_source_direct": True,
        "vdda_v": [3.15, 3.15], "vdda_source_direct": True,
        "actuation_angle_deg": [10.5, 11.5],
        "duty_a": [2600, 2601], "duty_b": [2500, 2501],
        "duty_c": [2400, 2401],
        "vd_applied_v": [0.12, 0.13],
        "vq_applied_v": [1.20, 1.30],
        "applied_voltage_source_direct": True,
        "idref_a": [0.01, -0.01],
        "ia_a": [0.2, 0.3], "ib_a": [-0.4, -0.5],
        "vd_raw": [100.0, 101.0], "vq_raw": [200.0, 201.0],
        "vbus_v": [24.0, 24.0],
    }
    assert buffer.append_columns(columns)
    output = tmp_path / "direct_id.csv"
    buffer.write_csv(output)
    loaded = load_rls_capture_csv(output)

    assert loaded["id_source_direct"] is True
    assert loaded["sequence_source_direct"] is True
    assert list(loaded["sample_seq"]) == [65535, 0]
    assert loaded["vdda_source_direct"] is True
    assert list(loaded["vdda_v"]) == pytest.approx([3.15, 3.15])
    assert list(loaded["id_a"]) == pytest.approx([-0.02, -0.03])
    assert list(loaded["idref_a"]) == pytest.approx([0.01, -0.01])
    assert loaded["applied_voltage_source_direct"] is True
    assert list(loaded["duty_a"]) == [2600, 2601]
    assert list(loaded["vd_applied_v"]) == pytest.approx([0.12, 0.13])


def test_RLS冻结快照可直接展开分析而无需CSV往返():
    buffer = RlsCaptureBuffer(max_seconds=1.0)
    for sequence in ((10, 11), (12, 13)):
        count = len(sequence)
        assert buffer.append_columns({
            "count": count, "rate_hz": 16000,
            "tick_ms": [1] * count,
            "angle_deg": [10.0] * count,
            "speed_rpm": [500.0] * count,
            "iq_a": [1.0] * count, "id_a": [0.0] * count,
            "id_source_direct": True,
            "iqref_a": [1.08] * count, "idref_a": [0.08] * count,
            "ia_a": [1.0] * count, "ib_a": [-0.5] * count,
            "vd_raw": [100.0] * count, "vq_raw": [200.0] * count,
            "vbus_v": [24.0] * count,
            "sample_seq": list(sequence), "sequence_source_direct": True,
            "vdda_v": [3.15] * count, "vdda_source_direct": True,
        })

    columns = buffer.snapshot().to_columns()

    assert columns["count"] == 4
    assert columns["rate_hz"] == 16000
    assert columns["id_source_direct"] is True
    assert columns["sequence_source_direct"] is True
    assert columns["vdda_source_direct"] is True
    assert list(columns["sample_seq"]) == [10, 11, 12, 13]
    assert list(columns["idref_a"]) == pytest.approx([0.08] * 4)


def test_RLS捕获数据在清空波形时一并清空():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._on_high_rate_telemetry_columns({
        "count": 1,
        "rate_hz": 1000,
        "tick_ms": [1],
        "angle_deg": [1.0], "speed_rpm": [2.0],
        "iq_a": [0.1], "iqref_a": [0.2],
        "ia_a": [0.3], "ib_a": [-0.3],
        "vd_raw": [4.0], "vq_raw": [5.0], "vbus_v": [24.0],
    })
    assert page._rls_capture.sample_count == 1

    page._clear_all_curves()

    assert page._rls_capture.sample_count == 0
    page.close()


def test_RLS激励关闭后冻结捕获区避免无激励尾段污染():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    columns = {
        "count": 1, "rate_hz": 16000, "tick_ms": [1],
        "angle_deg": [1.0], "speed_rpm": [500.0],
        "iq_a": [1.0], "iqref_a": [1.08],
        "ia_a": [0.2], "ib_a": [-0.4],
        "vd_raw": [100.0], "vq_raw": [200.0], "vbus_v": [24.0],
    }
    page._on_high_rate_telemetry_columns(columns)
    assert page._rls_capture.sample_count == 1

    page._rls_capture_active = False
    page._on_high_rate_telemetry_columns(columns)

    assert page._rls_capture.sample_count == 1
    page.close()


def test_RLS物理采集只在固件遥测确认激励后开始():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._rls_capture.clear()
    page._rls_capture_active = False
    page._rls_capture_had_probe = False
    columns = {
        "count": 1, "rate_hz": 16000, "tick_ms": [1],
        "angle_deg": [1.0], "speed_rpm": [500.0],
        "iq_a": [1.0], "iqref_a": [1.12],
        "ia_a": [0.2], "ib_a": [-0.4],
        "vd_raw": [100.0], "vq_raw": [200.0], "vbus_v": [24.0],
    }

    page._on_high_rate_telemetry_columns(columns)
    assert page._rls_capture.sample_count == 0

    frame = TelemetryFrame()
    frame.mc_state = 6
    frame.rls_probe_enabled = True
    page._on_telemetry(frame)
    page._on_high_rate_telemetry_columns(columns)

    assert page._rls_capture_active is True
    assert page._rls_capture_had_probe is True
    assert page._rls_capture.sample_count == 1

    frame.rls_probe_enabled = False
    page._on_telemetry(frame)
    page._on_high_rate_telemetry_columns(columns)
    assert page._rls_capture_active is False
    assert page._rls_capture.sample_count == 1
    page.close()


def test_分析当前采集在短数据时前置拒绝而不启动后台线程(monkeypatch):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._rls_capture_active = False
    page._rls_capture_had_probe = True
    messages = []
    monkeypatch.setattr(
        "pages.monitor_page.QMessageBox.warning",
        lambda _parent, title, message: messages.append((title, message)))

    page._analyze_current_rls_capture()

    assert messages
    assert "至少需要3 s" in messages[0][1]
    assert page._offline_rls_dialog._worker is None
    page.close()


def test_辨识期间断链会冻结采集且重连数据不会污染():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._rls_capture_had_probe = True
    page._rls_capture_active = True
    page._btn_rls_probe.setChecked(True)
    columns = {
        "count": 1, "rate_hz": 16000, "tick_ms": [1],
        "angle_deg": [1.0], "speed_rpm": [500.0],
        "iq_a": [1.0], "iqref_a": [1.08],
        "ia_a": [0.2], "ib_a": [-0.4],
        "vd_raw": [100.0], "vq_raw": [200.0], "vbus_v": [24.0],
    }
    page._on_high_rate_telemetry_columns(columns)

    page._on_connection_status_changed(False, "断开")
    page._on_high_rate_telemetry_columns(columns)

    assert page._rls_capture_active is False
    assert page._btn_rls_probe.isChecked() is False
    assert page._rls_capture.sample_count == 1
    assert "已冻结" in page._rls_status.text()
    page.close()


def test_保存工作线程使用不受后续采样影响的快照(tmp_path):
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._c_current.append({"实际 Iq": 0.25, "给定 Iq": 0.5})
    first = {
        "count": 1, "rate_hz": 16000, "tick_ms": [10],
        "angle_deg": [1.0], "speed_rpm": [100.0],
        "iq_a": [0.25], "iqref_a": [0.5],
        "ia_a": [0.1], "ib_a": [-0.1],
        "vd_raw": [10.0], "vq_raw": [20.0], "vbus_v": [24.0],
    }
    page._rls_capture.append_columns(first)
    curve_snapshot = page._curve_csv_snapshot()
    rls_snapshot = page._rls_capture.snapshot()

    second = dict(first)
    second["tick_ms"] = [11]
    second["iq_a"] = [9.0]
    page._rls_capture.append_columns(second)

    png_path = tmp_path / "waveforms.png"
    csv_path = tmp_path / "raw.csv"
    rls_path = tmp_path / "rls.csv"
    worker = _WaveformSaveWorker(
        str(png_path), b"PNG", str(csv_path), curve_snapshot,
        str(rls_path), rls_snapshot)
    worker.run()

    assert png_path.read_bytes() == b"PNG"
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert any(row["channel"] == "iq_current" and
               row["series"] == "实际 Iq" and
               float(row["value"]) == 0.25 for row in rows)
    loaded = load_rls_capture_csv(rls_path)
    assert loaded["count"] == 1
    assert list(loaded["iq_a"]) == pytest.approx([0.25])
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
    page._c_rls_R.append({"b1_d": 0.5, "b1_q": 0.6})
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

    voltage_scale = 48.0 / (3.0 ** 0.5 * 32768.0)
    assert list(page._c_voltage._buffers["Vq"])[-2:] == pytest.approx(
        [5.0 * voltage_scale, 6.0 * voltage_scale])
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


def _f1_speed_columns(values, rate_hz=16000):
    count = len(values)
    return {
        "count": count, "rate_hz": rate_hz,
        "angle_deg": [0.0] * count,
        "speed_rpm": list(values),
        "iq_a": [0.0] * count, "iqref_a": [0.0] * count,
        "ia_a": [0.0] * count, "ib_a": [0.0] * count,
        "vd_raw": [0.0] * count, "vq_raw": [0.0] * count,
        "vbus_v": [24.0] * count,
    }


def test_speed_curve_uses_f1_at_encoder_500hz_instead_of_16khz_duplicates():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._latest.speed_target = 500.0
    page._last_telemetry_time = time.time()
    page._on_high_rate_telemetry_columns(_f1_speed_columns(
        [100.0] * 32 + [101.0] * 32))

    page._refresh()

    assert list(page._c_speed._buffers["实际"]) == [100.0, 101.0]
    assert list(page._c_speed._buffers["给定"]) == [500.0, 500.0]
    assert page._c_speed._sample_rate_hz == pytest.approx(500.0)
    assert "F1连续流" in page._c_speed._source_processing
    assert len(page._c_current._buffers["实际 Iq"]) == 64
    page.close()


def test_f1_speed_decimation_phase_remains_continuous_across_ui_batches():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    page._latest = TelemetryFrame()
    page._last_telemetry_time = time.time()

    page._on_high_rate_telemetry_columns(_f1_speed_columns(range(20)))
    page._refresh()
    page._on_high_rate_telemetry_columns(_f1_speed_columns(range(20, 64)))
    page._refresh()

    assert list(page._c_speed._buffers["实际"]) == [0.0, 32.0]
    assert page._c_speed._sample_rate_hz == pytest.approx(500.0)
    page.close()


def test_f0_speed_fallback_records_each_telemetry_frame_only_once():
    _app()
    page = MonitorPage(CommManager())
    page._timer.stop()
    frame = TelemetryFrame()
    frame.speed_actual = 100.0
    frame.speed_target = 500.0

    page._on_telemetry(frame)
    page._refresh()
    page._refresh()
    frame.speed_actual = 110.0
    page._on_telemetry(frame)
    page._refresh()

    assert list(page._c_speed._buffers["实际"]) == [100.0, 110.0]
    assert page._speed_curve_source == "f0"
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
