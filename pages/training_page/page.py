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
    QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import (
    MONITOR_REFRESH_MS, TRAIN_LOSSES, TRAIN_MODEL_TYPES,
    TRAIN_OPTIMIZERS, TRAIN_SCHEDULERS,
)
from logs.operation_logger import logger
from training.trainer import Trainer
from training.drl_trainer import DRLTrainer

from .model_panels import DRLPanel, MODEL_PANELS, ModelPanel
from .model_struct import describe_model

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:
    _PG_OK = False

_FEATURE_NAMES = [
    "speed_actual", "speed_target",
    "current_actual", "current_target",
    "torque_actual", "torque_target",
    "angle_actual", "temperature",
]

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "training", "data")


def _frame_to_row(f: TelemetryFrame) -> list:
    return [f.speed_actual, f.speed_target,
            f.current_actual, f.current_target,
            f.torque_actual, f.torque_target,
            f.angle_actual, f.temperature]


class TrainingPage(QWidget):
    def __init__(self, comm: CommManager, control_page=None) -> None:
        super().__init__()
        self._comm = comm
        self._ctrl = control_page
        self._latest = TelemetryFrame()
        self._raw: list[list] = []
        self._collecting = False
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
        f = QFormLayout()
        self._label_combo = QComboBox()
        self._label_combo.addItems(["正常 (0.0)", "警告 (0.5)", "异常 (1.0)"])
        self._collect_dur = QSpinBox(); self._collect_dur.setRange(1, 3600); self._collect_dur.setValue(10)
        f.addRow("数据标签", self._label_combo)
        f.addRow("采集时长 (s)", self._collect_dur)
        v.addLayout(f)

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
        return box

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
        left.addRow("任务类型", self._task_combo)
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

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        if self._collecting:
            label_map = [0.0, 0.5, 1.0]
            label = label_map[self._label_combo.currentIndex()]
            self._raw.append(_frame_to_row(frame) + [label])
            self._collect_status.setText(f"已采集：{len(self._raw)} 条")

    def _on_toggle_collect(self) -> None:
        if not self._collecting:
            dur = self._collect_dur.value()
            self._collecting = True
            self._btn_collect.setText("停止采集")
            logger.log("开始数据采集", f"标签={self._label_combo.currentText()} 时长={dur}s")
            QTimer.singleShot(dur * 1000, self._stop_collect)
        else:
            self._stop_collect()

    def _stop_collect(self) -> None:
        if not self._collecting:
            return
        self._collecting = False
        self._btn_collect.setText("开始采集")
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
            self._raw.extend(rows)
            self._collect_status.setText(f"已采集：{len(self._raw)} 条")
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
            w.writerow(_FEATURE_NAMES + ["label"])
            w.writerows(self._raw)
        logger.log("保存数据集", os.path.basename(path))
        QMessageBox.information(self, "已保存", path)

    def _on_clean(self) -> None:
        if not self._raw:
            QMessageBox.information(self, "提示", "请先采集或导入数据")
            return
        data = np.array(self._raw, dtype=np.float64)
        n0 = len(data)

        if self._chk_drop_nan.isChecked():
            data = data[np.isfinite(data).all(axis=1)]

        mn, mx = self._speed_min.value(), self._speed_max.value()
        data = data[(data[:, 0] >= mn) & (data[:, 0] <= mx)]

        if self._chk_dedup.isChecked():
            _, idx = np.unique(data, axis=0, return_index=True)
            data = data[np.sort(idx)]

        X, y = data[:, :8], data[:, 8]

        if self._chk_norm.isChecked() and len(X) > 1:
            self._mean = X.mean(axis=0)
            self._std = X.std(axis=0) + 1e-8
            X = (X - self._mean) / self._std
        else:
            self._mean = np.zeros(8)
            self._std = np.ones(8)

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
