"""边缘AI页面：本地 ONNX 推理 + 规则检测，实时显示异常分数。"""
import os

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QVBoxLayout, QWidget, QPlainTextEdit,
)

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_REFRESH_MS
from edge_ai.engine import EdgeAIEngine
from logs.operation_logger import logger


class EdgeAIPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._latest = TelemetryFrame()
        self._engine = EdgeAIEngine()   # 默认规则模式

        root = QVBoxLayout(self)
        title = QLabel("边缘AI 异常检测")
        title.setObjectName("TitleLabel")
        root.addWidget(title)
        root.addWidget(self._build_model_box())
        root.addWidget(self._build_result_box())
        root.addWidget(self._build_history_box(), 1)

        comm.telemetryReceived.connect(self._on_telemetry)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._run_inference)
        self._timer.start(MONITOR_REFRESH_MS * 5)   # 每 500ms 推理一次

    def _build_model_box(self) -> QGroupBox:
        box = QGroupBox("模型配置")
        h = QHBoxLayout(box)
        self._model_label = QLabel("当前：规则/统计检测（无 ONNX 模型）")
        btn_load = QPushButton("加载 ONNX 模型")
        btn_load.clicked.connect(self._on_load_model)
        h.addWidget(self._model_label, 1)
        h.addWidget(btn_load)
        return box

    def _build_result_box(self) -> QGroupBox:
        box = QGroupBox("实时推理结果")
        f = QFormLayout(box)
        self._score_bar = QProgressBar()
        self._score_bar.setRange(0, 100)
        self._score_bar.setTextVisible(True)
        self._label_val = QLabel("--")
        self._label_val.setObjectName("BigValue")
        self._detail_val = QLabel("--")
        f.addRow("异常分数", self._score_bar)
        f.addRow("状态", self._label_val)
        f.addRow("说明", self._detail_val)
        return box

    def _build_history_box(self) -> QGroupBox:
        box = QGroupBox("检测历史")
        v = QVBoxLayout(box)
        self._history = QPlainTextEdit()
        self._history.setReadOnly(True)
        v.addWidget(self._history)
        return box

    def _on_load_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ONNX 模型", "", "ONNX 模型 (*.onnx)"
        )
        if not path:
            return
        self._engine = EdgeAIEngine(path)
        if self._engine.using_onnx:
            mode = "ONNX 模型"
        else:
            err = self._engine._load_error or "未知原因"
            mode = f"规则检测（加载失败：{err}）"
        self._model_label.setText(f"当前：{mode}  |  {os.path.basename(path)}")
        logger.log("加载边缘AI模型", os.path.basename(path))

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame

    def _run_inference(self) -> None:
        f = self._latest
        features = [
            f.speed_actual, f.speed_target,
            f.current_actual, f.current_target,
            f.torque_actual, f.torque_target,
            f.angle_actual, f.temperature,
        ]
        result = self._engine.infer(features)
        pct = int(result.score * 100)
        self._score_bar.setValue(pct)
        self._score_bar.setFormat(f"{pct}%")
        self._label_val.setText(result.label)
        self._detail_val.setText(result.detail)

        # 进度条颜色
        if result.score < 0.3:
            color = "#66bb6a"
        elif result.score < 0.6:
            color = "#ffa726"
        else:
            color = "#ef5350"
        self._score_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
        )

        if result.score >= 0.6:
            self._history.appendPlainText(
                f"[异常] 分数={pct}%  {result.detail}  "
                f"转速={f.speed_actual:.0f}rpm  温度={f.temperature:.1f}°C"
            )
