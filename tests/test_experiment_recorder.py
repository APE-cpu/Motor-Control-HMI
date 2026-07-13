import csv
import json

from communications.comm_manager import CommManager, TelemetryFrame
from experiments import (
    ExperimentRecorder,
    ExperimentSessionManager,
    SessionStatus,
)
from logs.operation_logger import OperationLogger


def _running_manager(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "experiments")
    session = manager.create_session("自动记录测试")
    manager.start()
    return manager, session


def test_通信遥测和操作日志自动进入实验档案(tmp_path, monkeypatch):
    manager, session = _running_manager(tmp_path)
    comm = CommManager()
    operation_logger = OperationLogger()
    monkeypatch.setattr("logs.operation_logger._LOG_FILE", tmp_path / "operation.log")
    recorder = ExperimentRecorder(manager, comm, operation_logger)

    frame = TelemetryFrame()
    frame.speed_actual = 1200.0
    frame.current_actual = 1.2
    frame.data_source = "sim"
    comm.telemetryReceived.emit(frame)
    operation_logger.log("启动电机", "目标转速=1200rpm")
    manager.complete()
    recorder.close()

    loaded = manager.load(session.experiment_id)
    # session_started + operation + session_completed
    assert loaded.event_count == 3
    assert loaded.telemetry_count == 1

    directory = manager.repository.session_dir(session.experiment_id)
    with open(directory / "telemetry.csv", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["speed_actual"] == "1200.0"

    events = [json.loads(line) for line in
              (directory / "events.jsonl").read_text("utf-8").splitlines()]
    operation = events[1]
    assert operation["type"] == "operation"
    assert operation["message"] == "启动电机"
    assert operation["details"]["detail"] == "目标转速=1200rpm"
    assert operation["details"]["source_timestamp"]


def test_实验开始前和结束后不会记录信号(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "experiments")
    session = manager.create_session("边界测试")
    comm = CommManager()
    operation_logger = OperationLogger()
    recorder = ExperimentRecorder(manager, comm, operation_logger)

    comm.telemetryReceived.emit(TelemetryFrame())
    operation_logger.newRecord.emit("2026-07-12T12:00:00+08:00", "开始前", "")
    assert session.telemetry_count == 0
    assert session.event_count == 0

    manager.start()
    manager.complete()
    comm.telemetryReceived.emit(TelemetryFrame())
    operation_logger.newRecord.emit("2026-07-12T12:01:00+08:00", "结束后", "")
    loaded = manager.load(session.experiment_id)
    assert loaded.telemetry_count == 0
    assert loaded.event_count == 2
    recorder.close()


def test_close解除连接防止后续串写(tmp_path):
    manager, session = _running_manager(tmp_path)
    comm = CommManager()
    operation_logger = OperationLogger()
    recorder = ExperimentRecorder(manager, comm, operation_logger)
    recorder.close()
    recorder.close()  # 幂等

    comm.telemetryReceived.emit(TelemetryFrame())
    operation_logger.newRecord.emit("2026-07-12T12:00:00+08:00", "不应记录", "")
    manager.complete()

    loaded = manager.load(session.experiment_id)
    assert recorder.is_attached is False
    assert loaded.telemetry_count == 0
    assert loaded.event_count == 2


def test_记录失败通过信号上报且不影响发送者(tmp_path, monkeypatch):
    manager, _ = _running_manager(tmp_path)
    comm = CommManager()
    operation_logger = OperationLogger()
    recorder = ExperimentRecorder(manager, comm, operation_logger)
    errors = []
    recorder.recordingError.connect(errors.append)

    def fail(_frame, timestamp=None):
        raise OSError("磁盘已满")

    monkeypatch.setattr(manager, "record_telemetry", fail)
    comm.telemetryReceived.emit(TelemetryFrame())

    assert errors == ["遥测写入失败：磁盘已满"]
    assert manager.active_session.status is SessionStatus.RUNNING
    recorder.close()
