"""矢量畸变图谱：拖动滑块看零偏、三相不对称、谐波、死区如何把电流圆变形，并与实测轨迹对照。

左：畸变来源（滑块 + 典型预设）；中：αβ 电流圆、dq 波形、复数频谱；
右：实测轨迹的圆度分解、按几何反推的等效参数和可能原因。
模型与反推见 core/vector_distortion.py；全部量以基波幅值归一化。
"""
from __future__ import annotations

from dataclasses import fields, replace
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from core.vector_distortion import (
    PRESETS, DistortionParams, diagnose, estimate_params, simulate,
)
from core.vector_shape import trajectory_shape

# (字段, 名称, 最小, 最大, 单位, 说明)
_SLIDERS = (
    ("offset_a_pct", "Ia 零偏", -15.0, 15.0, "%", "k=0：圆心沿 α 轴偏移；dq 中出现 1 倍电频率纹波"),
    ("offset_b_pct", "Ib 零偏", -15.0, 15.0, "%", "k=0：圆心沿 B 相方向偏移"),
    ("gain_b_pct", "Ib 增益误差", -20.0, 20.0, "%", "k=−1 负序：椭圆（长轴约 104°）；dq 中 2 倍电频率纹波"),
    ("phase_b_deg", "Ib 相位误差", -10.0, 10.0, "°", "k=−1 负序：椭圆（长轴约 60°），如两路采样时刻不一致"),
    ("h5_pct", "5 次谐波", 0.0, 20.0, "%", "k=−5 负序：六瓣；dq 中 6 倍电频率纹波"),
    ("h7_pct", "7 次谐波", 0.0, 20.0, "%", "k=+7 正序：六瓣；dq 中 6 倍电频率纹波"),
    ("dead_time_pct", "死区", 0.0, 20.0, "%", "6k±1 次（−5、+7、−11、+13…），按 5 次分量幅值计"),
)
_STEP = 10          # 滑块整数刻度 = 值 × 10（0.1 分辨率）
_SIM = "#4fc3f7"
_MEASURED = "#ffb74d"
_IDEAL = "#546e7a"


class VectorDistortionLab(QWidget):
    def __init__(self, measured: Callable[[], tuple] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._measured = measured
        self._params = DistortionParams()
        self._sliders: dict[str, QSlider] = {}
        self._values: dict[str, QLabel] = {}
        self._measured_shape = None
        self._measured_xy = None

        root = QHBoxLayout(self)
        root.addWidget(self._build_controls(), 0)
        root.addLayout(self._build_plots(), 1)
        root.addWidget(self._build_measured(), 0)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_measured)
        self._update_simulation()

    # ------------------------------------------------------------ 布局
    def _build_controls(self) -> QGroupBox:
        box = QGroupBox("畸变来源（相对基波）")
        box.setFixedWidth(300)
        layout = QVBoxLayout(box)
        self._preset = QComboBox()
        self._preset.addItems(list(PRESETS))
        self._preset.setToolTip("典型情形；选中后可继续拖动滑块微调")
        self._preset.activated.connect(self._apply_preset)
        layout.addWidget(self._preset)
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        for row, (name, label, lo, hi, unit, tip) in enumerate(_SLIDERS):
            title = QLabel(label)
            title.setToolTip(tip)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(int(lo * _STEP), int(hi * _STEP))
            slider.setToolTip(tip)
            slider.valueChanged.connect(self._on_slider)
            value = QLabel()
            value.setMinimumWidth(52)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setProperty("unit", unit)
            grid.addWidget(title, 2 * row, 0)
            grid.addWidget(value, 2 * row, 1)
            grid.addWidget(slider, 2 * row + 1, 0, 1, 2)
            self._sliders[name] = slider
            self._values[name] = value
        layout.addLayout(grid)
        buttons = QHBoxLayout()
        reset = QPushButton("复位")
        reset.clicked.connect(lambda: self.set_params(DistortionParams()))
        self._btn_fit = QPushButton("按实测反推")
        self._btn_fit.setToolTip("用右侧实测轨迹的圆度分解，反推等效零偏、增益/相位误差和死区并填入滑块")
        self._btn_fit.clicked.connect(self.apply_measured_estimate)
        buttons.addWidget(reset)
        buttons.addWidget(self._btn_fit)
        layout.addLayout(buttons)
        legend = QLabel(
            "频谱颜色：<span style='color:#ffb74d'>正序</span>、"
            "<span style='color:#4fc3f7'>负序</span>、"
            "<span style='color:#b0bec5'>零次（静止矢量）</span>。<br>"
            "静止坐标 k 次 ↔ dq 中 (k−1) 倍电频率纹波。")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#90a4ae; font-size:12px;")
        layout.addWidget(legend)
        layout.addStretch(1)
        return box

    def _build_plots(self) -> QVBoxLayout:
        column = QVBoxLayout()
        self._circle = pg.PlotWidget(title="αβ 电流圆（归一化）")
        self._circle.setAspectLocked(True)
        self._circle.showGrid(x=True, y=True, alpha=0.25)
        self._circle.setLabel("bottom", "iα / I₁")
        self._circle.setLabel("left", "iβ / I₁")
        theta = np.linspace(0.0, 2.0 * np.pi, 361)
        self._circle.plot(np.cos(theta), np.sin(theta),
                          pen=pg.mkPen(_IDEAL, width=1.0, style=Qt.DashLine))
        self._measured_curve = self._circle.plot(
            [], [], pen=None, symbol="o", symbolSize=2,
            symbolPen=None, symbolBrush=pg.mkBrush(255, 183, 77, 90))
        self._sim_curve = self._circle.plot([], [], pen=pg.mkPen(_SIM, width=2.2))
        self._circle.setRange(xRange=(-1.4, 1.4), yRange=(-1.4, 1.4))
        column.addWidget(self._circle, 3)

        row = QHBoxLayout()
        self._dq = pg.PlotWidget(title="一个电周期内的 dq 电流")
        self._dq.showGrid(x=True, y=True, alpha=0.25)
        self._dq.setLabel("bottom", "电角度 / °")
        self._dq.addLegend(offset=(6, 6))
        self._id_curve = self._dq.plot([], [], pen=pg.mkPen("#81c784", width=1.6), name="id / I₁")
        self._iq_curve = self._dq.plot([], [], pen=pg.mkPen(_SIM, width=1.6), name="iq / I₁")
        self._spectrum = pg.PlotWidget(title="复数频谱 |c_k|（去掉 k=+1 基波）")
        self._spectrum.showGrid(x=False, y=True, alpha=0.25)
        self._spectrum.setLabel("bottom", "谐波次数 k")
        self._spectrum.setLabel("left", "% 基波")
        self._bars = None
        row.addWidget(self._dq, 1)
        row.addWidget(self._spectrum, 1)
        column.addLayout(row, 2)
        # 图的高度跟随可用空间，不用 pyqtgraph 默认的大尺寸把页面撑出窗口
        for plot, minimum in ((self._circle, 260), (self._dq, 170), (self._spectrum, 170)):
            plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
            plot.setMinimumHeight(minimum)
        return column

    def _build_measured(self) -> QGroupBox:
        box = QGroupBox("实测对照")
        box.setFixedWidth(330)
        layout = QVBoxLayout(box)
        self._source = QLabel()
        self._source.setWordWrap(True)
        self._source.setStyleSheet("color:#90a4ae; font-size:12px;")
        layout.addWidget(self._source)
        grid = QGridLayout()
        grid.addWidget(QLabel(""), 0, 0)
        for column, text, color in ((1, "仿真", _SIM), (2, "实测", _MEASURED)):
            header = QLabel(text)
            header.setStyleSheet(f"color:{color}; font-weight:bold;")
            header.setAlignment(Qt.AlignRight)
            grid.addWidget(header, 0, column)
        self._table: dict[str, tuple[QLabel, QLabel]] = {}
        for row, (key, text) in enumerate((("eccentric", "偏心（零偏）"), ("ellipse", "椭圆（负序）"),
                                           ("hexagon", "六边形（5/7 次）"), ("triangle", "三角（−2 次）")), 1):
            grid.addWidget(QLabel(text), row, 0)
            cells = (QLabel("--"), QLabel("--"))
            for column, cell in enumerate(cells, 1):
                cell.setAlignment(Qt.AlignRight)
                grid.addWidget(cell, row, column)
            self._table[key] = cells
        layout.addLayout(grid)
        self._chk_overlay = QCheckBox("在电流圆上叠加实测轨迹")
        self._chk_overlay.setChecked(True)
        self._chk_overlay.toggled.connect(self._draw_measured)
        layout.addWidget(self._chk_overlay)
        refresh = QPushButton("刷新实测")
        refresh.clicked.connect(self.refresh_measured)
        layout.addWidget(refresh)
        title = QLabel("可能原因")
        title.setStyleSheet("color:#4fc3f7; font-weight:bold; margin-top:6px;")
        layout.addWidget(title)
        self._diagnosis = QLabel()
        self._diagnosis.setWordWrap(True)
        self._diagnosis.setTextFormat(Qt.RichText)
        self._diagnosis.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._diagnosis, 1)
        note = QLabel("实测只有轨迹几何，反推值是“等效”量：同样的形状可能由多种原因叠加，"
                      "需结合零偏校准、低速/高速对比等试验确认。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#90a4ae; font-size:11px;")
        layout.addWidget(note)
        return box

    # ------------------------------------------------------------ 参数
    @property
    def params(self) -> DistortionParams:
        return self._params

    def set_params(self, params: DistortionParams) -> None:
        self._params = params
        for name, slider in self._sliders.items():
            slider.blockSignals(True)
            slider.setValue(int(round(getattr(params, name) * _STEP)))
            slider.blockSignals(False)
        self._update_simulation()

    def _apply_preset(self, index: int) -> None:
        self.set_params(PRESETS[self._preset.itemText(index)])

    def _on_slider(self, _value: int) -> None:
        values = {name: slider.value() / _STEP for name, slider in self._sliders.items()}
        self._params = replace(self._params, **values)
        self._update_simulation()

    def apply_measured_estimate(self) -> None:
        self.refresh_measured()
        estimate = estimate_params(self._measured_shape)
        if estimate is None:
            return
        # 反推值可能超出滑块范围，按范围截断
        limits = {name: (lo, hi) for name, _l, lo, hi, _u, _t in _SLIDERS}
        values = {f.name: float(np.clip(getattr(estimate, f.name), *limits[f.name]))
                  for f in fields(DistortionParams)}
        self.set_params(DistortionParams(**values))

    # ------------------------------------------------------------ 仿真
    def _update_simulation(self) -> None:
        for name, value in self._values.items():
            value.setText(f"{getattr(self._params, name):+.1f} {value.property('unit')}")
        result = simulate(self._params)
        closed = np.append(np.arange(result.alpha.size), 0)
        self._sim_curve.setData(result.alpha[closed], result.beta[closed])
        degrees = np.degrees(result.theta)
        self._id_curve.setData(degrees, result.i_d)
        self._iq_curve.setData(degrees, result.i_q)
        orders = [k for k in result.spectrum_pct if k != 1]
        heights = [result.spectrum_pct[k] for k in orders]
        brushes = [pg.mkBrush("#ffb74d" if k > 0 else "#4fc3f7" if k < 0 else "#b0bec5")
                   for k in orders]
        if self._bars is not None:
            self._spectrum.removeItem(self._bars)
        self._bars = pg.BarGraphItem(x=orders, height=heights, width=0.7, brushes=brushes)
        self._spectrum.addItem(self._bars)
        self._spectrum.setYRange(0.0, max(5.0, max(heights) * 1.15), padding=0)
        self._spectrum.setXRange(-13.5, 13.5, padding=0)
        self._fill_column(0, result.shape)

    def _fill_column(self, column: int, shape) -> None:
        for key, cells in self._table.items():
            value = getattr(shape, f"{key}_pct") if shape is not None else None
            cells[column].setText("--" if value is None else f"{value:.1f}%")

    # ------------------------------------------------------------ 实测
    def refresh_measured(self) -> None:
        data = self._measured() if self._measured is not None else None
        xs, ys, source = data if data else ([], [], "")
        shape = trajectory_shape(xs, ys) if len(xs) else None
        self._measured_shape = shape
        self._measured_xy = (np.asarray(xs, float), np.asarray(ys, float)) if shape else None
        self._fill_column(1, shape)
        self._btn_fit.setEnabled(shape is not None)
        if shape is None:
            self._source.setText(
                (source + "：" if source else "")
                + "暂无足够的实测轨迹。请在“实时轨迹”页启用矢量可视化并让电机转过约 3/4 圈，"
                "推荐数据源选“相电流 Clarke”。")
        else:
            extra = ("；当前是 iq 重建轨迹，只反映 iq 纹波，偏心/椭圆含义与相电流不同"
                     if source.startswith("iq") else "")
            self._source.setText(
                f"数据源：{source}，{shape.points} 点，平均半径 {shape.mean_radius:.3f} A{extra}")
        self._diagnosis.setText("<br>".join(f"• {line}" for line in diagnose(shape)))
        self._draw_measured()

    def _draw_measured(self, *_args) -> None:
        if self._measured_xy is None or not self._chk_overlay.isChecked():
            self._measured_curve.setData([], [])
            return
        xs, ys = self._measured_xy
        radius = self._measured_shape.mean_radius
        self._measured_curve.setData(xs / radius, ys / radius)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh_measured()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)
