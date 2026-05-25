import time
from collections import deque
from typing import Deque, Dict

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from config.config import CURVE_BUFFER_SIZE

try:
    import pyqtgraph as pg
    _PG_OK = True
except Exception:  # pragma: no cover
    _PG_OK = False


class TrendCurve(QWidget):
    """显示一条或多条同图曲线（如转速 实际值 vs 给定值）。"""

    def __init__(self, title: str, series: Dict[str, str], y_label: str = "") -> None:
        super().__init__()
        self._popout_callbacks = []
        self._title = title
        self._series = series
        self._y_label = y_label
        self._t0: float = 0.0  # 第一个数据点的时间，用于计算相对时间
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._buffers: Dict[str, Deque[float]] = {
            name: deque(maxlen=CURVE_BUFFER_SIZE) for name in series
        }
        self._times: Deque[float] = deque(maxlen=CURVE_BUFFER_SIZE)
        if _PG_OK:
            self._plot = pg.PlotWidget(title=title)
            self._plot.setBackground("#2b2f38")
            self._plot.showGrid(x=True, y=True, alpha=0.3)
            self._plot.addLegend()
            self._plot.setLabel("left", y_label)
            self._plot.setLabel("bottom", "时间 (s)")
            self._curves = {}
            for name, color in series.items():
                pen = pg.mkPen(color=color, width=2)
                self._curves[name] = self._plot.plot([], [], pen=pen, name=name)
            layout.addWidget(self._plot)
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
        for cb in self._popout_callbacks:
            cb(values)

    def add_popout_callback(self, cb) -> None:
        self._popout_callbacks.append(cb)
