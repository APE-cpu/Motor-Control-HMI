"""矢量可视化页面：iα-iβ 电流圆与 ψα-ψβ 磁链圆。

由遥测（角度 + q 轴电流）按 id≈0 假设重构 αβ 矢量：
  iα = −iq·sin(θe)          iβ =  iq·cos(θe)
  ψd = ψf（id≈0）           ψq = Lq·iq
  ψα = ψd·cos(θe) − ψq·sin(θe)   ψβ = ψd·sin(θe) + ψq·cos(θe)
电机参数取自数字孪生配置（参数辨识页可更新）。

几何解读：正圆=正常；圆度变差/偏心=不平衡、偏心、退磁等异常。
真机优先使用 F1 1~16 kHz 电角度/Iq；没有 F1 时才回退到低速遥测。
"""
import math
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame

try:
    import numpy as np
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
        self._plot.setBackground("#10131a")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setAspectLocked(True)
        self._plot.setLabel("left", f"β ({unit})")
        self._plot.setLabel("bottom", f"α ({unit})")
        self._scatter = pg.ScatterPlotItem(
            size=4, pen=None, brush=pg.mkBrush(color + "80"))
        self._plot.addItem(self._scatter)
        self._vector = self._plot.plot(pen=pg.mkPen(color, width=2))
        # 极限圆（虚线）：电流圆=电流限幅，磁链圆=电压极限（随 Vdc/转速缩放）
        self._limit = self._plot.plot(
            pen=pg.mkPen("#ef5350", width=1.5, style=Qt.DashLine))
        self._limit_text = ""
        self._info = QLabel("|·| = --")
        v.addWidget(self._plot, 1)
        v.addWidget(self._info)

    def append(self, x: float, y: float) -> None:
        self._xs.append(x)
        self._ys.append(y)

    def extend(self, xs, ys) -> None:
        self._xs.extend(float(value) for value in xs)
        self._ys.extend(float(value) for value in ys)

    def clear(self) -> None:
        self._xs.clear()
        self._ys.clear()

    def set_limit(self, radius: float, text: str = "") -> None:
        """画/隐藏极限圆（radius 为 None 时隐藏），text 附加到信息栏。"""
        self._limit_text = text
        if radius is None:
            self._limit.setData([], [])
            return
        angs = [i * 2.0 * math.pi / 120 for i in range(121)]
        self._limit.setData([radius * math.cos(a) for a in angs],
                            [radius * math.sin(a) for a in angs])

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
        self._info.setText(f"|·| = {mag:.3f} {self._unit}    {self._limit_text}")


class VectorPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._analysis_enabled = False
        self._latest = TelemetryFrame()   # 极限圆需要转速与母线电压
        self._last_high_rate_at = 0.0

        root = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("矢量可视化（αβ 平面）")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._chk_enabled = QCheckBox("启用矢量可视化")
        self._chk_enabled.setChecked(False)
        self._chk_enabled.setToolTip(
            "默认关闭以避免在后台持续处理16 kHz点云；启用时从空余辉开始。")
        title_row.addWidget(self._chk_enabled)
        self._chk_persist = QCheckBox("无限余辉")
        self._chk_persist.setChecked(False)
        title_row.addWidget(self._chk_persist)
        btn_clear = QPushButton("清空余辉")
        title_row.addWidget(btn_clear)
        root.addLayout(title_row)

        hint = QLabel(
            "仿真模式：直接取虚拟电机 1 kHz 高速轨迹（真实 dq 变换，无频闪）；"
            "真机模式：优先使用F1 1~16 kHz轨迹，为控制绘图负载最多保留4 kHz点云；"
            "无F1时才回退到F0低速遥测。"
            "正圆 = 正常，圆度/圆心异常 = 不平衡、偏心、退磁等征兆。"
            "红色虚线为极限圆：电流圆=电流限幅；磁链圆=电压极限（半径 Vdc/√3/ωe，"
            "随母线电压和转速实时缩放，轨迹逼近它 = 电压余量耗尽/弱磁边界）。")
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
        comm.highRateTelemetryReceived.connect(self._on_high_rate)
        comm.highRateTelemetryBatchReceived.connect(self._on_high_rate_batch)
        comm.highRateTelemetryColumnsReceived.connect(self._on_high_rate_columns)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.setInterval(50)  # 20 Hz重绘；点云采集频率与绘图频率解耦
        self._chk_enabled.toggled.connect(self._set_analysis_enabled)

    def _set_analysis_enabled(self, enabled: bool) -> None:
        """按需启用高频点云处理；关闭时立即释放余辉和绘图定时器。"""
        self._analysis_enabled = bool(enabled)
        self._on_clear()
        self._last_high_rate_at = 0.0
        if self._analysis_enabled:
            self._timer.start()
        else:
            self._timer.stop()
            self._latest = TelemetryFrame()
            self._i_plot.refresh(False, None)
            self._psi_plot.refresh(False, None)

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
        if not self._analysis_enabled:
            return
        self._latest = frame
        # 仿真模式走 1 kHz 高速轨迹（_refresh 里取），10 Hz 遥测只在真机时用
        if self._comm.is_sim_running():
            return
        if time.monotonic() - self._last_high_rate_at < 0.5:
            return
        # 固件F0的angle_actual已经是电角度，不能再次乘极对数。
        theta_e = math.radians(frame.angle_actual)
        self._append_point(theta_e, 0.0, frame.current_actual)   # id ≈ 0

    def _append_high_rate_arrays(self, angle_deg, iq_a,
                                 rate_hz: int) -> None:
        if not self._analysis_enabled:
            return
        if self._comm.is_sim_running():
            return
        count = min(len(angle_deg), len(iq_a))
        if count <= 0:
            return
        # 输入可达16 kHz，而屏幕只以20 Hz刷新。保留最多4 kHz
        # 几何点云已足以呈现圆度，避免将Python/UI拖入逐点热路径。
        stride = max(1, int(rate_hz) // 4000)
        angles = np.deg2rad(np.asarray(angle_deg[:count:stride], dtype=float))
        iq = np.asarray(iq_a[:count:stride], dtype=float)
        iq = iq[:angles.size]
        s, c = np.sin(angles), np.cos(angles)
        self._i_plot.extend(-iq * s, iq * c)  # id≈0
        p = self._comm.motor_sim_params()
        psi_d = float(p.psi_f)
        psi_q = float(p.Lq) * iq
        self._psi_plot.extend(psi_d * c - psi_q * s,
                              psi_d * s + psi_q * c)
        self._last_high_rate_at = time.monotonic()

    def _on_high_rate(self, sample: dict) -> None:
        self._append_high_rate_arrays(
            [sample.get("angle_deg", 0.0)], [sample.get("iq_a", 0.0)],
            int(sample.get("rate_hz", 200)))

    def _on_high_rate_batch(self, samples: list[dict]) -> None:
        if not samples:
            return
        self._append_high_rate_arrays(
            [sample.get("angle_deg", 0.0) for sample in samples],
            [sample.get("iq_a", 0.0) for sample in samples],
            int(samples[-1].get("rate_hz", 200)))

    def _on_high_rate_columns(self, columns: dict) -> None:
        self._append_high_rate_arrays(
            columns.get("angle_deg", ()), columns.get("iq_a", ()),
            int(columns.get("rate_hz", 200)))

    def _refresh(self) -> None:
        if not self._analysis_enabled:
            return
        active = self._comm.is_sim_running() or self._comm.is_connected()
        if not active:
            self._latest = TelemetryFrame()
            self._spin = 0.0
            self._i_plot.clear()
            self._psi_plot.clear()
            self._update_limits()
            self._i_plot.refresh(self._chk_persist.isChecked(), None)
            self._psi_plot.refresh(self._chk_persist.isChecked(), None)
            return
        if self._comm.is_sim_running():
            for theta_e, i_d, i_q in self._comm.motor_sim_trace():
                self._append_point(theta_e, i_d, i_q)
        # 仅在转子确实旋转时慢放；停机后矢量停在最后一个真实采样点。
        spin_angle = None
        if abs(self._latest.speed_actual) >= 1.0:
            self._spin = (getattr(self, "_spin", 0.0) + 0.35) % (2.0 * math.pi)
            spin_angle = self._spin
        self._update_limits()
        unlimited = self._chk_persist.isChecked()
        self._i_plot.refresh(unlimited, spin_angle)
        self._psi_plot.refresh(unlimited, spin_angle)

    def _update_limits(self) -> None:
        """极限圆：电流圆画 i_max；磁链圆画电压极限 |ψ|≤Vdc/√3/ωe（忽略 Rs）。

        母线下垂/泵升或转速升高都会让电压极限圆实时缩放——磁链轨迹
        逼近该圆即意味着电压余量耗尽（进入弱磁边界）。
        """
        p = self._comm.motor_sim_params()
        f = self._latest
        self._i_plot.set_limit(p.i_max, f"极限 {p.i_max:.1f} A")
        we = abs(f.speed_actual) * math.pi / 30.0 * p.pole_pairs
        vdc_text = f"Vdc={f.vdc:.1f} V"
        if we < 1.0:
            self._psi_plot.set_limit(None, vdc_text)
            return
        r = f.vdc / math.sqrt(3.0) / we
        if r > 5.0 * p.psi_f:   # 低速时极限圆远超视图，不画避免轨迹被压缩
            self._psi_plot.set_limit(None, f"电压极限圆超视图  {vdc_text}")
        else:
            self._psi_plot.set_limit(r, f"电压极限 {r:.4f} Wb  {vdc_text}")
