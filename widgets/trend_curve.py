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
        """滚轮/拖拽只作用于时间轴；Y 轴自动适配可见数据。双击恢复跟随。"""

        def __init__(self, owner: "TrendCurve", **kwargs) -> None:
            super().__init__(**kwargs)
            self._owner = owner
            vb = self.getViewBox()
            vb.setMouseEnabled(x=True, y=False)
            vb.sigRangeChangedManually.connect(self._on_manual)
            self.setToolTip("滚轮缩放时间轴、拖拽平移；双击恢复自动跟随")

        def _on_manual(self, *_args) -> None:
            self._owner._manual_x = True
            self._owner._update_y_range()

        def wheelEvent(self, ev) -> None:  # noqa: N802 - Qt signature
            """视窗模式（如相电流）下，滚轮改变视窗宽度但保持实时跟随，不冻结；
            普通曲线仍走默认的时间轴缩放（会切手动、停跟随）。"""
            if self._owner._view_window_s > 0.0:
                delta = ev.angleDelta().y()
                if delta != 0:
                    factor = 0.8 if delta > 0 else 1.25   # 上滚放大=窗变窄
                    new_w = min(max(self._owner._view_window_s * factor,
                                    0.005), 5.0)
                    self._owner.set_view_window(new_w)
                ev.accept()
                return
            super().wheelEvent(ev)

        def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt signature
            self._owner._manual_x = False
            if self._owner._view_window_s > 0.0:
                self._owner._apply_xview()
            else:
                self.getViewBox().enableAutoRange(x=True)
            self._owner._update_y_range()
            super().mouseDoubleClickEvent(ev)


class TrendCurve(QWidget):
    """显示一条或多条同图曲线（如转速 实际值 vs 给定值）。"""

    def __init__(self, title: str, series: Dict[str, str], y_label: str = "",
                 buffer_size: int = CURVE_BUFFER_SIZE) -> None:
        super().__init__()
        self._popout_callbacks = []
        self._popout_batch_callbacks = []
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
        self._manual_x = False   # 用户手动缩放过时间轴时停止自动跟随
        self._smooth_n = 1       # 显示平滑窗口（1=关）；仅平滑显示，不动缓冲
        self._disp: Dict[str, tuple] = {}   # 最近一次显示的 (times, values)，供Y量程
        self._view_window_s = 0.0  # >0 时只显示最近这么多秒（如相电流看几个电周期）
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
            self._draw()
            self._apply_xview()
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
            self._draw()
            self._apply_xview()
            self._update_y_range()
            self._update_stats()
        for cb in self._popout_batch_callbacks:
            cb(samples, interval_s)

    def _smooth(self, y):
        """居中滑动平均，仅用于显示：零净相移、不改缓冲、不进控制。"""
        n = self._smooth_n
        if n <= 1 or len(y) < 3:
            return y
        n = min(n, len(y))
        arr = np.asarray(y, dtype=float)
        pad = n // 2
        ypad = np.pad(arr, pad, mode="edge")
        kernel = np.ones(n) / float(n)
        return np.convolve(ypad, kernel, mode="same")[pad:pad + len(arr)]

    def _draw(self) -> None:
        """把缓冲画到曲线（按需居中平滑），并缓存显示值供 Y 轴量程使用。"""
        if not _PG_OK:
            return
        ts = list(self._times)
        self._disp = {}
        for name, curve in self._curves.items():
            y = list(self._buffers[name])
            tsu = ts[-len(y):] if y else []
            yv = self._smooth(y) if (self._smooth_n > 1 and len(y) >= 3) else y
            self._disp[name] = (tsu, yv)
            curve.setData(tsu, list(yv))

    def set_smoothing(self, n: int) -> None:
        """设置显示平滑窗口（1=关）。仅影响显示，不动原始缓冲/导出/统计。"""
        new_n = max(1, int(n))
        if new_n == self._smooth_n:
            return
        self._smooth_n = new_n
        if _PG_OK and self._times:
            self._draw()
            self._update_y_range()

    def set_view_window(self, seconds: float) -> None:
        """>0 时时间轴只显示最近 seconds 秒（滚动跟随），0=显示全部缓冲。
        仅改显示范围，缓冲/导出/统计仍是全量。用户滚轮缩放后自动让位，
        双击恢复此视窗。"""
        self._view_window_s = max(0.0, float(seconds))
        if _PG_OK:
            # 视窗模式=示波器：禁用拖拽平移（滚轮已被 wheelEvent 接管改窗宽），
            # 避免一拖就切手动、时间轴锁死看着像冻住。0=恢复普通可平移。
            self._plot.getViewBox().setMouseEnabled(
                x=(self._view_window_s <= 0.0), y=False)
            if self._times:
                self._apply_xview()
                self._update_y_range()

    def _apply_xview(self) -> None:
        """在跟随模式下把 X 轴钉在最近 view_window 秒。程序化 setXRange 不会触发
        sigRangeChangedManually，故不会误判为用户手动缩放。"""
        if (_PG_OK and self._view_window_s > 0.0 and not self._manual_x
                and self._times):
            t_end = self._times[-1]
            self._plot.setXRange(t_end - self._view_window_s, t_end, padding=0)

    def _visible_xrange(self):
        """当前用于取 Y 量程的 X 窗口：手动缩放 > 滚动视窗 > 无（全量）。"""
        if not self._times:
            return None
        if self._manual_x:
            x0, x1 = self._plot.getViewBox().viewRange()[0]
            return x0, x1
        if self._view_window_s > 0.0:
            t_end = self._times[-1]
            return t_end - self._view_window_s, t_end
        return None

    def _update_y_range(self) -> None:
        """Y 轴自动量程：手动缩放时间轴后只按可见窗口取值，放大后的正弦
        能撑满纵轴；同时限制最小跨度，稳态噪声不被放大成满屏波浪。"""
        # 用"显示值"（可能已平滑）算量程，曲线才撑得满、不留空白。
        disp = self._disp or {name: (list(self._times)[-len(buf):], list(buf))
                              for name, buf in self._buffers.items()}
        vals = []
        xr = self._visible_xrange()
        if xr is not None:
            x0, x1 = xr
            for tsu, yv in disp.values():
                vals.extend(v for t, v in zip(tsu, yv) if x0 <= t <= x1)
        if not vals:
            vals = [v for _tsu, yv in disp.values() for v in yv]
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

    def resume_follow(self) -> None:
        """恢复时间轴自动跟随。新一次运行开始时由页面调用，避免用户
        上次缩放定格后误以为曲线不再更新。"""
        if not self._manual_x:
            return
        self._manual_x = False
        if _PG_OK:
            if self._view_window_s > 0.0:
                self._apply_xview()
            else:
                self._plot.getViewBox().enableAutoRange(x=True)
            self._update_y_range()

    def add_popout_callback(self, cb) -> None:
        self._popout_callbacks.append(cb)

    def add_popout_batch_callback(self, cb) -> None:
        self._popout_batch_callbacks.append(cb)

    def clear(self) -> None:
        """清空当前曲线数据并重置相对时间轴。"""
        self._times.clear()
        for buffer in self._buffers.values():
            buffer.clear()
        self._t0 = 0.0
        self._manual_x = False
        self._disp = {}
        if _PG_OK:
            self._plot.getViewBox().enableAutoRange(x=True)
            for curve in self._curves.values():
                curve.setData([], [])
            self._stats_label.clear()
