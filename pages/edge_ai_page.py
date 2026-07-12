"""边缘AI页面：本地 ONNX 推理 + 规则检测，实时显示异常分数。"""
import os
from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QVBoxLayout, QWidget, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QSpinBox,
)

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_REFRESH_MS
from edge_ai.engine import EdgeAIEngine
from logs.operation_logger import logger
from runtime_paths import resource_path
from widgets.radar_chart import RadarChart

# 内置模型：数字孪生训练的故障检测器（随包 motor_anomaly.onnx）
_BUILTIN_MODEL = str(resource_path("motor_anomaly.onnx"))

# 模型输入特征的中文名与单位，用于原始输出展示
_FEATURE_LABELS = [
    ("实际转速", "rpm"), ("给定转速", "rpm"),
    ("实际电流", "A"), ("给定电流", "A"),
    ("实际转矩", "N·m"), ("给定转矩", "N·m"),
    ("转子角度", "°"), ("温度", "°C"),
]

# 雷达图 6 维：(名称, 满量程) —— 各物理量按量程归一化才能同框比较
_RADAR_AXES = [
    ("转速", 3000.0),      # rpm
    ("电流", 10.0),        # A
    ("转矩", 5.0),         # N·m
    ("温度", 100.0),       # °C
    ("母线电压", 60.0),    # V
    ("异常分数", 1.0),     # 0~1
]


class EdgeAIPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._latest = TelemetryFrame()
        self._custom_path = ""     # 用户加载的自定义模型路径
        self._engine = EdgeAIEngine()   # 占位，_on_model_choice 里正式初始化
        self._hi_streak = 0        # 连续高分帧计数（时间去抖）
        self._confirmed = False    # 去抖后确认的故障态
        # 雷达图滑动窗口：最近 ~20 帧（约 10s）的 6 维快照，用于算平均值
        self._radar_win = deque(maxlen=20)

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

        # 时间去抖：连续 N 帧异常才确认故障，滤掉启动/沉降的单帧尖峰
        db = QHBoxLayout()
        db.addWidget(QLabel("故障确认帧数"))
        self._debounce = QSpinBox()
        self._debounce.setRange(1, 20)
        self._debounce.setValue(3)
        self._debounce.setToolTip(
            "时间去抖：分数需连续这么多帧 ≥0.6 才判“故障”。\n"
            "单帧尖峰（如启动限流、沉降瞬态）会被滤掉，\n"
            "真实故障会持续多帧因而被确认。1=不去抖。")
        db.addWidget(self._debounce)
        db.addWidget(QLabel("（每帧 0.5s；1=关闭去抖）"))
        db.addStretch(1)
        v.addLayout(db)
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
        h = QHBoxLayout(box)

        # 左：分数条 + 判定
        f = QFormLayout()
        self._score_bar = QProgressBar()
        self._score_bar.setRange(0, 100)
        self._score_bar.setTextVisible(True)
        self._raw_score_val = QLabel("--")
        self._label_val = QLabel("--")
        self._label_val.setObjectName("BigValue")
        self._detail_val = QLabel("--")
        f.addRow("异常分数(去抖后)", self._score_bar)
        f.addRow("模型原始分数", self._raw_score_val)
        f.addRow("状态", self._label_val)
        f.addRow("说明", self._detail_val)
        h.addLayout(f, 1)

        # 中：多维雷达图（当前值 vs 滑动窗口平均值）
        self._radar = RadarChart(_RADAR_AXES)
        h.addWidget(self._radar, 1)

        # 右：模型的 8 个输入特征原始值（模型此刻在judge什么）
        self._feat_table = QTableWidget(len(_FEATURE_LABELS), 2)
        self._feat_table.setHorizontalHeaderLabels(["模型输入特征", "当前值"])
        self._feat_table.verticalHeader().setVisible(False)
        self._feat_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._feat_table.horizontalHeader().setStretchLastSection(True)
        for i, (name, unit) in enumerate(_FEATURE_LABELS):
            self._feat_table.setItem(i, 0, QTableWidgetItem(f"{name} ({unit})"))
            self._feat_table.setItem(i, 1, QTableWidgetItem("--"))
        self._feat_table.setMaximumWidth(280)
        h.addWidget(self._feat_table)
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

        # 显示模型的 8 个输入特征原始值
        for i, val in enumerate(features):
            item = self._feat_table.item(i, 1)
            if item is not None:
                item.setText(f"{val:.2f}")

        # 时间去抖：连续 N 帧原始分数 ≥0.6 才确认为故障，
        # 单帧/瞬态尖峰（启动沉降、噪声）不触发，避免误报。
        need = self._debounce.value()
        raw_score = result.score
        if raw_score >= 0.6:
            self._hi_streak += 1
        else:
            self._hi_streak = 0
        self._confirmed = self._hi_streak >= need

        # 去抖后的展示分数：未确认时压到"警告"上限以下，不飙红
        shown = raw_score if self._confirmed else min(raw_score, 0.59)
        pct = int(shown * 100)
        self._score_bar.setValue(pct)
        self._score_bar.setFormat(f"{pct}%")
        self._raw_score_val.setText(
            f"{raw_score:.3f}"
            + (f"  (高分连续 {self._hi_streak}/{need} 帧)" if self._hi_streak else "")
        )

        if self._confirmed:
            label, detail, color = "异常", result.detail, "#ef5350"
        elif raw_score >= 0.6:
            label = "疑似（观察中）"
            detail = f"检测到高分，但未连续 {need} 帧，暂不判故障"
            color = "#ffa726"
        elif raw_score >= 0.3:
            label, detail, color = "警告", result.detail, "#ffa726"
        else:
            label, detail, color = "正常", result.detail, "#66bb6a"
        self._label_val.setText(label)
        self._detail_val.setText(detail)
        self._score_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
        )

        # 仅在去抖确认后记入历史，且每段故障只记一次（上升沿）
        if self._confirmed and self._hi_streak == need:
            self._history.appendPlainText(
                f"[异常] 原始分数={raw_score:.2f}  连续{need}帧确认  {result.detail}  "
                f"转速={f.speed_actual:.0f}rpm  电流={f.current_actual:.2f}A  "
                f"温度={f.temperature:.1f}°C"
            )

        # 六维雷达图：当前值 + 滑动窗口平均值（维度顺序对齐 _RADAR_AXES）
        radar_cur = [abs(f.speed_actual), abs(f.current_actual),
                     abs(f.torque_actual), f.temperature, f.vdc, raw_score]
        self._radar_win.append(radar_cur)
        radar_avg = [sum(col) / len(self._radar_win) for col in zip(*self._radar_win)]
        self._radar.set_values(radar_cur, radar_avg)
