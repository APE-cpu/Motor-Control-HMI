import time
from collections import deque
from typing import Deque, Dict

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from config.config import CURVE_BUFFER_SIZE

try:
    import numpy as np
    import pyqtgraph as pg
    _PG_OK = True
except Exception:  # pragma: no cover
    _PG_OK = False

if _PG_OK:
    class _YZoomPlot(pg.PlotWidget):
        """滚轮/拖拽只缩放 Y 轴（X 为时间轴自动跟随）；双击恢复自动量程。"""

        def __init__(self, owner: "TrendCurve", **kwargs) -> None:
            super().__init__(**kwargs)
            self._owner = owner
            vb = self.getViewBox()
            vb.setMouseEnabled(x=False, y=True)
            vb.sigRangeChangedManually.connect(self._on_manual)
            self.setToolTip("滚轮/拖拽缩放 Y 轴；双击恢复自动量程")

        def _on_manual(self, *_args) -> None:
            self._owner._manual_y = True

        def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt signature
            self._owner._manual_y = False
            self._owner._update_y_range()
            super().mouseDoubleClickEvent(ev)


class TrendCurve(QWidget):
    """显示一条或多条同图曲线（如转速 实际值 vs 给定值）。"""

    def __init__(self, title: str, series: Dict[str, str], y_label: str = "",
                 buffer_size: int = CURVE_BUFFER_SIZE) -> None:
        super().__init__()
        self._popout_callbacks = []
        self._title = title
        self._series = series
        self._y_label = y_label
        self._buffer_size = max(1, int(buffer_size))
        self._t0: float = 0.0  # 第一个数据点的时间，用于计算相对时间
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._buffers: Dict[str, Deque[float]] = {
            name: deque(maxlen=self._buffer_size) for name in series
        }
        self._times: Deque[float] = deque(maxlen=self._buffer_size)
        self._manual_y = False   # 用户手动缩放过 Y 轴时暂停自动量程
        if _PG_OK:
            self._plot = _YZoomPlot(self, title=title)
            self._plot.setBackground("#10131a")
            self._plot.showGrid(x=True, y=True, alpha=0.3)
            self._plot.addLegend()
            self._plot.setLabel("left", y_label)
            self._plot.setLabel("bottom", "时间 (s)")
            self._curves = {}
            for name, color in series.items():
                pen = pg.mkPen(color=color, width=2)
                self._curves[name] = self._plot.plot([], [], pen=pen, name=name)
            layout.addWidget(self._plot)
            self._stats_label = QLabel("")
            self._stats_label.setStyleSheet("color: #90a4ae; font-size: 11px;")
            layout.addWidget(self._stats_label)
        else:
            layout.addWidget(QLabel(f"[未安装 pyqtgraph]\n{title}"))

    def append(self, values: Dict[str, float]) -> None:
        now = time.time()
        if not self._times:
            self._t0 = now
        rel_t = now - self._t0
        self._times.append(rel_t)
        for name, v in values.items():
            if name in self._buffers:
                self._buffers[name].append(float(v))
        if _PG_OK:
            ts = list(self._times)
            for name, curve in self._curves.items():
                curve.setData(ts, list(self._buffers[name]))
            self._update_y_range()
            self._update_stats()
        for cb in self._popout_callbacks:
            cb(values)

    def append_batch(self, samples: list[Dict[str, float]], interval_s: float) -> None:
        """批量加入高速样本，只重绘一次，避免高频刷新阻塞界面。"""
        if not samples:
            return
        if not self._times:
            self._t0 = time.time()
        start = self._times[-1] + interval_s if self._times else 0.0
        for index, values in enumerate(samples):
            self._times.append(start + index * interval_s)
            for name, value in values.items():
                if name in self._buffers:
                    self._buffers[name].append(float(value))
        if _PG_OK:
            for name, curve in self._curves.items():
                count = len(self._buffers[name])
                curve.setData(list(self._times)[-count:], list(self._buffers[name]))
            self._update_y_range()
            self._update_stats()
        for values in samples:
            for cb in self._popout_callbacks:
                cb(values)

    def _update_y_range(self) -> None:
        """限制纵轴最小跨度：稳态噪声不再被自动缩放放大成满屏波浪。"""
        if self._manual_y:
            return
        vals = [v for buf in self._buffers.values() for v in buf]
        if not vals:
            return
        lo, hi = min(vals), max(vals)
        floor = max(abs(lo), abs(hi)) * 0.1 or 1.0   # 最小跨度=量级的10%；全零时给 ±0.5
        if hi - lo < floor:
            mid = (lo + hi) / 2.0
            lo, hi = mid - floor / 2.0, mid + floor / 2.0
        self._plot.setYRange(lo, hi, padding=0.08)

    def _update_stats(self) -> None:
        """统计行：均值 / RMS / 峰峰值 / THD（FFT 去直流，谐波能量/最大基波）。"""
        parts = []
        for name, buf in self._buffers.items():
            if len(buf) < 16:
                continue
            a = np.asarray(buf, dtype=float)
            mean, pp = a.mean(), a.max() - a.min()
            rms = float(np.sqrt((a * a).mean()))
            spec = np.abs(np.fft.rfft(a - mean))
            thd_text = "--"
            if len(spec) > 2:
                k = int(np.argmax(spec[1:])) + 1
                fund = spec[k]
                if fund > 1e-9:
                    rest = np.sqrt(max(float((spec[1:] ** 2).sum() - fund ** 2), 0.0))
                    thd_text = f"{rest / fund * 100.0:.1f}%"
            parts.append(f"{name}: μ={mean:.2f}  RMS={rms:.2f}"
                         f"  峰峰={pp:.2f}  THD={thd_text}")
        self._stats_label.setText("    |    ".join(parts))

    def add_popout_callback(self, cb) -> None:
        self._popout_callbacks.append(cb)

    def clear(self) -> None:
        """清空当前曲线数据并重置相对时间轴。"""
        self._times.clear()
        for buffer in self._buffers.values():
            buffer.clear()
        self._t0 = 0.0
        self._manual_y = False
        if _PG_OK:
            for curve in self._curves.values():
                curve.setData([], [])
            self._stats_label.clear()
