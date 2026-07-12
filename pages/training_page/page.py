"""模型训练页面：模型/超参数选择、数据采集/导入、清洗、训练、Loss 曲线、ONNX 导出。"""
from __future__ import annotations

import csv
import os
from collections import deque
from datetime import datetime

import numpy as np
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import (
    CMD_START, MONITOR_REFRESH_MS, TRAIN_LOSSES, TRAIN_MODEL_TYPES,
    TRAIN_OPTIMIZERS, TRAIN_SCHEDULERS,
)
from logs.operation_logger import logger
from training.trainer import Trainer
from training.drl_trainer import DRLTrainer
from widgets.fault_sweep_dialogs import FaultCriteriaDialog, SweepConfigDialog

from .model_panels import DRLPanel, MODEL_PANELS, ModelPanel
from .model_struct import describe_model

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:
    _PG_OK = False

# 特征中文名（顺序须与 _frame_to_row 一致）
_FEATURE_NAMES = [
    "实际转速", "给定转速",
    "实际电流", "给定电流",
    "实际转矩", "给定转矩",
    "转子角度", "温度",
]

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "training", "data")

# 自动标注默认判据（数字孪生量级；真机可在「故障判据」对话框调整）
_DEFAULT_FAULT_CFG = {
    "temp_fault": 85.0,     # 过温故障 °C
    "temp_warn": 65.0,      # 温度告警 °C
    "cur_fault": 0.0,       # 过流故障 A（0=禁用）
    "cur_warn": 0.0,        # 过流告警 A（0=禁用）
    "sensor_q_warn": 0.5,   # 传感器质量告警下限
    "use_bus": True,        # 是否采用母线状态判据（过压→故障/欠压斩波→告警）
}


def _frame_to_row(f: TelemetryFrame) -> list:
    return [f.speed_actual, f.speed_target,
            f.current_actual, f.current_target,
            f.torque_actual, f.torque_target,
            f.angle_actual, f.temperature]


def _auto_label(f: TelemetryFrame, cfg: dict = None) -> float:
    """按遥测健康度自动判定标签：0.0 正常 / 0.5 警告 / 1.0 故障。

    判据可自定义（cfg）：温度、电流阈值、传感器质量、母线状态。
    默认采用数字孪生/下位机已算好的健康信号，无需人工判断。
    """
    cfg = cfg or _DEFAULT_FAULT_CFG
    bus = getattr(f, "bus_state", "normal")
    cur = abs(getattr(f, "current_actual", 0.0))
    use_bus = cfg.get("use_bus", True)

    # 故障：母线过压跳闸 / 过温 / 过流
    if ((use_bus and bus == "ov")
            or f.temperature >= cfg["temp_fault"]
            or (cfg.get("cur_fault", 0.0) > 0 and cur >= cfg["cur_fault"])):
        return 1.0
    # 警告：欠压/制动斩波 / 温度偏高 / 电流偏高 / 传感器质量低 / 低速不可用
    if ((use_bus and bus in ("uv", "brake"))
            or f.temperature >= cfg["temp_warn"]
            or (cfg.get("cur_warn", 0.0) > 0 and cur >= cfg["cur_warn"])
            or getattr(f, "sensor_quality", 1.0) < cfg["sensor_q_warn"]
            or getattr(f, "low_speed_warn", False)):
        return 0.5
    return 0.0


# 模型与任务类型的一句话介绍（选中时显示）
_MODEL_DESCS = {
    "MLP (多层感知机)":
        "全连接网络：结构简单、训练快，表格型遥测特征的首选基线。",
    "1D-CNN (一维卷积)":
        "一维卷积提取局部模式：适合波形片段类特征，参数量小。",
    "LSTM (长短时记忆)":
        "循环网络记忆时序依赖：适合按时间顺序采集的序列数据，训练较慢。",
    "Transformer":
        "自注意力建模长程依赖：能力强但需要较多数据，小数据集易过拟合。",
    "随机森林 (Random Forest)":
        "决策树集成（scikit-learn）：无需归一化、不易过拟合，小数据稳健，"
        "训练秒级完成（无 Epoch/学习率概念）。",
    "支持向量机 (SVM)":
        "核方法（scikit-learn）：小样本高维表现好，数据量大时训练慢。",
}

_TASK_DESCS = {
    0: "回归：输出 0~1 连续异常分数，能表达“轻微异常(0.5)”这类中间状态。",
    1: "二分类：只判正常/异常两类，标签 ≥0.5 视为异常，界限分明。",
}


class TrainingPage(QWidget):
    def __init__(self, comm: CommManager, control_page=None) -> None:
        super().__init__()
        self._comm = comm
        self._ctrl = control_page
        self._latest = TelemetryFrame()
        self._raw: list[list] = []
        self._collecting = False
        self._fault_cfg = dict(_DEFAULT_FAULT_CFG)
        self._sweeping = False
        self._trainer = Trainer()
        self._drl_trainer = DRLTrainer()
        self._tr_losses: deque = deque(maxlen=500)
        self._val_losses: deque = deque(maxlen=500)

        os.makedirs(_DATA_DIR, exist_ok=True)

        root = QVBoxLayout(self)
        title = QLabel("模型训练")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        top = QHBoxLayout()
        top.addWidget(self._build_collect_box(), 1)
        top.addWidget(self._build_clean_box(), 1)
        root.addLayout(top)

        self._tab = QTabWidget()
        self._tab.addTab(self._build_fault_tab(), "电机故障分类")
        self._tab.addTab(self._build_drl_tab(), "深度强化学习(DRL) - 学习MPC")
        root.addWidget(self._tab, 1)
        self._update_model_desc()

        comm.telemetryReceived.connect(self._on_telemetry)
        self._trainer.epochDone.connect(self._on_epoch)
        self._trainer.finished.connect(self._on_train_done)
        self._trainer.error.connect(lambda e: self._log(f"[错误] {e}"))
        self._drl_trainer.epochDone.connect(self._on_epoch)
        self._drl_trainer.finished.connect(self._on_train_done)
        self._drl_trainer.error.connect(lambda e: self._log(f"[DRL错误] {e}"))
        self._drl_trainer.mpcInfo.connect(self._on_mpc_info)
        self._drl_trainer.datasetReady.connect(
            lambda n: self._mpc_dataset_label.setText(f"回放缓冲区：{n} 条"))

    # ─── 数据采集 ───────────────────────────────────────────
    def _build_collect_box(self) -> QGroupBox:
        box = QGroupBox("数据采集")
        v = QVBoxLayout(box)

        # 输入特征选择：决定采集哪些遥测量作为模型输入
        feat_box = QGroupBox("输入特征（模型的输入维度 = 勾选数）")
        fg = QHBoxLayout(feat_box)
        fg.setContentsMargins(6, 2, 6, 2)
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        self._feat_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(_FEATURE_NAMES):
            cb = QCheckBox(name)
            cb.setChecked(True)
            cb.toggled.connect(self._on_feature_toggled)
            self._feat_checks[name] = cb
            (col1 if i % 2 == 0 else col2).addWidget(cb)
        fg.addLayout(col1)
        fg.addLayout(col2)
        v.addWidget(feat_box)

        f = QFormLayout()
        self._label_combo = QComboBox()
        self._label_combo.addItems(["正常 (0.0)", "警告 (0.5)", "异常 (1.0)"])
        self._collect_dur = QSpinBox(); self._collect_dur.setRange(1, 3600); self._collect_dur.setValue(10)
        f.addRow("数据标签", self._label_combo)
        f.addRow("采集时长 (s)", self._collect_dur)
        v.addLayout(f)

        # 自动标注：按遥测健康度实时判定每帧标签，无需人工选标签
        self._chk_auto_label = QCheckBox("自动标注故障（按遥测健康度：过压/过温/传感器异常）")
        self._chk_auto_label.setToolTip(
            "勾选后忽略上面的「数据标签」，每帧标签由母线状态、温度、传感器质量\n"
            "等健康信号自动判定：正常 0.0 / 警告 0.5 / 故障 1.0。\n"
            "判据可在「故障判据…」中自定义。")
        self._chk_auto_label.toggled.connect(
            lambda c: self._label_combo.setEnabled(not c))
        v.addWidget(self._chk_auto_label)

        auto_h = QHBoxLayout()
        btn_fault = QPushButton("故障判据…")
        btn_fault.setToolTip("自定义自动标注的故障/告警阈值（温度、电流、传感器、母线）")
        btn_fault.clicked.connect(self._on_fault_criteria)
        self._btn_sweep = QPushButton("扫频采集…")
        self._btn_sweep.setToolTip("自动遍历「转速×负载」测试点网格采集，免逐点手动采样")
        self._btn_sweep.clicked.connect(self._on_sweep)
        auto_h.addWidget(btn_fault)
        auto_h.addWidget(self._btn_sweep)
        auto_h.addStretch(1)
        v.addLayout(auto_h)

        self._auto_label_hint = QLabel("")
        self._auto_label_hint.setStyleSheet("color: #8fa3b8;")
        v.addWidget(self._auto_label_hint)

        h = QHBoxLayout()
        self._btn_collect = QPushButton("开始采集")
        self._btn_collect.setObjectName("PrimaryButton")
        self._btn_collect.clicked.connect(self._on_toggle_collect)
        btn_import = QPushButton("导入 CSV")
        btn_import.clicked.connect(self._on_import_csv)
        btn_save = QPushButton("保存数据集")
        btn_save.clicked.connect(self._on_save_dataset)
        h.addWidget(self._btn_collect)
        h.addWidget(btn_import)
        h.addWidget(btn_save)
        v.addLayout(h)

        self._collect_status = QLabel("已采集：0 条")
        v.addWidget(self._collect_status)

        # 数据预览：展示前若干条，直观确认格式与规模
        self._preview = QTableWidget(0, len(_FEATURE_NAMES) + 1)
        self._preview.setHorizontalHeaderLabels(_FEATURE_NAMES + ["标签"])
        self._preview.verticalHeader().setVisible(False)
        self._preview.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview.setMinimumHeight(240)
        self._preview.horizontalHeader().setDefaultSectionSize(72)
        self._preview.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self._preview, 1)      # 占据采集框剩余空间
        self._preview_info = QLabel("数据格式：特征 + 1 标签；暂无数据")
        self._preview_info.setStyleSheet("color: #8fa3b8;")
        v.addWidget(self._preview_info)
        return box

    def _selected_features(self) -> list[str]:
        return [n for n in _FEATURE_NAMES if self._feat_checks[n].isChecked()]

    def _on_feature_toggled(self, _checked: bool) -> None:
        sel = self._selected_features()
        sender = self.sender()
        if not sel:                       # 至少保留一个特征
            sender.blockSignals(True)
            sender.setChecked(True)
            sender.blockSignals(False)
            return
        if self._raw:
            ans = QMessageBox.question(
                self, "变更输入特征",
                "已采集的数据列与新特征选择不一致，需要清空现有数据。\n"
                "确定变更并清空吗？")
            if ans != QMessageBox.Yes:
                sender.blockSignals(True)
                sender.setChecked(not sender.isChecked())
                sender.blockSignals(False)
                return
            self._raw.clear()
            self._collect_status.setText("已采集：0 条")
        self._refresh_preview()

    def _refresh_preview(self, head_n: int = 20) -> None:
        """刷新数据预览表：列随所选特征，前 head_n 条 + 形状说明。"""
        sel = self._selected_features()
        self._preview.setColumnCount(len(sel) + 1)
        self._preview.setHorizontalHeaderLabels(sel + ["标签"])
        rows = self._raw[:head_n]
        self._preview.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                self._preview.setItem(r, c, QTableWidgetItem(f"{val:.3f}"))
        n = len(self._raw)
        if n:
            ncol = len(self._raw[0])
            mb = n * ncol * 8 / 1048576   # float64 估算
            self._preview_info.setText(
                f"共 {n} 条 × {ncol} 列（{ncol - 1} 特征 + 1 标签），"
                f"约 {mb:.2f} MB；上表为前 {len(rows)} 条")
        else:
            self._preview_info.setText(
                f"数据格式：{len(sel)} 特征 + 1 标签；暂无数据")

    # ─── 数据清洗 ───────────────────────────────────────────
    def _build_clean_box(self) -> QGroupBox:
        box = QGroupBox("数据清洗")
        f = QFormLayout(box)
        self._chk_dedup = QCheckBox("去重（相邻完全相同行）"); self._chk_dedup.setChecked(True)
        self._chk_norm = QCheckBox("归一化（Z-score）"); self._chk_norm.setChecked(True)
        self._chk_drop_nan = QCheckBox("删除含 NaN/Inf 行"); self._chk_drop_nan.setChecked(True)
        self._speed_min = QDoubleSpinBox(); self._speed_min.setRange(-1e5, 1e5); self._speed_min.setValue(-5000)
        self._speed_max = QDoubleSpinBox(); self._speed_max.setRange(-1e5, 1e5); self._speed_max.setValue(5000)
        f.addRow(self._chk_dedup)
        f.addRow(self._chk_norm)
        f.addRow(self._chk_drop_nan)
        f.addRow("转速过滤 min", self._speed_min)
        f.addRow("转速过滤 max", self._speed_max)
        btn_clean = QPushButton("执行清洗")
        btn_clean.clicked.connect(self._on_clean)
        f.addRow("", btn_clean)
        self._clean_status = QLabel("清洗状态：--")
        f.addRow(self._clean_status)
        self._X_clean: np.ndarray | None = None
        self._y_clean: np.ndarray | None = None
        return box

    # ─── 故障分类 Tab ────────────────────────────────────────
    def _build_fault_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._build_model_box())
        btn_struct = QPushButton("查看模型结构")
        btn_struct.clicked.connect(self._show_model_structure)
        v.addWidget(btn_struct)
        v.addWidget(self._build_train_box())
        return w

    # ─── DRL Tab ─────────────────────────────────────────────
    def _build_drl_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self._drl_panel = DRLPanel()
        v.addWidget(self._drl_panel)

        # MPC 专家信息面板（勾选 mpc_reference 时显示）
        self._mpc_info_box = QGroupBox("MPC 专家参考信息")
        mpc_f = QFormLayout(self._mpc_info_box)
        self._mpc_params_label = QLabel("N=10  Q=1.0  R=0.01  u_max=24.0")
        self._mpc_steps_label = QLabel("已生成专家轨迹：0 步")
        self._mpc_last_label = QLabel("最近一步：--")
        self._mpc_dataset_label = QLabel("回放缓冲区：0 条")
        mpc_f.addRow("MPC参数", self._mpc_params_label)
        mpc_f.addRow("专家轨迹", self._mpc_steps_label)
        mpc_f.addRow("最近动作", self._mpc_last_label)
        mpc_f.addRow("数据集", self._mpc_dataset_label)
        # 数据集操作按钮
        ds_h = QHBoxLayout()
        btn_gen = QPushButton("生成专家数据集")
        btn_gen.clicked.connect(self._on_gen_expert_dataset)
        btn_export_ds = QPushButton("导出数据集")
        btn_export_ds.clicked.connect(self._on_export_expert_dataset)
        btn_import_ds = QPushButton("导入数据集")
        btn_import_ds.clicked.connect(self._on_import_expert_dataset)
        for b in (btn_gen, btn_export_ds, btn_import_ds):
            ds_h.addWidget(b)
        mpc_f.addRow(ds_h)
        v.addWidget(self._mpc_info_box)
        self._mpc_info_box.setVisible(False)
        self._drl_panel.mpc_ref.stateChanged.connect(
            lambda s: self._mpc_info_box.setVisible(bool(s)))

        h = QHBoxLayout()
        self._btn_drl_train = QPushButton("开始DRL训练")
        self._btn_drl_train.setObjectName("PrimaryButton")
        self._btn_drl_train.clicked.connect(self._on_start_drl_train)
        self._btn_drl_stop = QPushButton("中止训练")
        self._btn_drl_stop.setEnabled(False)
        self._btn_drl_stop.clicked.connect(self._on_stop_train)
        btn_drl_curve = QPushButton("查看曲线")
        btn_drl_curve.clicked.connect(self._show_curve_window)
        h.addWidget(self._btn_drl_train)
        h.addWidget(self._btn_drl_stop)
        h.addWidget(btn_drl_curve)
        h.addStretch(1)
        v.addLayout(h)
        self._drl_log = QPlainTextEdit(); self._drl_log.setReadOnly(True); self._drl_log.setMaximumHeight(80)
        self._drl_log.setPlaceholderText(
            "DRL 训练日志：显示环境步数、回报、MPC 专家轨迹与训练状态")
        v.addWidget(self._drl_log)
        v.addStretch(1)
        return w

    # ─── 模型选择 ───────────────────────────────────────────
    def _build_model_box(self) -> QGroupBox:
        _FAULT_MODELS = [t for t in TRAIN_MODEL_TYPES if not t.startswith("深度强化学习")]
        box = QGroupBox("模型选择与结构超参数")
        h = QHBoxLayout(box)

        left = QFormLayout()
        self._model_combo = QComboBox()
        self._model_combo.addItems(_FAULT_MODELS)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        left.addRow("模型类型", self._model_combo)

        self._task_combo = QComboBox()
        self._task_combo.addItems(["回归 (异常分数)", "二分类 (正常/异常)"])
        self._task_combo.currentIndexChanged.connect(self._update_model_desc)
        left.addRow("任务类型", self._task_combo)

        self._model_desc = QLabel("")
        self._model_desc.setWordWrap(True)
        self._model_desc.setStyleSheet("color: #8fa3b8;")
        self._model_desc.setMaximumWidth(320)
        left.addRow(self._model_desc)
        h.addLayout(left, 0)

        self._model_stack = QStackedWidget()
        self._model_panels: dict[str, ModelPanel] = {}
        for name in _FAULT_MODELS:
            panel = MODEL_PANELS[name]()
            self._model_panels[name] = panel
            self._model_stack.addWidget(panel)
        h.addWidget(self._model_stack, 1)
        return box

    # ─── 训练参数 ───────────────────────────────────────────
    def _build_train_box(self) -> QGroupBox:
        box = QGroupBox("训练参数")
        h = QHBoxLayout(box)
        f = QFormLayout()
        self._epochs = QSpinBox(); self._epochs.setRange(1, 2000); self._epochs.setValue(50)
        self._lr = QDoubleSpinBox(); self._lr.setRange(1e-6, 1.0); self._lr.setDecimals(6); self._lr.setValue(1e-3)
        self._batch = QSpinBox(); self._batch.setRange(1, 1024); self._batch.setValue(32)
        self._val_split = QDoubleSpinBox(); self._val_split.setRange(0.05, 0.5); self._val_split.setSingleStep(0.05); self._val_split.setValue(0.2)
        self._weight_decay = QDoubleSpinBox(); self._weight_decay.setRange(0, 1.0); self._weight_decay.setDecimals(6); self._weight_decay.setValue(0.0)
        self._epochs.setToolTip("全数据集完整迭代的次数。过多会过拟合"
                                "（验证 Loss 先降后升即是信号）")
        self._lr.setToolTip("每步参数更新的幅度，最重要的超参数：过大发散、"
                            "过小收敛慢。Adam 常用 1e-3 起步")
        self._batch.setToolTip("每次梯度更新使用的样本数。大→训练稳定但"
                               "泛化略差；小→噪声大但有正则效果")
        self._val_split.setToolTip("留出这一比例的数据不参与训练，"
                                   "仅用于监控过拟合（验证 Loss）")
        self._weight_decay.setToolTip("L2 正则化强度，抑制过拟合。"
                                      "常用 0 或 1e-4")
        f.addRow("训练轮数 (Epochs)", self._epochs)
        f.addRow("学习率 (LR)", self._lr)
        f.addRow("批大小 (Batch)", self._batch)
        f.addRow("验证集比例", self._val_split)
        f.addRow("权重衰减 (L2)", self._weight_decay)
        h.addLayout(f)

        f2 = QFormLayout()
        self._optimizer = QComboBox(); self._optimizer.addItems(TRAIN_OPTIMIZERS)
        self._loss_fn = QComboBox(); self._loss_fn.addItems(TRAIN_LOSSES)
        self._scheduler = QComboBox(); self._scheduler.addItems(TRAIN_SCHEDULERS)
        self._seed = QSpinBox(); self._seed.setRange(0, 99999); self._seed.setValue(42)
        self._optimizer.setToolTip("参数更新算法：Adam 自适应学习率、省心"
                                   "（默认首选）；SGD 需精调但泛化常更好")
        self._loss_fn.setToolTip("误差的度量方式：回归用 MSE/MAE；"
                                 "二分类用交叉熵（BCE）")
        self._scheduler.setToolTip("训练过程中自动衰减学习率：前期大步快走，"
                                   "后期小步精调，收敛更稳")
        self._seed.setToolTip("固定随机初始化/数据打乱的种子，"
                              "同一种子结果可复现")
        f2.addRow("优化器", self._optimizer)
        f2.addRow("损失函数", self._loss_fn)
        f2.addRow("学习率调度", self._scheduler)
        f2.addRow("随机种子", self._seed)
        h.addLayout(f2)

        v2 = QVBoxLayout()
        self._btn_train = QPushButton("开始训练")
        self._btn_train.setObjectName("PrimaryButton")
        self._btn_train.clicked.connect(self._on_start_train)
        self._btn_stop_train = QPushButton("中止训练")
        self._btn_stop_train.clicked.connect(self._on_stop_train)
        self._btn_stop_train.setEnabled(False)
        self._btn_export = QPushButton("导出 ONNX")
        self._btn_export.clicked.connect(self._on_export)
        self._btn_export.setEnabled(False)
        btn_curve = QPushButton("查看曲线")
        btn_curve.clicked.connect(self._show_curve_window)
        self._progress = QProgressBar(); self._progress.setRange(0, 100); self._progress.setValue(0)
        self._train_log = QPlainTextEdit(); self._train_log.setReadOnly(True); self._train_log.setMaximumHeight(80)
        self._train_log.setPlaceholderText(
            "训练日志：开始训练后逐轮显示 train/val 损失、进度与完成/错误信息")
        for b in (self._btn_train, self._btn_stop_train, self._btn_export, btn_curve):
            v2.addWidget(b)
        v2.addWidget(self._progress)
        v2.addWidget(self._train_log)
        h.addLayout(v2)
        return box

    def _show_curve_window(self) -> None:
        if not _PG_OK:
            QMessageBox.information(self, "提示", "未安装 pyqtgraph，无法显示曲线")
            return
        # 每次都重建，确保曲线对象与当前数据同步
        if hasattr(self, "_curve_win"):
            try:
                self._curve_win.close()
            except Exception:
                pass
        win = QWidget(None, Qt.Window)
        win.setWindowTitle("训练曲线")
        win.resize(800, 400)
        h = QHBoxLayout(win)
        self._plot = pg.PlotWidget(title="Loss")
        self._plot.setBackground("#10131a"); self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.addLegend(); self._plot.setLabel("left", "Loss"); self._plot.setLabel("bottom", "Epoch")
        self._curve_tr = self._plot.plot(pen=pg.mkPen("#4fc3f7", width=2), name="Train Loss")
        self._curve_val = self._plot.plot(pen=pg.mkPen("#ffb74d", width=2), name="Val Loss")
        xs = getattr(self, "_epochs_list", [])
        if xs and self._tr_losses:
            self._curve_tr.setData(xs, list(self._tr_losses))
            self._curve_val.setData(xs, list(self._val_losses))

        self._plot_acc = pg.PlotWidget(title="准确率")
        self._plot_acc.setBackground("#10131a"); self._plot_acc.showGrid(x=True, y=True, alpha=0.3)
        self._plot_acc.addLegend(); self._plot_acc.setLabel("left", "Accuracy"); self._plot_acc.setLabel("bottom", "Epoch")
        self._plot_acc.setYRange(0, 1)
        self._curve_acc_tr = self._plot_acc.plot(pen=pg.mkPen("#81c784", width=2), name="Train Acc")
        self._curve_acc_val = self._plot_acc.plot(pen=pg.mkPen("#f48fb1", width=2), name="Val Acc")

        h.addWidget(self._plot_acc, 1)
        h.addWidget(self._plot, 1)
        win.show()
        self._curve_win = win

    # ─── slots ──────────────────────────────────────────────
    def _on_model_changed(self, idx: int) -> None:
        self._model_stack.setCurrentIndex(idx)
        name = self._model_combo.currentText()
        is_sk = name.startswith(("随机森林", "支持向量机"))
        for w in (self._epochs, self._lr, self._batch, self._weight_decay,
                  self._optimizer, self._loss_fn, self._scheduler, self._val_split):
            w.setEnabled(not is_sk)
        self._update_model_desc()

    def _update_model_desc(self) -> None:
        if not hasattr(self, "_model_desc"):
            return
        model = _MODEL_DESCS.get(self._model_combo.currentText(), "")
        task = _TASK_DESCS.get(self._task_combo.currentIndex(), "")
        self._model_desc.setText(f"{model}\n{task}")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        if self._collecting:
            if self._chk_auto_label.isChecked():
                label = _auto_label(frame, self._fault_cfg)
            else:
                label = [0.0, 0.5, 1.0][self._label_combo.currentIndex()]
            full = _frame_to_row(frame)
            row = [full[_FEATURE_NAMES.index(n)]
                   for n in self._selected_features()]
            self._raw.append(row + [label])
            self._collect_status.setText(f"已采集：{len(self._raw)} 条")
            if self._chk_auto_label.isChecked():
                self._update_auto_label_hint()

    def _update_auto_label_hint(self) -> None:
        labels = [r[-1] for r in self._raw]
        n0 = labels.count(0.0)
        n5 = labels.count(0.5)
        n1 = labels.count(1.0)
        cur = _auto_label(self._latest, self._fault_cfg)
        cur_txt = {0.0: "正常", 0.5: "警告", 1.0: "故障"}[cur]
        prefix = "扫频中" if self._sweeping else "自动标注中"
        self._auto_label_hint.setText(
            f"{prefix} → 当前：{cur_txt}　｜　"
            f"正常 {n0} / 警告 {n5} / 故障 {n1}")

    # ─── 自定义故障判据 ──────────────────────────────────────
    def _on_fault_criteria(self) -> None:
        dlg = FaultCriteriaDialog(self._fault_cfg, self)
        if dlg.exec():
            self._fault_cfg = dlg.values()
            logger.log("故障判据更新", str(self._fault_cfg))

    # ─── 扫频采集 ────────────────────────────────────────────
    def _on_sweep(self) -> None:
        if self._sweeping:
            self._sweep_finish(aborted=True)
            return
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            QMessageBox.warning(self, "无法扫频",
                                "请先启动仿真（虚拟电机）或连接真机。")
            return
        has_load = self._comm.is_sim_running()
        dlg = SweepConfigDialog(self, has_load=has_load)
        if not dlg.exec():
            return
        cfg = dlg.values()
        self._sweep_points = [(s, l) for s in cfg["speeds"] for l in cfg["loads"]]
        self._sweep_dwell = cfg["dwell_s"]
        self._sweep_collect = cfg["collect_s"]
        self._sweep_idx = 0
        self._sweeping = True
        self._chk_auto_label.setChecked(True)   # 扫频强制自动标注
        self._btn_sweep.setText("停止扫频")
        self._btn_collect.setEnabled(False)
        logger.log("开始扫频采集",
                   f"{len(self._sweep_points)} 点 稳定{self._sweep_dwell}s "
                   f"采集{self._sweep_collect}s")
        self._sweep_next()

    def _sweep_next(self) -> None:
        if not self._sweeping:
            return
        if self._sweep_idx >= len(self._sweep_points):
            self._sweep_finish()
            return
        spd, load = self._sweep_points[self._sweep_idx]
        self._comm.send_frame(encode_frame(CMD_START, f"target={spd}".encode("utf-8")))
        if self._comm.is_sim_running():
            self._comm.set_sim_load(load)
        self._auto_label_hint.setText(
            f"扫频 {self._sweep_idx + 1}/{len(self._sweep_points)}："
            f"转速 {spd:.0f} rpm，负载 {load:.2f} N·m —— 稳定中…")
        QTimer.singleShot(int(self._sweep_dwell * 1000), self._sweep_collect_start)

    def _sweep_collect_start(self) -> None:
        if not self._sweeping:
            return
        self._collecting = True
        QTimer.singleShot(int(self._sweep_collect * 1000), self._sweep_collect_end)

    def _sweep_collect_end(self) -> None:
        if not self._sweeping:
            return
        self._collecting = False
        self._sweep_idx += 1
        self._refresh_preview()
        self._sweep_next()

    def _sweep_finish(self, aborted: bool = False) -> None:
        self._sweeping = False
        self._collecting = False
        if self._comm.is_sim_running():
            self._comm.set_sim_load(0.0)     # 卸载
        self._btn_sweep.setText("扫频采集…")
        self._btn_collect.setEnabled(True)
        self._refresh_preview()
        msg = "扫频已停止" if aborted else "扫频完成"
        self._auto_label_hint.setText(f"{msg}，共采集 {len(self._raw)} 条")
        logger.log("扫频采集", f"{msg}，共 {len(self._raw)} 条")

    def _on_toggle_collect(self) -> None:
        if not self._collecting:
            dur = self._collect_dur.value()
            self._collecting = True
            self._btn_collect.setText("停止采集")
            mode = "自动标注" if self._chk_auto_label.isChecked() else self._label_combo.currentText()
            logger.log("开始数据采集", f"标签={mode} 时长={dur}s")
            QTimer.singleShot(dur * 1000, self._stop_collect)
        else:
            self._stop_collect()

    def _stop_collect(self) -> None:
        if not self._collecting:
            return
        self._collecting = False
        self._btn_collect.setText("开始采集")
        self._refresh_preview()
        logger.log("停止数据采集", f"共 {len(self._raw)} 条")

    def _on_import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入 CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                rows = [list(map(float, r)) for r in reader if r]
            expect = len(self._selected_features()) + 1
            if rows and len(rows[0]) != expect:
                QMessageBox.warning(
                    self, "列数不匹配",
                    f"该 CSV 每行 {len(rows[0])} 列，当前特征选择需要 {expect} 列"
                    f"（{expect - 1} 特征 + 1 标签）。\n"
                    "请调整上方「输入特征」勾选以匹配文件，再重新导入。")
                return
            self._raw.extend(rows)
            self._collect_status.setText(f"已采集：{len(self._raw)} 条")
            self._refresh_preview()
            logger.log("导入 CSV", f"{os.path.basename(path)}  {len(rows)} 行")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def _on_save_dataset(self) -> None:
        if not self._raw:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_DATA_DIR, f"dataset_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self._selected_features() + ["label"])
            w.writerows(self._raw)
        logger.log("保存数据集", os.path.basename(path))
        QMessageBox.information(self, "已保存", path)

    def _on_clean(self) -> None:
        if not self._raw:
            QMessageBox.information(self, "提示", "请先采集或导入数据")
            return
        data = np.array(self._raw, dtype=np.float64)
        n0 = len(data)
        n_feat = data.shape[1] - 1
        sel = self._selected_features()

        if self._chk_drop_nan.isChecked():
            data = data[np.isfinite(data).all(axis=1)]

        # 转速过滤只在选了 speed_actual 时按其所在列执行
        if "speed_actual" in sel and len(sel) == n_feat:
            col = sel.index("speed_actual")
            mn, mx = self._speed_min.value(), self._speed_max.value()
            data = data[(data[:, col] >= mn) & (data[:, col] <= mx)]

        if self._chk_dedup.isChecked():
            _, idx = np.unique(data, axis=0, return_index=True)
            data = data[np.sort(idx)]

        X, y = data[:, :n_feat], data[:, n_feat]

        if self._chk_norm.isChecked() and len(X) > 1:
            self._mean = X.mean(axis=0)
            self._std = X.std(axis=0) + 1e-8
            X = (X - self._mean) / self._std
        else:
            self._mean = np.zeros(n_feat)
            self._std = np.ones(n_feat)

        self._X_clean = X.astype(np.float32)
        self._y_clean = y.astype(np.float32)
        msg = f"清洗完成：{n0} → {len(X)} 条"
        self._clean_status.setText(msg)
        self._log(msg)
        logger.log("数据清洗", msg)

    def _on_start_train(self) -> None:
        if self._X_clean is None or len(self._X_clean) < 10:
            QMessageBox.warning(self, "数据不足", "请先执行数据清洗，且至少需要 10 条数据")
            return
        np.random.seed(self._seed.value())
        try:
            import torch
            torch.manual_seed(self._seed.value())
        except ImportError:
            pass

        self._tr_losses.clear()
        self._val_losses.clear()
        self._epochs_list: list[int] = []
        self._btn_train.setEnabled(False)
        self._btn_stop_train.setEnabled(True)
        self._show_curve_window()
        self._btn_export.setEnabled(False)
        self._progress.setValue(0)
        self._total_epochs = self._epochs.value()

        model_name = self._model_combo.currentText()
        hyper = self._model_panels[model_name].values()
        logger.log("开始训练",
                   f"模型={model_name} epochs={self._total_epochs} "
                   f"lr={self._lr.value()} opt={self._optimizer.currentText()} "
                   f"loss={self._loss_fn.currentText()} hp={hyper}")

        self._trainer.start(
            self._X_clean, self._y_clean,
            model_name=model_name,
            epochs=self._total_epochs,
            lr=self._lr.value(),
            batch_size=self._batch.value(),
            val_split=self._val_split.value(),
            optimizer=self._optimizer.currentText(),
            loss_name=self._loss_fn.currentText(),
            scheduler=self._scheduler.currentText(),
            weight_decay=self._weight_decay.value(),
            hyper=hyper,
        )

    def _on_stop_train(self) -> None:
        self._trainer.stop()
        logger.log("中止训练")

    def _on_epoch(self, ep: int, tr_loss: float, val_loss: float) -> None:
        self._tr_losses.append(tr_loss)
        self._val_losses.append(val_loss)
        if not hasattr(self, "_epochs_list"):
            self._epochs_list = []
        self._epochs_list.append(ep)
        pct = int(ep / max(1, self._total_epochs) * 100)
        self._progress.setValue(pct)
        self._log(f"Epoch {ep}/{self._total_epochs}  train={tr_loss:.5f}  val={val_loss:.5f}")
        if _PG_OK and hasattr(self, "_curve_win") and self._curve_win.isVisible():
            xs = self._epochs_list
            self._curve_tr.setData(xs, list(self._tr_losses))
            self._curve_val.setData(xs, list(self._val_losses))

    def _on_start_drl_train(self) -> None:
        params = self._drl_panel.values()
        # 读取电机控制页面的MPC配置
        mpc_cfg = {}
        if self._ctrl is not None:
            mpc_panel = self._ctrl._panels.get("模型预测控制(MPC)")
            if mpc_panel is not None:
                mpc_cfg = mpc_panel.values()
        params["mpc_cfg"] = mpc_cfg
        logger.log("开始DRL训练", f"算法={params['algorithm']} 聚合={params['aggregation']} 步数={params['env_steps']}")
        self._btn_drl_train.setEnabled(False)
        self._btn_drl_stop.setEnabled(True)
        self._total_epochs = params["env_steps"]
        self._tr_losses.clear(); self._val_losses.clear()
        self._epochs_list = []
        self._show_curve_window()
        if hasattr(self, "_mpc_params_label"):
            N = mpc_cfg.get("prediction_horizon", 10)
            Q = mpc_cfg.get("weight_q", 1.0)
            R = mpc_cfg.get("weight_r", 0.01)
            u_max = mpc_cfg.get("u_max", 24.0)
            self._mpc_params_label.setText(f"N={N}  Q={Q}  R={R}  u_max={u_max}  算法={params['algorithm']}")
            self._mpc_steps_label.setText("已生成专家轨迹：0 步")
            self._mpc_dataset_label.setText("回放缓冲区：0 条")
        self._drl_trainer.start(params)

    def _on_train_done(self, msg: str) -> None:
        for btn in (self._btn_train, self._btn_drl_train):
            btn.setEnabled(True)
        for btn in (self._btn_stop_train, self._btn_drl_stop):
            btn.setEnabled(False)
        is_torch = not self._model_combo.currentText().startswith(("随机森林", "支持向量机"))
        self._btn_export.setEnabled(is_torch)
        self._progress.setValue(100)
        self._log(msg)
        logger.log("训练完成", msg)

    def _log(self, msg: str) -> None:
        self._train_log.appendPlainText(msg)
        if hasattr(self, "_drl_log"):
            self._drl_log.appendPlainText(msg)

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 ONNX 模型", "motor_anomaly.onnx", "ONNX (*.onnx)"
        )
        if not path:
            return
        try:
            self._trainer.export_onnx(path)
            logger.log("导出 ONNX", os.path.basename(path))
            QMessageBox.information(self, "导出成功", f"已保存到：\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _show_model_structure(self) -> None:
        name = self._model_combo.currentText()
        lines = describe_model(name, self._model_panels[name].values())
        win = QWidget(None, Qt.Window)
        win.setWindowTitle(f"模型结构 — {name}")
        win.resize(500, 350)
        txt = QPlainTextEdit(win)
        txt.setReadOnly(True)
        txt.setPlainText("\n".join(lines))
        QVBoxLayout(win).addWidget(txt)
        win.show()
        self._struct_win = win

    def _on_mpc_info(self, steps: int, buf_size: int, last_state: str, last_act: float, last_rew: float) -> None:
        if not hasattr(self, "_mpc_steps_label"):
            return
        self._mpc_steps_label.setText(f"已生成专家轨迹：{steps} 步")
        self._mpc_dataset_label.setText(f"回放缓冲区：{buf_size} 条")
        act_v = last_act * 24.0
        self._mpc_last_label.setText(f"{last_state}  控制量={act_v:.1f}V  奖励={last_rew:.4f}")

    def _on_gen_expert_dataset(self) -> None:
        params = self._drl_panel.values()
        if self._ctrl is not None:
            mpc_panel = self._ctrl._panels.get("模型预测控制(MPC)")
            if mpc_panel is not None:
                params["mpc_cfg"] = mpc_panel.values()
        self._mpc_steps_label.setText("正在生成专家数据集...")
        self._drl_trainer.generate_expert_dataset(params, n_steps=10_000)

    def _on_export_expert_dataset(self) -> None:
        import datetime
        path, _ = QFileDialog.getSaveFileName(
            self, "导出专家数据集",
            f"expert_dataset_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV (*.csv)"
        )
        if not path:
            return
        try:
            self._drl_trainer.save_expert_dataset(path)
            QMessageBox.information(self, "导出成功", f"已保存到：\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _on_import_expert_dataset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入专家数据集", "", "CSV (*.csv)")
        if not path:
            return
        try:
            n = self._drl_trainer.load_expert_dataset(path)
            self._mpc_dataset_label.setText(f"回放缓冲区：{n} 条（已从文件加载）")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
