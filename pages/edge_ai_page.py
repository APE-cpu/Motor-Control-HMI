"""边缘AI页面：本地 ONNX 推理 + 规则检测，实时显示异常分数。"""
import os

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QVBoxLayout, QWidget, QPlainTextEdit,
)

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_REFRESH_MS
from edge_ai.engine import EdgeAIEngine
from logs.operation_logger import logger
from runtime_paths import resource_path

# 内置模型：数字孪生训练的故障检测器（随包 motor_anomaly.onnx）
_BUILTIN_MODEL = str(resource_path("motor_anomaly.onnx"))


class EdgeAIPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._latest = TelemetryFrame()
        self._custom_path = ""     # 用户加载的自定义模型路径
        self._engine = EdgeAIEngine()   # 占位，_on_model_choice 里正式初始化

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

        # 默认选内置模型（存在则用，否则回退规则）
        self._model_combo.setCurrentIndex(0 if os.path.isfile(_BUILTIN_MODEL) else 1)
        self._on_model_choice(self._model_combo.currentIndex())

    def _build_model_box(self) -> QGroupBox:
        box = QGroupBox("模型配置")
        v = QVBoxLayout(box)
        h = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.addItems([
            "内置模型（数字孪生训练的故障检测器）",
            "规则/统计检测（无需模型）",
            "自定义 ONNX 模型…",
        ])
        self._model_combo.currentIndexChanged.connect(self._on_model_choice)
        btn_reload = QPushButton("重新选择文件")
        btn_reload.setToolTip("重新选择自定义 ONNX 文件（需先在下拉选“自定义”）")
        btn_reload.clicked.connect(self._on_pick_custom)
        h.addWidget(self._model_combo, 1)
        h.addWidget(btn_reload)
        v.addLayout(h)
        self._model_label = QLabel("")
        self._model_label.setStyleSheet("color: #8fa3b8;")
        v.addWidget(self._model_label)
        return box

    def _on_model_choice(self, idx: int) -> None:
        if idx == 0:                          # 内置模型
            if not os.path.isfile(_BUILTIN_MODEL):
                self._engine = EdgeAIEngine()
                self._model_label.setText("内置模型缺失，已回退规则检测")
                return
            self._engine = EdgeAIEngine(_BUILTIN_MODEL)
            self._set_model_status("内置模型", os.path.basename(_BUILTIN_MODEL))
        elif idx == 1:                        # 规则检测
            self._engine = EdgeAIEngine()
            self._model_label.setText("规则/统计检测：转速偏差、温度>60°C、电流>10A 综合评分")
        else:                                 # 自定义
            if not self._custom_path:
                self._on_pick_custom()
            elif os.path.isfile(self._custom_path):
                self._engine = EdgeAIEngine(self._custom_path)
                self._set_model_status("自定义模型", os.path.basename(self._custom_path))
        logger.log("边缘AI模型切换", self._model_combo.currentText())

    def _set_model_status(self, kind: str, name: str) -> None:
        if self._engine.using_onnx:
            self._model_label.setText(f"{kind}：{name}（ONNX 推理）")
        else:
            err = self._engine._load_error or "未知原因"
            self._model_label.setText(f"{name} 加载失败（{err}），已回退规则检测")

    def _on_pick_custom(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ONNX 模型", "", "ONNX 模型 (*.onnx)")
        if not path:
            # 未选文件：若当前在“自定义”项但无路径，回到内置
            if self._model_combo.currentIndex() == 2 and not self._custom_path:
                self._model_combo.setCurrentIndex(0)
            return
        self._custom_path = path
        self._model_combo.blockSignals(True)
        self._model_combo.setCurrentIndex(2)
        self._model_combo.blockSignals(False)
        self._engine = EdgeAIEngine(path)
        self._set_model_status("自定义模型", os.path.basename(path))
        logger.log("加载边缘AI模型", os.path.basename(path))

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
