"""Offline A/B observer and identification verification, no device writes."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QCheckBox, QToolButton)

from pages.dynamic_analysis_dialog import AnalysisWorker, double_spin


class AlgorithmValidationPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot_provider = None
        self.worker = None
        self.result = None
        self.source_path = None
        self.profiles = []
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(["当前同步 F1 缓冲", "同步 CSV", "演示 RL 数据（非实机）"])
        row.addWidget(self.source)
        load = QPushButton("选择 CSV…")
        load.clicked.connect(self._choose_csv)
        row.addWidget(load)
        self.source_label = QLabel("只读重放实测反馈 · 每次运行从零状态开始")
        row.addWidget(self.source_label, 1)
        help_button = QToolButton()
        help_button.setText("?")
        help_button.setToolTip("这页怎么用？")
        help_button.clicked.connect(self._help)
        row.addWidget(help_button)
        demo_button = QPushButton("先看一次演示")
        demo_button.clicked.connect(self._demo)
        row.addWidget(demo_button)
        root.addLayout(row)
        grid = QGridLayout()
        for c, name in enumerate(["参数组", "模块", "ESO 带宽 rad/s", "标称电感 mH", "RLS 遗忘因子 λ"]):
            grid.addWidget(QLabel(name), 0, c)
        for i in range(2):
            module = QComboBox()
            module.addItems(["ESO 电流观测器", "ESO＋RLS"])
            module.setCurrentIndex(1)
            bandwidth = double_spin(2000 if i == 0 else 4000, 1, 200000, 1)
            inductance = double_spin(.66, .001, 10000, 4)
            forgetting = double_spin(1, .9, 1, 6)
            forgetting.setSingleStep(.0001)
            self.profiles.append((module, bandwidth, inductance, forgetting))
            for c, widget in enumerate([QLabel("A" if i == 0 else "B"), module, bandwidth, inductance, forgetting]):
                grid.addWidget(widget, i+1, c)
            module.currentIndexChanged.connect(lambda index, box=forgetting: box.setEnabled(index == 1))
        root.addLayout(grid)
        row = QHBoxLayout()
        self.train = double_spin(70, 10, 90, 0)
        self.warmup = double_spin(.02, 0, 10, 4)
        row.addWidget(QLabel("前段训练 %"))
        row.addWidget(self.train)
        row.addWidget(QLabel("预热 s"))
        row.addWidget(self.warmup)
        self.axis = QComboBox()
        self.axis.addItems(["q 轴", "d 轴"])
        self.axis.currentIndexChanged.connect(self._render)
        row.addWidget(self.axis)
        self.run_button = QPushButton("开始 A/B 验证")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self._run)
        row.addWidget(self.run_button)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self._cancel)
        self.cancel.setEnabled(False)
        row.addWidget(self.cancel)
        self.save = QPushButton("导出比较…")
        self.save.setEnabled(False)
        self.save.clicked.connect(self._save)
        row.addWidget(self.save)
        root.addLayout(row)
        self.status = QLabel("① 选择记录或演示 → ② 调整 A/B 参数 → ③ 开始验证；“?” 查看用法")
        self.status.setWordWrap(True)
        self.status.setToolTip("ESO 校正后误差小不代表抗噪更好；请同时检查先验预测误差。"
                               "RLS 预测目标是 ESO 电流，ARX 系数不直接等同于物理 R/L。"
                               "离线重放不能证明更换控制器后的闭环性能。")
        root.addWidget(self.status)
        self.table = QTableWidget(4, 3)
        self.table.setHorizontalHeaderLabels(["后段评价（全采样率）", "A", "B"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setMaximumHeight(160)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table)
        display = QHBoxLayout()
        self.trace_toggles = {}
        for key, title in (("measured", "实测 · 灰色"), ("A", "A · 青色实线"), ("B", "B · 橙色虚线")):
            check = QCheckBox(title)
            check.setChecked(True)
            check.toggled.connect(self._render)
            self.trace_toggles[key] = check
            display.addWidget(check)
        self.current_view = QComboBox()
        self.current_view.addItems(["叠加对照", "相对实测的差值"])
        self.current_view.currentIndexChanged.connect(self._render)
        display.addWidget(self.current_view)
        display.addStretch()
        zoom = QToolButton()
        zoom.setText("↗")
        zoom.setToolTip("弹出大图，可缩放并分别查看三个图表")
        zoom.clicked.connect(self._popout)
        display.addWidget(zoom)
        root.addLayout(display)
        self.graph = pg.GraphicsLayoutWidget()
        self.graph.setBackground("#10151c")
        self.graph.setMinimumHeight(400)
        root.addWidget(self.graph, 1)
        self.current = self.graph.addPlot(row=0, col=0, title="实测电流与 ESO 校正值")
        self.error = self.graph.addPlot(row=1, col=0, title="ESO 先验预测误差（校正前）")
        self.coefficients = self.graph.addPlot(row=2, col=0, title="RLS 本轴电压系数 b₀（非物理电感）")
        for plot in (self.current, self.error, self.coefficients):
            plot.addLegend(offset=(8, 4))
            plot.setLabel("bottom", "重放时间", units="s")
            plot.showGrid(x=True, y=True, alpha=.15)
        self.current.setLabel("left", "电流", units="A")
        self.error.setLabel("left", "预测误差", units="A")
        self.error.setXLink(self.current)
        self.coefficients.setXLink(self.current)

    def _help(self):
        QMessageBox.information(self, "算法验证怎么用",
            "这页让两套算法参数读取同一段已记录的数据，比较它们的估计结果。\n\n"
            "1. 没有数据：点击“先看一次演示”。有数据：选择当前 F1 缓冲，或导入同步 CSV。\n"
            "2. A/B 分别设置带宽、电感和遗忘因子。例如只改 B 的带宽，其他参数保持相同。\n"
            "3. 点击“开始 A/B 验证”，先看表格里的 ESO 先验 RMSE，再看误差曲线。\n\n"
            "灰线是实测，青色实线是 A，橙色虚线是 B。曲线重合通常表示结果接近；"
            "可取消勾选某条线，或选择“相对实测的差值”。右上角 ↗ 可放大查看。\n\n"
            "后段 RLS 冻结系数用于评价；误差小不必然意味着抗噪更好。"
            "这里验证已接入的 ESO/RLS，不会向电机下发控制命令。")

    def _demo(self):
        if self.busy():
            return
        self.source.setCurrentIndex(2)
        self._run()

    def _popout(self):
        from widgets.analysis_plot_dialog import show_plot_dialog
        show_plot_dialog([("电流对照 / 差值", self.current), ("先验预测误差", self.error),
                          ("RLS 系数", self.coefficients)], self)

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def closeEvent(self, event):
        if self.busy():
            self._cancel()
            event.ignore()
        else:
            super().closeEvent(event)

    def _choose_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "同步 F1 或 SI dq 数据", "", "CSV (*.csv)")
        if path:
            self.source_path = path
            self.source.setCurrentIndex(1)
            self.source_label.setText(path)

    def _cancel(self):
        if self.worker:
            self.worker.requestInterruption()
            self.status.setText("正在取消；等待当前 C++ 批次结束")

    def _run(self):
        if self.busy():
            return
        from algorithm_replay import compare_capture, read_capture, demo_capture
        try:
            source = self.source.currentIndex()
            snapshot = None
            path = self.source_path
            if source == 0:
                if self.snapshot_provider is None:
                    raise ValueError("同步 F1 缓冲尚未接入")
                snapshot = self.snapshot_provider()
                if snapshot.sample_count < 32:
                    raise ValueError("暂无同步 F1 数据；可加载同步 CSV 或查看演示")
            elif source == 1 and not path:
                raise ValueError("请先选择同步 CSV")
            profiles = [dict(rls=m.currentIndex() == 1, bandwidth=b.value(),
                             inductance=l.value()/1000, **{"lambda": f.value()})
                        for m, b, l, f in self.profiles]
            train, warmup = self.train.value()/100, self.warmup.value()
            source_name = path if source == 1 else self.source.currentText()
        except Exception as exc:
            self.status.setText(str(exc))
            return
        def operation(cancel):
            columns = snapshot.to_columns() if source == 0 else read_capture(path) if source == 1 else demo_capture()
            result = compare_capture(columns, profiles, train, warmup, cancel)
            result["source"] = source_name
            return result
        self.result = None
        self.run_button.setEnabled(False)
        self.save.setEnabled(False)
        self.cancel.setEnabled(True)
        self.status.setText("后台读取同步数据并运行 C++ A/B 验证…")
        self.worker = AnalysisWorker(operation, self)
        self.worker.succeeded.connect(self._completed)
        self.worker.failed.connect(self.status.setText)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        self.run_button.setEnabled(True)
        self.cancel.setEnabled(False)
        if self.worker and self.worker.isInterruptionRequested():
            self.status.setText("已取消")
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()

    def _completed(self, result):
        self.result = result
        self.save.setEnabled(True)
        self.status.setText(f"{result['source']} ｜ {result['rate']} Hz ｜ {result['continuity']} ｜ "
                            f"电压：{result['voltage_source']}；RLS 后段冻结，系数非物理 R/L")
        self._render()

    def _render(self):
        if self.result is None:
            return
        result = self.result
        axis = "q" if self.axis.currentIndex() == 0 else "d"
        for plot in (self.current, self.error, self.coefficients):
            plot.clear()
        measured = result["i"+axis]
        time = result["results"][0]["time"]
        difference = self.current_view.currentIndex() == 1
        self.current.setTitle("ESO 校正值 − 实测值" if difference else "实测电流与 ESO 校正值")
        if self.trace_toggles["measured"].isChecked():
            self.current.plot(time, np.zeros_like(measured) if difference else measured,
                              pen=pg.mkPen("#b8b8b8", width=3), name="实测基线" if difference else "实测 · 灰色")
        rows = [("ESO 先验 RMSE / A", "prior_rmse_"+axis),
                ("ESO 校正后 RMSE / A", "corrected_rmse_"+axis),
                ("冻结 RLS → ESO 目标 RMSE / A", "rls_holdout_rmse_"+axis)]
        for row, (label, key) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(label))
            for i, replay in enumerate(result["results"]):
                value = replay["metrics"][key]
                self.table.setItem(row, i+1, QTableWidgetItem("未启用" if value is None else f"{value:.6g}"))
        self.table.setItem(3, 0, QTableWidgetItem("前段（含预热）/ 评价样本"))
        for i, (replay, color) in enumerate(zip(result["results"], ("#69cfbd", "#ffc477"))):
            name = "A" if i == 0 else "B"
            self.table.setItem(3, i+1, QTableWidgetItem(
                f"{replay['sample_count']-replay['evaluated_count']:,} / {replay['evaluated_count']:,}"))
            if not self.trace_toggles[name].isChecked():
                continue
            pen = pg.mkPen(color, width=1.8, style=Qt.SolidLine if i == 0 else Qt.DashLine)
            self.current.plot(time, replay["i"+axis+"_hat"]-(measured if difference else 0), pen=pen, name=name)
            self.error.plot(time, measured-replay["i"+axis+"_prior"], pen=pen, name=name)
            if result["profiles"][i]["rls"]:
                self.coefficients.plot(time, replay["theta_"+axis][:, 5 if axis == "q" else 3], pen=pen, name=name)
        for plot in (self.current, self.error, self.coefficients):
            plot.addItem(pg.InfiniteLine(result["results"][0]["split_s"], pen="#a69470"))

    def _save(self):
        if self.result is None:
            return
        path, selected = QFileDialog.getSaveFileName(self, "保存 A/B 参数、指标和轨迹", "ESO_RLS比较.npz",
                                                    "NumPy 数据 (*.npz);;图像 (*.png)")
        if not path:
            return
        try:
            if "*.png" in selected or Path(path).suffix.lower() == ".png":
                if not self.grab().save(path, "PNG"):
                    raise OSError("图像写入失败")
                self.status.setText("已保存图像："+path)
                return
            arrays = {"id": self.result["id"], "iq": self.result["iq"]}
            metadata = {k: v for k, v in self.result.items() if k not in ("results", "id", "iq")}
            metadata["results"] = []
            for i, replay in enumerate(self.result["results"]):
                arrays.update({f"{'A' if i == 0 else 'B'}_{k}": v for k, v in replay.items()
                               if isinstance(v, np.ndarray)})
                metadata["results"].append({k: v for k, v in replay.items() if not isinstance(v, np.ndarray)})
            with open(path, "wb") as stream:
                np.savez_compressed(stream, **arrays, metadata=json.dumps(metadata, ensure_ascii=False))
            self.status.setText("已保存："+path)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
