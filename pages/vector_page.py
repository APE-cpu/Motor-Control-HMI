"""矢量可视化页面：iα-iβ 电流圆与 ψα-ψβ 磁链圆。

由遥测（角度 + q 轴电流）按 id≈0 假设重构 αβ 矢量：
  iα = −iq·sin(θe)          iβ =  iq·cos(θe)
  ψd = ψf（id≈0）           ψq = Lq·iq
  ψα = ψd·cos(θe) − ψq·sin(θe)   ψβ = ψd·sin(θe) + ψq·cos(θe)
电机参数取自数字孪生配置（参数辨识页可更新）。

几何解读：正圆=正常；圆度变差/偏心=不平衡、偏心、退磁等异常。
注意：10 Hz 遥测在时间上欠采样，轨迹靠余辉点云累积成圆；
真机高保真矢量图需协议增加突发快照通道。
"""
import math
from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:  # pragma: no cover
    _PG_OK = False

_TRAIL = 2000       # 滑动余辉点数（1 kHz 下约 2 s）
_TRAIL_MAX = 20000  # 无限余辉安全上限（兼顾重绘性能）


class _CirclePlot(QWidget):
    """带余辉散点 + 当前矢量线的正方形轨迹图。"""

    def __init__(self, title: str, unit: str, color: str) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self._xs: deque = deque(maxlen=_TRAIL_MAX)
        self._ys: deque = deque(maxlen=_TRAIL_MAX)
        self._unit = unit
        self._plot = pg.PlotWidget(title=title)
        self._plot.setBackground("#2b2f38")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setAspectLocked(True)
        self._plot.setLabel("left", f"β ({unit})")
        self._plot.setLabel("bottom", f"α ({unit})")
        self._scatter = pg.ScatterPlotItem(
            size=4, pen=None, brush=pg.mkBrush(color + "80"))
        self._plot.addItem(self._scatter)
        self._vector = self._plot.plot(pen=pg.mkPen(color, width=2))
        self._info = QLabel("|·| = --")
        v.addWidget(self._plot, 1)
        v.addWidget(self._info)

    def append(self, x: float, y: float) -> None:
        self._xs.append(x)
        self._ys.append(y)

    def clear(self) -> None:
        self._xs.clear()
        self._ys.clear()

    def refresh(self, unlimited: bool = True, spin_angle: float = None) -> None:
        xs, ys = list(self._xs), list(self._ys)
        if not unlimited:
            xs, ys = xs[-_TRAIL:], ys[-_TRAIL:]
        self._scatter.setData(xs, ys)
        if not xs:
            self._vector.setData([], [])
            return
        # 矢量线做"慢放频闪"：每帧指向角度最接近 spin_angle 的真实采样点
        # （屏幕刷新率远低于电角频率，直接画最新点会频闪跳动）
        tip_x, tip_y = xs[-1], ys[-1]
        if spin_angle is not None:
            best = None
            for x, y in zip(xs[-600:], ys[-600:]):
                diff = abs((math.atan2(y, x) - spin_angle + math.pi)
                           % (2.0 * math.pi) - math.pi)
                if best is None or diff < best[0]:
                    best = (diff, x, y)
            if best is not None:
                _, tip_x, tip_y = best
        self._vector.setData([0.0, tip_x], [0.0, tip_y])
        mag = math.hypot(tip_x, tip_y)
        self._info.setText(f"|·| = {mag:.3f} {self._unit}")


class VectorPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm

        root = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("矢量可视化（αβ 平面）")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._chk_persist = QCheckBox("无限余辉")
        self._chk_persist.setChecked(True)
        title_row.addWidget(self._chk_persist)
        btn_clear = QPushButton("清空余辉")
        title_row.addWidget(btn_clear)
        root.addLayout(title_row)

        hint = QLabel(
            "仿真模式：直接取虚拟电机 1 kHz 高速轨迹（真实 dq 变换，无频闪）；"
            "真机模式：由 10 Hz 遥测按 id≈0 重构（欠采样，需协议突发快照通道）。"
            "正圆 = 正常，圆度/圆心异常 = 不平衡、偏心、退磁等征兆。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #90a4ae;")
        root.addWidget(hint)

        if not _PG_OK:
            root.addWidget(QLabel("未安装 pyqtgraph，无法显示矢量图"))
            return

        box = QGroupBox("轨迹")
        h = QHBoxLayout(box)
        self._i_plot = _CirclePlot("电流圆 iα-iβ", "A", "#4fc3f7")
        self._psi_plot = _CirclePlot("磁链圆 ψα-ψβ", "Wb", "#ffb74d")
        h.addWidget(self._i_plot, 1)
        h.addWidget(self._psi_plot, 1)
        root.addWidget(box, 1)

        btn_clear.clicked.connect(self._on_clear)
        comm.telemetryReceived.connect(self._on_telemetry)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(100)

    def _on_clear(self) -> None:
        self._i_plot.clear()
        self._psi_plot.clear()

    def _append_point(self, theta_e: float, i_d: float, i_q: float) -> None:
        p = self._comm.motor_sim_params()
        s, c = math.sin(theta_e), math.cos(theta_e)
        self._i_plot.append(i_d * c - i_q * s, i_d * s + i_q * c)
        psi_d, psi_q = p.psi_f + p.Ld * i_d, p.Lq * i_q
        self._psi_plot.append(psi_d * c - psi_q * s, psi_d * s + psi_q * c)

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        # 仿真模式走 1 kHz 高速轨迹（_refresh 里取），10 Hz 遥测只在真机时用
        if self._comm.is_sim_running():
            return
        p = self._comm.motor_sim_params()
        theta_e = math.radians(frame.angle_actual) * p.pole_pairs
        self._append_point(theta_e, 0.0, frame.current_actual)   # id ≈ 0

    def _refresh(self) -> None:
        if self._comm.is_sim_running():
            for theta_e, i_d, i_q in self._comm.motor_sim_trace():
                self._append_point(theta_e, i_d, i_q)
        # 演示旋转角：每帧 0.35 rad，约 1.8 s 扫一圈（慢放，幅值取真实采样点）
        self._spin = (getattr(self, "_spin", 0.0) + 0.35) % (2.0 * math.pi)
        unlimited = self._chk_persist.isChecked()
        self._i_plot.refresh(unlimited, self._spin)
        self._psi_plot.refresh(unlimited, self._spin)
