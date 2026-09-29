"""功率流离线回放：读取保存的高速数据，按时间回放各级功率。

整段功率曲线画在下方，竖线是当前时刻（可拖动）；每帧通过 frameChanged 把
该时刻的功率快照交给桑基图与计算面板。功率由 core/power_estimate 按 20 ms 分块估算。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QSlider, QVBoxLayout,
)

from core.phasor_decomposition import Capture, load_capture
from core.power_estimate import PowerParams, estimate_power_series, power_summary, powers_at

_SPEEDS = (("1/10", 0.1), ("1/3", 1 / 3), ("1×", 1.0), ("3×", 3.0), ("10×", 10.0))
_CURVES = (("inv", "逆变器输入", "#ffb74d"), ("em", "电磁功率", "#4fc3f7"),
           ("cu", "定子铜损", "#ef5350"), ("kinetic", "动能变化", "#b39ddb"))
_FRAME_MS = 40
_STEPS = 2000


class PowerPlayback(QGroupBox):
    frameChanged = Signal(dict, float, float)     # 功率快照、母线电压、时刻

    def __init__(self, params_provider=None, parent=None) -> None:
        super().__init__("离线回放", parent)
        self._params_provider = params_provider
        self._capture: Capture | None = None
        self._series: dict | None = None
        self._vbus: np.ndarray | None = None
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self._advance)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self._btn_open = QPushButton("打开高速数据…")
        self._btn_open.clicked.connect(self._open_dialog)
        self._source = QLabel("未加载：选择实验目录里的 高速数据.csv（旧记录为 RLS辨识数据.csv）")
        self._source.setStyleSheet("color:#90a4ae;")
        self._t0, self._t1 = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self._t0, self._t1):
            spin.setDecimals(2)
            spin.setSuffix(" s")
        self._btn_apply = QPushButton("计算")
        self._btn_apply.clicked.connect(self.recompute)
        bar.addWidget(self._btn_open)
        bar.addWidget(self._source, 1)
        for text, widget in (("时间窗", self._t0), ("至", self._t1)):
            bar.addWidget(QLabel(text))
            bar.addWidget(widget)
        bar.addWidget(self._btn_apply)
        root.addLayout(bar)
        self._controls = QHBoxLayout()
        root.addLayout(self._controls)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#10131a")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel("bottom", "时间", units="s")
        self._plot.setLabel("left", "功率", units="W")
        self._plot.addLegend(offset=(8, 8))
        self._plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._plot.setMinimumHeight(150)
        self._curves = {key: self._plot.plot([], [], pen=pg.mkPen(color, width=1.4), name=name)
                        for key, name, color in _CURVES}
        self._cursor = pg.InfiniteLine(angle=90, movable=True,
                                       pen=pg.mkPen("#ffffff", width=1.2))
        self._cursor.sigPositionChanged.connect(self._on_cursor)
        self._plot.addItem(self._cursor)
        self._summary = QLabel()
        self._summary.setStyleSheet("color:#80cbc4;")
        root.addWidget(self._summary)
        root.addWidget(self._plot, 1)

        controls = self._controls
        self._btn_play = QPushButton("▶ 播放")
        self._btn_play.setCheckable(True)
        self._btn_play.toggled.connect(self._on_play)
        self._speed = QComboBox()
        for text, value in _SPEEDS:
            self._speed.addItem(f"速度 {text}", value)
        self._speed.setCurrentIndex(1)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, _STEPS)
        self._slider.valueChanged.connect(self._on_slider)
        self._time_label = QLabel("t = --")
        self._time_label.setMinimumWidth(120)
        controls.addWidget(self._btn_play)
        controls.addWidget(self._speed)
        controls.addWidget(self._slider, 1)
        controls.addWidget(self._time_label)

    # ------------------------------------------------------------ 数据
    def _params(self) -> PowerParams:
        from core.dyno_load import dyno_setting
        mode, load_inertia = dyno_setting()
        motor = None
        if callable(self._params_provider):
            try:
                motor = self._params_provider()
            except Exception:  # noqa: BLE001 - 参数不可用时用铭牌默认值
                motor = None
        base = PowerParams.from_motor(motor) if motor is not None else PowerParams()
        return base.with_dyno(mode, motor, load_inertia)

    def _open_dialog(self) -> None:
        start = Path(__file__).resolve().parents[1] / "波形记录"
        path, _ = QFileDialog.getOpenFileName(
            self, "打开高速数据", str(start if start.exists() else ""),
            "高速数据 (高速数据.csv RLS辨识数据.csv);;CSV (*.csv);;所有文件 (*)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> bool:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            capture = load_capture(path)
            if "iq_a" not in capture.extra or "vq_raw" not in capture.extra:
                raise ValueError("该文件没有 iq_a / vq_raw 列，无法估算功率（需要 16 kHz 高速数据）")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法回放", str(exc))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self._capture = capture
        start, end = float(capture.time[0]), float(capture.time[-1])
        for spin in (self._t0, self._t1):
            spin.blockSignals(True)
            spin.setRange(start, end)
            spin.blockSignals(False)
        self._t0.setValue(start)
        self._t1.setValue(end)
        self._source.setText(f"{capture.source} · {capture.duration:.2f} s")
        self.recompute()
        return True

    def recompute(self) -> None:
        if self._capture is None:
            return
        t0, t1 = self._t0.value(), self._t1.value()
        if t1 <= t0:
            QMessageBox.warning(self, "时间窗无效", "结束时间必须大于开始时间")
            return
        window = self._capture.window(t0, t1)
        try:
            self._series = estimate_power_series(window.time, window.columns(), self._params())
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "无法估算功率", str(exc))
            return
        vbus = window.extra.get("vbus_v")
        self._vbus = (np.interp(self._series["time"], window.time, vbus)
                      if vbus is not None else np.full(self._series["time"].size, 24.0))
        for key, curve in self._curves.items():
            curve.setData(self._series["time"], self._series[key])
        summary = power_summary(self._series)
        mean = summary["mean_w"]
        efficiency = summary["motor_efficiency"]
        self._summary.setText(
            f"平均：逆变器 {mean['inv']:.2f} W · 铜损 {mean['cu']:.2f} W · 电磁 {mean['em']:.2f} W · "
            f"未解释 {mean['inv'] - mean['cu'] - mean['em']:+.2f} W"
            + (f" · 电机效率 {efficiency:.1%}" if efficiency is not None else ""))
        self.seek(float(self._series["time"][0]))

    @property
    def loaded(self) -> bool:
        return self._series is not None

    # ------------------------------------------------------------ 播放
    def _bounds(self) -> tuple[float, float]:
        times = self._series["time"]
        return float(times[0]), float(times[-1])

    def seek(self, t: float) -> None:
        if self._series is None:
            return
        t0, t1 = self._bounds()
        self._t = float(np.clip(t, t0, t1))
        self._cursor.blockSignals(True)
        self._cursor.setValue(self._t)
        self._cursor.blockSignals(False)
        self._slider.blockSignals(True)
        self._slider.setValue(int(round((self._t - t0) / max(t1 - t0, 1e-9) * _STEPS)))
        self._slider.blockSignals(False)
        self._time_label.setText(f"t = {self._t:.3f} s")
        index = int(np.clip(np.searchsorted(self._series["time"], self._t),
                            0, self._series["time"].size - 1))
        self.frameChanged.emit(powers_at(self._series, self._t),
                               float(self._vbus[index]), self._t)

    def _advance(self) -> None:
        if self._series is None:
            return
        t0, t1 = self._bounds()
        step = self._speed.currentData() * _FRAME_MS / 1000.0
        self.seek(t0 + (self._t + step - t0) % max(t1 - t0, 1e-9))

    def _on_play(self, playing: bool) -> None:
        self._btn_play.setText("⏸ 暂停" if playing else "▶ 播放")
        if playing and self._series is not None:
            self._timer.start()
        else:
            self._timer.stop()

    def _on_slider(self, value: int) -> None:
        if self._series is not None:
            t0, t1 = self._bounds()
            self.seek(t0 + (t1 - t0) * value / _STEPS)

    def _on_cursor(self, line) -> None:
        self.seek(float(line.value()))

    def stop(self) -> None:
        self._btn_play.setChecked(False)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if self._btn_play.isChecked():
            self._timer.start()
