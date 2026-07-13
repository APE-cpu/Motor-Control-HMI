import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

import pytest
from PySide6.QtWidgets import QApplication

from experiments import ExperimentSessionManager, summarize_telemetry
from widgets.historical_telemetry_dialog import HistoricalTelemetryDialog


def _app():
    return QApplication.instance() or QApplication([])


def _finished_experiment(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "records")
    session = manager.create_session("历史曲线测试")
    manager.start()
    for i in range(5):
        manager.record_telemetry({
            "monotonic_s": i * 0.1,
            "speed_actual": 1000 + i * 10,
            "speed_target": 1020,
            "current_actual": i,
            "torque_actual": i * 0.2,
            "vdc": 48 - i * 0.1,
            "temperature": 25 + i,
            "data_source": "sim",
        })
    manager.record_marker("load_applied", "负载投入", {"speed_actual": 1020})
    manager.complete()
    return manager, session


def test_历史遥测读取转换与等距降采样(tmp_path):
    manager, session = _finished_experiment(tmp_path)
    rows = manager.repository.read_telemetry(session.experiment_id)
    sampled = manager.repository.read_telemetry(session.experiment_id, max_points=3)

    assert len(rows) == 5
    assert rows[0]["speed_actual"] == 1000.0
    assert rows[-1]["temperature"] == 29.0
    assert [row["speed_actual"] for row in sampled] == [1000.0, 1020.0, 1040.0]
    with pytest.raises(ValueError, match="至少为 2"):
        manager.repository.read_telemetry(session.experiment_id, max_points=1)


def test_损坏单元格置空且不影响其它物理量(tmp_path):
    manager, session = _finished_experiment(tmp_path)
    path = manager.repository.session_dir(session.experiment_id) / "telemetry.csv"
    text = path.read_text("utf-8-sig")
    path.write_text(text.replace("1000", "bad", 1), encoding="utf-8-sig")

    rows = manager.repository.read_telemetry(session.experiment_id)
    assert rows[0]["speed_actual"] is None
    assert rows[0]["vdc"] == 48.0


def test_基础统计与转速跟踪误差():
    rows = [
        {"monotonic_s": 0, "speed_actual": 90, "speed_target": 100,
         "current_actual": 3, "torque_actual": 1, "vdc": 48, "temperature": 25},
        {"monotonic_s": 2, "speed_actual": 110, "speed_target": 100,
         "current_actual": 4, "torque_actual": 2, "vdc": 47, "temperature": 27},
    ]
    summary = summarize_telemetry(rows)

    assert summary["samples"] == 2
    assert summary["duration_s"] == 2
    assert summary["speed_mae_rpm"] == 10
    assert summary["metrics"]["speed_actual"]["mean"] == 100
    assert summary["metrics"]["current_actual"]["rms"] == pytest.approx(math.sqrt(12.5))
    assert summary["metrics"]["temperature"]["peak_to_peak"] == 2


def test_历史曲线对话框离屏构造(tmp_path):
    _app()
    manager, session = _finished_experiment(tmp_path)
    rows = manager.repository.read_telemetry(session.experiment_id)
    events = manager.repository.read_events(session.experiment_id)
    dialog = HistoricalTelemetryDialog(session.experiment_id, rows, events)

    assert dialog.summary["samples"] == 5
    assert dialog.windowTitle().endswith(session.experiment_id)
    assert len(dialog.timeline_events) == 1
    assert dialog.timeline_events[0]["message"] == "负载投入"
    assert dialog.marker_line_count == 5
    dialog.close()
