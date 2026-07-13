"""把运行时 Qt 信号桥接到实验会话。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from logs.operation_logger import OperationLogger

from .models import SessionStatus
from .session_manager import ExperimentSessionManager

if TYPE_CHECKING:
    from communications.comm_manager import CommManager


class ExperimentRecorder(QObject):
    """自动归档通信遥测和结构化操作日志。

    记录失败不会打断通信/UI 信号链，而是通过 recordingError 上报。调用 close()
    后解除所有连接，避免下一次实验误收上一会话的数据。
    """

    recordingError = Signal(str)
    countersChanged = Signal()

    def __init__(self, manager: ExperimentSessionManager, comm: "CommManager",
                 operation_logger: OperationLogger, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._comm = comm
        self._logger = operation_logger
        self._closed = False
        comm.telemetryReceived.connect(self._on_telemetry)
        operation_logger.newRecord.connect(self._on_operation)

    @property
    def is_attached(self) -> bool:
        return not self._closed

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._comm.telemetryReceived.disconnect(self._on_telemetry)
        except (RuntimeError, TypeError):
            pass
        try:
            self._logger.newRecord.disconnect(self._on_operation)
        except (RuntimeError, TypeError):
            pass
        self._closed = True

    @Slot(object)
    def _on_telemetry(self, frame: object) -> None:
        if not self._is_recording():
            return
        try:
            self._manager.record_telemetry(frame)
            self.countersChanged.emit()
        except Exception as exc:  # 信号消费者不能影响通信线程
            self.recordingError.emit(f"遥测写入失败：{exc}")

    @Slot(str, str, str)
    def _on_operation(self, timestamp: str, action: str, detail: str) -> None:
        if not self._is_recording():
            return
        try:
            self._manager.record_event(
                "operation",
                action,
                {"detail": detail, "source_timestamp": timestamp},
            )
            self.countersChanged.emit()
        except Exception as exc:
            self.recordingError.emit(f"操作日志写入失败：{exc}")

    def _is_recording(self) -> bool:
        session = self._manager.active_session
        return (not self._closed and session is not None and
                session.status is SessionStatus.RUNNING)
