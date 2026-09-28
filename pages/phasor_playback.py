"""分量合成（离线回放）：把电流矢量拆成首尾相接的旋转矢量，在 αβ 平面慢放。

数据来自保存的记录（高速数据.csv / 旧名 RLS辨识数据.csv 的 16 kHz 宽表，或监控页长表），可指定时间窗；
没有记录时用演示数据。每个分量一支箭头：基波、零偏、负序、谐波、与转速无关的
干扰（如 ±654 Hz）。±f 两个分量可合并成一支“摆动”箭头：幅值相等时沿直线来回摆。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSizePolicy,
    QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.phasor_decomposition import (
    Analysis, Capture, analyze, find_pairs, load_capture, synthetic_capture,
)

_PALETTE = ("#ffb74d", "#4fc3f7", "#81c784", "#f48fb1", "#ce93d8", "#fff176",
            "#80cbc4", "#ff8a65", "#9fa8da", "#bcaaa4")
_SPEEDS = (("1/10000", 1e-4), ("1/3000", 1 / 3000), ("1/1000", 1e-3), ("1/300", 1 / 300),
           ("1/100", 1e-2), ("1/30", 1 / 30), ("1/10", 0.1), ("1×", 1.0))
_FRAME_MS = 33
_SLIDER_STEPS = 2000


class PhasorPlayback(QWidget):
    def __init__(self, demo_params=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._demo_params = demo_params          # 可调用：返回畸变图谱当前参数
        self._capture: Capture | None = None
        self._analysis: Analysis | None = None
        self._items: list[dict] = []             # 显示单元：单个分量或 ±f 合并对
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self._advance)

        root = QVBoxLayout(self)
        root.addLayout(self._build_source_bar())
        body = QHBoxLayout()
        body.addWidget(self._build_component_panel(), 0)
        body.addWidget(self._build_plot(), 1)
        root.addLayout(body, 1)
        root.addLayout(self._build_playback_bar())

    # ------------------------------------------------------------ 布局
    def _build_source_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._btn_open = QPushButton("打开记录…")
        self._btn_open.setToolTip("高速数据.csv（旧记录为 RLS辨识数据.csv）：16 kHz 全程记录，含 angle_deg、ia_a、ib_a；"
                                  "或监控页“保存所有波形”的长表")
        self._btn_open.clicked.connect(self._open_dialog)
        self._btn_demo = QPushButton("演示数据")
        self._btn_demo.setToolTip("用“畸变图谱”当前参数 + 654 Hz 共模干扰合成 2 s 数据")
        self._btn_demo.clicked.connect(self.load_demo)
        self._source = QLabel("未加载")
        self._source.setStyleSheet("color:#90a4ae;")
        self._t0 = QDoubleSpinBox()
        self._t1 = QDoubleSpinBox()
        for spin in (self._t0, self._t1):
            spin.setDecimals(3)
            spin.setSuffix(" s")
            spin.setSingleStep(1.0)
        self._block = QDoubleSpinBox()
        self._block.setRange(0.02, 5.0)
        self._block.setValue(0.25)
        self._block.setDecimals(2)
        self._block.setSuffix(" s")
        self._block.setToolTip("分段拟合长度：越短越能跟上幅值变化，但低频分量更不稳")
        self._btn_analyze = QPushButton("分解")
        self._btn_analyze.clicked.connect(self.run_analysis)
        for widget in (self._btn_open, self._btn_demo):
            row.addWidget(widget)
        row.addWidget(self._source, 1)
        for text, widget in (("时间窗", self._t0), ("至", self._t1), ("分段", self._block)):
            row.addWidget(QLabel(text))
            row.addWidget(widget)
        row.addWidget(self._btn_analyze)
        return row

    def _build_component_panel(self) -> QGroupBox:
        box = QGroupBox("分量（勾选参与合成）")
        box.setFixedWidth(460)
        layout = QVBoxLayout(box)
        self._summary = QLabel("先打开记录或点“演示数据”。")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["分量", "静止系 Hz", "dq 中 Hz", "幅值 A", "占基波"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._table.itemChanged.connect(self._on_item_toggled)
        layout.addWidget(self._table, 1)
        self._chk_pairs = QCheckBox("±f 成对合并为“摆动”（等幅时沿直线来回）")
        self._chk_pairs.setChecked(True)
        self._chk_pairs.toggled.connect(self._rebuild_items)
        layout.addWidget(self._chk_pairs)
        self._pairs_label = QLabel()
        self._pairs_label.setWordWrap(True)
        self._pairs_label.setStyleSheet("color:#90a4ae; font-size:12px;")
        layout.addWidget(self._pairs_label)
        hint = QLabel("静止系 k 次 ↔ dq 中 (f − fe)：零偏在 dq 里是 1 倍电频率，负序是 2 倍，"
                      "−5/+7 次是 6 倍；与转速无关的干扰在 dq 里频率随转速移动。"
                      "接近采样率一半的分量（如 16 kHz 下的 ±6 kHz）每个采样点转一百多度，"
                      "余辉呈锯齿是采样本身的样子，取消勾选即可看清其余分量。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#90a4ae; font-size:11px;")
        layout.addWidget(hint)
        return box

    def _build_plot(self) -> pg.PlotWidget:
        self._plot = pg.PlotWidget(title="αβ 平面矢量合成")
        self._plot.setAspectLocked(True)
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.setLabel("bottom", "iα", units="A")
        self._plot.setLabel("left", "iβ", units="A")
        self._plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._plot.setMinimumHeight(320)
        self._measured_trail = self._plot.plot([], [], pen=pg.mkPen((255, 255, 255, 60), width=1))
        self._tip_trail = self._plot.plot([], [], pen=pg.mkPen("#e0f7fa", width=2))
        self._measured_dot = pg.ScatterPlotItem(size=9, pen=pg.mkPen("#ffffff"), brush=None)
        self._tip_dot = pg.ScatterPlotItem(size=8, pen=None, brush=pg.mkBrush("#e0f7fa"))
        self._plot.addItem(self._measured_dot)
        self._plot.addItem(self._tip_dot)
        self._guides: list[pg.PlotDataItem] = []
        self._arrows: list[pg.PlotDataItem] = []
        return self._plot

    def _build_playback_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._btn_play = QPushButton("▶ 播放")
        self._btn_play.setCheckable(True)
        self._btn_play.toggled.connect(self._on_play)
        self._speed = QComboBox()
        for text, value in _SPEEDS:
            self._speed.addItem(f"慢放 {text}", value)
        self._speed.setCurrentIndex(4)
        self._speed.setToolTip("回放时间 / 真实时间；1/100 时 53 Hz 基波约每秒转半圈")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, _SLIDER_STEPS)
        self._slider.valueChanged.connect(self._on_slider)
        self._time_label = QLabel("t = --")
        self._time_label.setMinimumWidth(230)
        self._chk_measured = QCheckBox("实测点")
        self._chk_measured.setChecked(True)
        self._chk_orbits = QCheckBox("分量轨道")
        self._chk_orbits.setChecked(True)
        self._trail = QComboBox()
        for periods in (0.5, 1, 2, 5):
            self._trail.addItem(f"余辉 {periods:g} 个电周期", periods)
        self._trail.setCurrentIndex(1)
        self._view = QComboBox()
        self._view.addItem("视图：全局", "global")
        self._view.addItem("视图：跟随基波末端（放大小分量）", "follow")
        self._view.setToolTip("小分量只有基波的百分之几，跟随模式以基波箭头末端为中心放大其余分量")
        self._view.currentIndexChanged.connect(self._autoscale)
        for widget in (self._chk_measured, self._chk_orbits, self._trail):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self._draw)
            else:
                widget.currentIndexChanged.connect(self._draw)
        row.addWidget(self._btn_play)
        row.addWidget(self._speed)
        row.addWidget(self._slider, 1)
        row.addWidget(self._time_label)
        row.addWidget(self._chk_measured)
        row.addWidget(self._chk_orbits)
        row.addWidget(self._trail)
        row.addWidget(self._view)
        return row

    # ------------------------------------------------------------ 数据
    def _open_dialog(self) -> None:
        start = Path(__file__).resolve().parents[1] / "波形记录"
        path, _ = QFileDialog.getOpenFileName(
            self, "打开波形记录", str(start if start.exists() else ""),
            "高速数据 (高速数据.csv RLS辨识数据.csv);;CSV (*.csv);;所有文件 (*)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> bool:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            capture = load_capture(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "记录无法读取", str(exc))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self._set_capture(capture)
        return True

    def load_demo(self) -> None:
        params = self._demo_params() if callable(self._demo_params) else None
        self._set_capture(synthetic_capture(params))

    def _set_capture(self, capture: Capture) -> None:
        self._capture = capture
        start, end = float(capture.time[0]), float(capture.time[-1])
        for spin in (self._t0, self._t1):
            spin.blockSignals(True)
            spin.setRange(start, end)
            spin.blockSignals(False)
        # 默认取最后 5 s（常见的稳态段），短记录取全段
        self._t0.setValue(max(start, end - 5.0))
        self._t1.setValue(end)
        self._source.setText(f"{capture.source} · {capture.duration:.2f} s · "
                             f"{capture.rate_hz:.0f} Hz"
                             + ("" if capture.theta is not None else " · 无电角度，按频谱估计电频率"))
        self.run_analysis()

    def set_window(self, t0: float, t1: float) -> None:
        self._t0.setValue(t0)
        self._t1.setValue(t1)

    def run_analysis(self) -> None:
        if self._capture is None:
            return
        t0, t1 = self._t0.value(), self._t1.value()
        if t1 <= t0:
            QMessageBox.warning(self, "时间窗无效", "结束时间必须大于开始时间")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._analysis = analyze(self._capture, t0, t1, block_s=self._block.value())
        except ValueError as exc:
            QMessageBox.warning(self, "无法分解", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._fill_table()
        self._rebuild_items()
        self._t = float(self._analysis.time[0])
        self._sync_slider()
        self._autoscale()
        self._draw()

    @property
    def analysis(self) -> Analysis | None:
        return self._analysis

    # ------------------------------------------------------------ 分量表
    def _fill_table(self) -> None:
        a = self._analysis
        fundamental = a.components[0].amp if a.components else 1.0
        speed = abs(a.fe_hz) * 60.0 / 4.0
        self._summary.setText(
            f"电频率 fe = {a.fe_hz:+.2f} Hz（4 对极约 {speed:.0f} rpm）；"
            f"{len(a.components)} 个分量，未解释残差 {a.residual_pct:.1f}% 基波。")
        self._table.blockSignals(True)
        self._table.setRowCount(len(a.components))
        for row, comp in enumerate(a.components):
            name = QTableWidgetItem(comp.label)
            name.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            name.setCheckState(Qt.Checked)
            name.setData(Qt.UserRole, comp.key)
            name.setForeground(pg.mkColor(_PALETTE[row % len(_PALETTE)]))
            self._table.setItem(row, 0, name)
            for column, text in enumerate((f"{comp.freq_hz:+.1f}", f"{comp.dq_freq_hz:+.1f}",
                                            f"{comp.amp:.4f}",
                                            f"{100.0 * comp.amp / max(fundamental, 1e-12):.1f}%"), 1):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(row, column, item)
        self._table.blockSignals(False)

    def _enabled_keys(self) -> set[str]:
        keys = set()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                keys.add(item.data(Qt.UserRole))
        return keys

    def set_component_enabled(self, key: str, enabled: bool) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == key:
                item.setCheckState(Qt.Checked if enabled else Qt.Unchecked)

    def _on_item_toggled(self, _item) -> None:
        self._rebuild_items()

    def _rebuild_items(self, *_args) -> None:
        """显示单元：勾选的分量按表格顺序排成首尾相接的链；±f 对合并成一支。"""
        a = self._analysis
        if a is None:
            return
        enabled = self._enabled_keys()
        colors = {c.key: _PALETTE[i % len(_PALETTE)] for i, c in enumerate(a.components)}
        by_key = {c.key: c for c in a.components}
        pairs = find_pairs(a) if self._chk_pairs.isChecked() else []
        paired = {}
        for pair in pairs:
            if pair.plus in enabled and pair.minus in enabled:
                paired[pair.plus] = pair
                paired[pair.minus] = pair
        items, done = [], set()
        for comp in a.components:
            if comp.key not in enabled or comp.key in done:
                continue
            pair = paired.get(comp.key)
            if pair is not None:
                members = [by_key[pair.plus], by_key[pair.minus]]
                done.update((pair.plus, pair.minus))
                shape = "直线" if pair.linearity >= 0.9 else "椭圆"
                items.append({"members": members, "color": colors[comp.key],
                              "label": f"±{pair.freq_hz:.1f} Hz {shape}摆动",
                              "period": 1.0 / abs(pair.freq_hz)})
            else:
                done.add(comp.key)
                period = 1.0 / abs(comp.freq_hz) if abs(comp.freq_hz) > 1e-6 else None
                items.append({"members": [comp], "color": colors[comp.key],
                              "label": comp.label, "period": period})
        self._items = items
        self._pairs_label.setText(
            "；".join(f"±{p.freq_hz:.1f} Hz：直线度 {100 * p.linearity:.0f}%，摆动方向 {p.axis_deg:.0f}°"
                     for p in pairs) or "没有频率互为相反数的分量对。")
        for item in self._guides + self._arrows:
            self._plot.removeItem(item)
        self._guides, self._arrows = [], []
        for item in self._items:
            guide = self._plot.plot([], [], pen=pg.mkPen(item["color"], width=1, style=Qt.DotLine))
            arrow = self._plot.plot([], [], pen=pg.mkPen(item["color"], width=2.4), connect="pairs")
            self._guides.append(guide)
            self._arrows.append(arrow)
        self._autoscale()
        self._draw()

    # ------------------------------------------------------------ 播放
    def _on_play(self, playing: bool) -> None:
        self._btn_play.setText("⏸ 暂停" if playing else "▶ 播放")
        if playing and self._analysis is not None:
            self._timer.start()
        else:
            self._timer.stop()

    def _advance(self) -> None:
        a = self._analysis
        if a is None:
            return
        step = self._speed.currentData() * _FRAME_MS / 1000.0
        t0, t1 = float(a.time[0]), float(a.time[-1])
        self._t = t0 + (self._t + step - t0) % max(t1 - t0, 1e-9)
        self._sync_slider()
        self._draw()

    def _sync_slider(self) -> None:
        a = self._analysis
        if a is None:
            return
        t0, t1 = float(a.time[0]), float(a.time[-1])
        self._slider.blockSignals(True)
        self._slider.setValue(int(round((self._t - t0) / max(t1 - t0, 1e-9) * _SLIDER_STEPS)))
        self._slider.blockSignals(False)

    def _on_slider(self, value: int) -> None:
        a = self._analysis
        if a is None:
            return
        t0, t1 = float(a.time[0]), float(a.time[-1])
        self._t = t0 + (t1 - t0) * value / _SLIDER_STEPS
        self._draw()

    def seek(self, t: float) -> None:
        self._t = float(t)
        self._sync_slider()
        self._draw()

    def _item_amp(self, item: dict) -> float:
        return sum(comp.amp for comp in item["members"])

    def _following(self) -> bool:
        return (self._view.currentData() == "follow" and len(self._items) > 1
                and self._items[0]["members"][0].order == 1)

    def _autoscale(self, *_args) -> None:
        """全局视图按勾选分量的总长度定范围；跟随视图在每帧绘制时移动。"""
        if self._analysis is None or self._following():
            self._draw()
            return
        reach = max(sum(self._item_amp(item) for item in self._items), 1e-6)
        self._plot.setRange(xRange=(-reach * 1.15, reach * 1.15),
                            yRange=(-reach * 1.15, reach * 1.15), padding=0)
        self._draw()

    # ------------------------------------------------------------ 绘制
    def _draw(self, *_args) -> None:
        a = self._analysis
        if a is None:
            return
        t = self._t
        fe = abs(a.fe_hz) if abs(a.fe_hz) >= 1.0 else 50.0
        trail_s = self._trail.currentData() / fe
        lo, hi = np.searchsorted(a.time, [t - trail_s, t])
        times = a.time[lo:hi]
        if times.size > 4000:
            times = times[:: int(np.ceil(times.size / 4000))]
        times = np.append(times, t)
        tip_history = np.zeros(times.size, complex)
        start = 0j
        following = self._following()
        if following:
            reach = max(sum(self._item_amp(item) for item in self._items[1:]), 1e-6)
        else:
            reach = max(sum(self._item_amp(item) for item in self._items), 1e-9)
        head = 0.05 * reach
        focus = 0j
        for item, guide, arrow in zip(self._items, self._guides, self._arrows):
            vector = complex(sum(a.series(comp, np.array([t]))[0] for comp in item["members"]))
            for comp in item["members"]:
                tip_history += a.series(comp, times)
            end = start + vector
            arrow.setData(*self._arrow_xy(start, end, head))
            if self._chk_orbits.isChecked() and item["period"] is not None:
                span = np.linspace(t, t + item["period"], 90)
                path = start + sum(a.series(comp, span) for comp in item["members"])
                guide.setData(path.real, path.imag)
            else:
                guide.setData([], [])
            if item is self._items[0]:
                focus = end
            start = end
        if following:
            span = reach * 1.3
            self._plot.setRange(xRange=(focus.real - span, focus.real + span),
                                yRange=(focus.imag - span, focus.imag + span), padding=0)
        self._tip_trail.setData(tip_history.real, tip_history.imag)
        self._tip_dot.setData([start.real], [start.imag])
        if self._chk_measured.isChecked():
            measured = a.measured_between(times[0], t)
            self._measured_trail.setData(measured.real, measured.imag)
            point = a.measured_at(t)
            self._measured_dot.setData([point.real], [point.imag])
        else:
            self._measured_trail.setData([], [])
            self._measured_dot.setData([], [])
        theta_deg = math.degrees(a.theta_at(t)) % 360.0
        self._time_label.setText(f"t = {t:.4f} s · θe = {theta_deg:5.1f}° · |i| = {abs(start):.3f} A")

    @staticmethod
    def _arrow_xy(start: complex, end: complex, head: float):
        """箭杆 + 两片箭头（connect='pairs' 的线段对）。"""
        vector = end - start
        if abs(vector) < 1e-12:
            return [start.real, end.real], [start.imag, end.imag]
        size = min(head, 0.35 * abs(vector))
        back = -vector / abs(vector) * size
        wing1 = end + back * complex(math.cos(0.45), math.sin(0.45))
        wing2 = end + back * complex(math.cos(-0.45), math.sin(-0.45))
        points = [start, end, end, wing1, end, wing2]
        return [p.real for p in points], [p.imag for p in points]

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if self._capture is None:
            self.load_demo()
        if self._btn_play.isChecked():
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)
