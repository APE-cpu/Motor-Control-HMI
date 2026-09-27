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
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from core.vector_shape import clarke, trajectory_shape

try:
    import numpy as np
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:  # pragma: no cover
    _PG_OK = False

_TRAIL = 800        # 普通余辉最近约 0.8 s
_TRAIL_MAX = 4000   # 无限余辉的内存上限
_DISPLAY_POINTS = 1200  # 散点渲染上限；保留全量几何缓存


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
        self._limit_radius = None
        self._dirty = True
        self._info = QLabel("|·| = --")
        v.addWidget(self._plot, 1)
        v.addWidget(self._info)

    def append(self, x: float, y: float) -> None:
        self._xs.append(x)
        self._ys.append(y)
        self._dirty = True

    def extend(self, xs, ys) -> None:
        self._xs.extend(float(value) for value in xs)
        self._ys.extend(float(value) for value in ys)
        self._dirty = True

    def clear(self) -> None:
        self._xs.clear()
        self._ys.clear()
        self._dirty = True

    def set_limit(self, radius: float, text: str = "") -> None:
        """画/隐藏极限圆（radius 为 None 时隐藏），text 附加到信息栏。"""
        self._dirty = self._dirty or text != self._limit_text
        self._limit_text = text
        if radius == self._limit_radius:
            return
        self._limit_radius = radius
        if radius is None:
            self._limit.setData([], [])
            return
        angs = [i * 2.0 * math.pi / 120 for i in range(121)]
        self._limit.setData([radius * math.cos(a) for a in angs],
                            [radius * math.sin(a) for a in angs])

    def refresh(self, unlimited: bool = True, spin_angle: float = None) -> None:
        if not self._dirty and spin_angle is None:
            return
        xs, ys = list(self._xs), list(self._ys)
        if not unlimited:
            xs, ys = xs[-_TRAIL:], ys[-_TRAIL:]
        if len(xs) > _DISPLAY_POINTS:
            stride = math.ceil(len(xs) / _DISPLAY_POINTS)
            draw_x, draw_y = xs[::stride], ys[::stride]
        else:
            draw_x, draw_y = xs, ys
        if self._dirty:
            self._scatter.setData(draw_x, draw_y)
        self._dirty = False
        if not xs:
            self._vector.setData([], [])
            return
        # 矢量线做"慢放频闪"：每帧指向角度最接近 spin_angle 的真实采样点
        # （屏幕刷新率远低于电角频率，直接画最新点会频闪跳动）
        tip_x, tip_y = xs[-1], ys[-1]
        if spin_angle is not None:
            best = None
            for x, y in zip(xs[-200:], ys[-200:]):
                diff = abs((math.atan2(y, x) - spin_angle + math.pi)
                           % (2.0 * math.pi) - math.pi)
                if best is None or diff < best[0]:
                    best = (diff, x, y)
            if best is not None:
                _, tip_x, tip_y = best
        self._vector.setData([0.0, tip_x], [0.0, tip_y])
        mag = math.hypot(tip_x, tip_y)
        self._info.setText(f"|·| = {mag:.3f} {self._unit}    {self._limit_text}")

    def render_native(self, xs, ys, tip, update_scatter: bool = True) -> None:
        """直接接收 C++ 数组；Qt 只处理限长显示点，不创建逐点 Python 对象。"""
        if update_scatter:
            stride = max(1, math.ceil(len(xs) / _DISPLAY_POINTS))
            self._scatter.setData(xs[::stride], ys[::stride])
        if len(xs):
            self._vector.setData((0.0, tip[0]), (0.0, tip[1]))
            self._info.setText(
                f"|·| = {math.hypot(*tip):.3f} {self._unit}    {self._limit_text}")
        else:
            self._vector.setData([], [])
            self._info.setText(f"|·| = --    {self._limit_text}")
        self._dirty = False


class VectorPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._analysis_enabled = False
        self._latest = TelemetryFrame()   # 极限圆需要转速与母线电压
        self._last_high_rate_at = 0.0
        self._current_source = "iq"      # "iq"：iq+电角度重建；"clarke"：相电流
        self._clarke_missing = False     # Clarke 模式下数据流缺少 Ia/Ib
        self._native_trail = getattr(comm, "_native_vector_trail", None)
        self._native_stream_active = False
        self._native_shape_generation = -1
        self._native_draw_generation = -1
        self._last_shape_analysis_at = 0.0
        self._shape_pending = False

        root = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("矢量可视化（αβ 平面）")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(QLabel("电流圆数据源"))
        self._cmb_source = QComboBox()
        self._cmb_source.addItem("iq + 电角度重建", "iq")
        self._cmb_source.addItem("相电流 Clarke（Ia、Ib）", "clarke")
        self._cmb_source.setToolTip(
            "iq 重建：半径 = iq，只反映 iq 随电角度的纹波；\n"
            "相电流 Clarke：直接由采样 Ia、Ib 合成，三相不对称表现为椭圆、"
            "采样零偏表现为圆心偏移。需要 F1 32/40 字节帧携带 Ia/Ib。")
        title_row.addWidget(self._cmb_source)
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
            "真机模式：优先使用F1 1~16 kHz轨迹，为控制绘图负载抽取约1 kHz点云；"
            "无F1时才回退到F0低速遥测。"
            "正圆 = 正常，圆度/圆心异常 = 不平衡、偏心、退磁等征兆。"
            "电流圆可选数据源：“iq 重建”的半径就是 iq，只反映 iq 随电角度的纹波；"
            "“相电流 Clarke”直接由 Ia、Ib 合成，负序（三相不对称）呈椭圆、采样零偏使圆心偏移。"
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
        self._shape_label = QLabel()
        self._shape_label.setWordWrap(True)
        self._shape_label.setToolTip(
            "对电流圆轨迹按极角做谐波拟合，形变幅值相对平均半径：\n"
            "偏心（1 次）：不转的矢量，如采样零偏；\n"
            "椭圆（2 次）：负序，即三相增益/相位不对称，括号内为长轴方向；\n"
            "三角（3 次）：负序 2 次谐波。")
        root.addWidget(self._shape_label)
        self._update_shape_label()

        btn_clear.clicked.connect(self._on_clear)
        self._cmb_source.currentIndexChanged.connect(self._on_source_changed)
        comm.telemetryReceived.connect(self._on_telemetry)
        comm.highRateTelemetryReceived.connect(self._on_high_rate)
        comm.highRateTelemetryBatchReceived.connect(self._on_high_rate_batch)
        comm.highRateTelemetryColumnsReceived.connect(self._on_high_rate_columns)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(33)  # 约30 Hz重绘；点云采集频率与绘图频率解耦
        self._chk_enabled.toggled.connect(self._set_analysis_enabled)
        self._chk_persist.toggled.connect(self._on_persistence_changed)

    def _on_persistence_changed(self, _enabled: bool) -> None:
        self._i_plot._dirty = True
        self._psi_plot._dirty = True
        self._native_shape_generation = -1
        self._native_draw_generation = -1
        self._shape_pending = True
        if self._analysis_enabled and self.isVisible():
            self._refresh()

    def _configure_native_trail(self) -> None:
        if self._native_trail is None:
            return
        params = self._comm.motor_sim_params()
        self._native_trail.configure(
            self._analysis_enabled and self.isVisible()
            and not self._comm.is_sim_running(),
            self._current_source == "clarke",
            float(params.psi_f), float(params.Lq))

    def _set_analysis_enabled(self, enabled: bool) -> None:
        """按需启用高频点云处理；关闭时立即释放余辉和绘图定时器。"""
        self._analysis_enabled = bool(enabled)
        self._on_clear()
        self._last_high_rate_at = 0.0
        self._configure_native_trail()
        if self._analysis_enabled and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
            self._latest = TelemetryFrame()
            self._i_plot.refresh(False, None)
            self._psi_plot.refresh(False, None)

    def _on_clear(self) -> None:
        self._i_plot.clear()
        self._psi_plot.clear()
        if self._native_trail is not None:
            self._native_trail.clear()
        self._native_stream_active = False
        self._native_shape_generation = -1
        self._native_draw_generation = -1
        self._last_shape_analysis_at = 0.0
        self._shape_pending = False
        self._clarke_missing = False
        self._update_shape_label()

    def _on_source_changed(self, _index: int) -> None:
        """切换电流圆数据源；两种轨迹不可混在同一余辉里。"""
        self._current_source = self._cmb_source.currentData() or "iq"
        self._on_clear()
        self._configure_native_trail()

    def _update_shape_label(self, xs=None, ys=None) -> None:
        if self._current_source == "clarke" and self._clarke_missing:
            self._shape_label.setText(
                "圆度：当前数据流不含 Ia/Ib（需 F1 32/40 字节帧），"
                "无法按相电流 Clarke 画电流圆，请切回“iq + 电角度重建”。")
            return
        if xs is None or ys is None:
            xs, ys = list(self._i_plot._xs), list(self._i_plot._ys)
            if not self._chk_persist.isChecked():
                xs, ys = xs[-_TRAIL:], ys[-_TRAIL:]
        shape = trajectory_shape(xs, ys)
        if shape is None:
            self._shape_label.setText("圆度：等待电流圆轨迹覆盖至少约 3/4 圈…")
            return
        self._shape_label.setText(
            f"圆度（平均半径 {shape.mean_radius:.3f} A，{shape.points} 点）："
            f"偏心 {shape.eccentric_pct:.1f}%  ·  "
            f"椭圆 {shape.ellipse_pct:.1f}%（长轴 {shape.ellipse_axis_deg:.0f}°）  ·  "
            f"三角 {shape.triangle_pct:.1f}%")

    def _append_point(self, theta_e: float, i_d: float, i_q: float,
                      include_current: bool = True) -> None:
        p = self._comm.motor_sim_params()
        s, c = math.sin(theta_e), math.cos(theta_e)
        if include_current:
            self._i_plot.append(i_d * c - i_q * s, i_d * s + i_q * c)
        psi_d, psi_q = p.psi_f + p.Ld * i_d, p.Lq * i_q
        self._psi_plot.append(psi_d * c - psi_q * s, psi_d * s + psi_q * c)

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        if not self._analysis_enabled or not self.isVisible():
            return
        self._latest = frame
        # 仿真模式走 1 kHz 高速轨迹（_refresh 里取），10 Hz 遥测只在真机时用
        if self._comm.is_sim_running():
            return
        if time.monotonic() - self._last_high_rate_at < 0.5:
            return
        # 固件F0的angle_actual已经是电角度，不能再次乘极对数。
        theta_e = math.radians(frame.angle_actual)
        # F0 不带相电流：Clarke 模式下只更新磁链圆
        clarke_mode = self._current_source == "clarke"
        if clarke_mode:
            self._clarke_missing = True
        self._append_point(theta_e, 0.0, frame.current_actual,   # id ≈ 0
                           include_current=not clarke_mode)

    def _append_high_rate_arrays(self, angle_deg, iq_a,
                                 rate_hz: int, ia_a=None, ib_a=None) -> None:
        if not self._analysis_enabled or not self.isVisible():
            return
        if self._comm.is_sim_running():
            return
        count = min(len(angle_deg), len(iq_a))
        if count <= 0:
            return
        # 输入可达16 kHz；仅为画图保留约1 kHz几何点，不改原始遥测。
        stride = max(1, math.ceil(int(rate_hz) / 1000))
        angles = np.deg2rad(np.asarray(angle_deg[:count:stride], dtype=float))
        iq = np.asarray(iq_a[:count:stride], dtype=float)
        iq = iq[:angles.size]
        s, c = np.sin(angles), np.cos(angles)
        if self._current_source == "clarke":
            if (ia_a is not None and ib_a is not None
                    and min(len(ia_a), len(ib_a)) >= count):
                self._clarke_missing = False
                self._i_plot.extend(*clarke(ia_a[:count:stride],
                                            ib_a[:count:stride]))
            else:
                self._clarke_missing = True
        else:
            self._i_plot.extend(-iq * s, iq * c)  # id≈0
        p = self._comm.motor_sim_params()
        psi_d = float(p.psi_f)
        psi_q = float(p.Lq) * iq
        self._psi_plot.extend(psi_d * c - psi_q * s,
                              psi_d * s + psi_q * c)
        self._last_high_rate_at = time.monotonic()

    def _on_high_rate(self, sample: dict) -> None:
        if not self._analysis_enabled or not self.isVisible():
            return
        self._native_stream_active = False
        has_phase = "ia_a" in sample and "ib_a" in sample
        self._append_high_rate_arrays(
            [sample.get("angle_deg", 0.0)], [sample.get("iq_a", 0.0)],
            int(sample.get("rate_hz", 200)),
            [sample["ia_a"]] if has_phase else None,
            [sample["ib_a"]] if has_phase else None)

    def _on_high_rate_batch(self, samples: list[dict]) -> None:
        if not samples or not self._analysis_enabled or not self.isVisible():
            return
        self._native_stream_active = False
        has_phase = all("ia_a" in s and "ib_a" in s for s in samples)
        self._append_high_rate_arrays(
            [sample.get("angle_deg", 0.0) for sample in samples],
            [sample.get("iq_a", 0.0) for sample in samples],
            int(samples[-1].get("rate_hz", 200)),
            [s["ia_a"] for s in samples] if has_phase else None,
            [s["ib_a"] for s in samples] if has_phase else None)

    def _on_high_rate_columns(self, columns: dict) -> None:
        if (columns.get("vector_native") and self._native_trail is not None
                and self._analysis_enabled and self.isVisible()):
            if not self._native_stream_active:
                self._i_plot.clear()
                self._psi_plot.clear()
                self._native_shape_generation = -1
                self._native_draw_generation = -1
            self._native_stream_active = True
            self._last_high_rate_at = time.monotonic()
            return
        self._native_stream_active = False
        self._append_high_rate_arrays(
            columns.get("angle_deg", ()), columns.get("iq_a", ()),
            int(columns.get("rate_hz", 200)),
            columns.get("ia_a"), columns.get("ib_a"))

    def _refresh(self) -> None:
        if not self._analysis_enabled or not self.isVisible():
            return
        self._configure_native_trail()
        active = self._comm.is_sim_running() or self._comm.is_connected()
        if not active:
            self._latest = TelemetryFrame()
            self._spin = 0.0
            if self._native_stream_active and self._native_trail is not None:
                self._native_trail.clear()
                self._native_stream_active = False
            self._i_plot.clear()
            self._psi_plot.clear()
            self._update_shape_label()
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
            self._spin = (getattr(self, "_spin", 0.0)
                          + 0.35 * self._timer.interval() / 100.0) % (2.0 * math.pi)
            spin_angle = self._spin
        self._update_limits()
        unlimited = self._chk_persist.isChecked()
        now = time.monotonic()
        if (self._native_stream_active and self._native_trail is not None
                and not self._comm.is_sim_running()):
            snapshot = self._native_trail.snapshot(unlimited, spin_angle)
            self._clarke_missing = bool(snapshot["clarke_missing"])
            generation = snapshot["generation"]
            if (generation != self._native_shape_generation
                    and now - self._last_shape_analysis_at >= 0.1):
                self._update_shape_label(
                    snapshot["current_x"], snapshot["current_y"])
                self._native_shape_generation = generation
                self._last_shape_analysis_at = now
            update_scatter = generation != self._native_draw_generation
            self._i_plot.render_native(
                snapshot["current_x"], snapshot["current_y"],
                snapshot["current_tip"], update_scatter)
            self._psi_plot.render_native(
                snapshot["flux_x"], snapshot["flux_y"],
                snapshot["flux_tip"], update_scatter)
            self._native_draw_generation = generation
            return
        if self._i_plot._dirty:
            self._shape_pending = True
        if self._shape_pending and now - self._last_shape_analysis_at >= 0.1:
            self._update_shape_label()
            self._last_shape_analysis_at = now
            self._shape_pending = False
        self._i_plot.refresh(unlimited, spin_angle)
        self._psi_plot.refresh(unlimited, spin_angle)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._configure_native_trail()
        if _PG_OK and self._analysis_enabled:
            self._timer.start()

    def hideEvent(self, event) -> None:
        if _PG_OK:
            self._timer.stop()
        super().hideEvent(event)
        self._configure_native_trail()

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
