"""监控页面：实时数据、统计、趋势曲线。"""
import datetime
import math
import os
import time
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import CMD_START, MONITOR_REFRESH_MS
from widgets.trend_curve import TrendCurve
from widgets.temperature_label import TemperatureLabel
from config.config import TEMP_HIGH_THRESHOLD, TEMP_NORMAL_THRESHOLD


def _make_curve_panel(curve: TrendCurve, title: str) -> QWidget:
    """把 TrendCurve 包装成带弹出按钮的面板。"""
    panel = QWidget()
    v = QVBoxLayout(panel)
    v.setContentsMargins(0, 0, 0, 0)
    btn = QPushButton("弹出 ↗")
    btn.setFixedHeight(22)

    def _popout():
        win = QWidget(None, Qt.Window)
        win.setWindowTitle(title)
        win.resize(600, 350)
        pop_curve = TrendCurve(curve._title, curve._series, curve._y_label)
        # 同步历史数据（含时间轴）
        pop_curve._times.extend(curve._times)
        for name, buf in curve._buffers.items():
            pop_curve._buffers[name].extend(buf)
        if curve._times:
            pop_curve._t0 = curve._t0
        # 立即刷新一次
        if hasattr(pop_curve, '_curves'):
            ts = list(pop_curve._times)
            for name, c in pop_curve._curves.items():
                c.setData(ts, list(pop_curve._buffers[name]))

        def _sync(values, pc=pop_curve):
            pc.append(values)

        curve.add_popout_callback(_sync)
        win.destroyed.connect(lambda: curve._popout_callbacks.remove(_sync)
                              if _sync in curve._popout_callbacks else None)
        lv = QVBoxLayout(win)
        lv.addWidget(pop_curve, 1)
        win.show()
        btn._wins = getattr(btn, '_wins', [])
        btn._wins.append(win)

    btn.clicked.connect(_popout)
    v.addWidget(btn, 0, Qt.AlignRight)
    v.addWidget(curve, 1)
    return panel


class _DataItem(QWidget):
    """单个 “标签 + 大字数值” 组合。"""

    def __init__(self, title: str, unit: str = "") -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self._title = QLabel(title)
        self._title.setAlignment(Qt.AlignCenter)
        self._value = QLabel("--")
        self._value.setObjectName("BigValue")
        self._value.setAlignment(Qt.AlignCenter)
        v.addWidget(self._title)
        v.addWidget(self._value)
        self._unit = unit

    def set_value(self, v: float) -> None:
        self._value.setText(f"{v:.2f} {self._unit}".strip())


class _TemperaturePanel(QWidget):
    """将实时温度与额定工作点放在同一语境中。"""

    def __init__(self) -> None:
        super().__init__()
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        self.actual = TemperatureLabel()
        self.actual.setAlignment(Qt.AlignCenter)
        self.actual.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.rated = QLabel("额定工作点：未设置")
        self.delta = QLabel("与额定点偏差：—")
        self.status = QLabel("状态：等待数据")
        self.status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.actual, 0, 0, 1, 2)
        layout.addWidget(self.rated, 1, 0)
        layout.addWidget(self.delta, 1, 1)
        layout.addWidget(self.status, 2, 0, 1, 2)

    def set_values(self, actual_c: float, rated_c: float = 0.0) -> None:
        self.actual.set_temperature(actual_c)
        if rated_c > 0.0:
            self.rated.setText(f"额定工作点：{rated_c:.1f} °C")
            delta = actual_c - rated_c
            self.delta.setText(f"与额定点偏差：{delta:+.1f} °C")
        else:
            self.rated.setText("额定工作点：未设置")
            self.delta.setText("与额定点偏差：—")
        if actual_c >= TEMP_HIGH_THRESHOLD:
            text, color = "高温：请检查负载与散热", "#ff5252"
        elif actual_c >= TEMP_NORMAL_THRESHOLD:
            text, color = "温度偏高", "#ffb74d"
        else:
            text, color = "温度正常", "#69f0ae"
        self.status.setText("状态：" + text)
        self.status.setStyleSheet(f"color: {color}; font-weight: 600;")


class _AngleDial(QWidget):
    """机械角度表盘。

    低速（<SLOW_RPM，10 Hz 采样不混叠）指针直读真实角度，适合对位/
    找零/验编码器；高速时角度数字只是混叠噪声，切换为慢放模式：
    指针按实际转向匀速旋转示意，数字区显示累计圈数（转速积分）。
    双击清零累计圈数。
    """

    SLOW_RPM = 90.0        # 直读/慢放切换阈值 rpm
    SLOW_MO_DPS = 120.0    # 慢放指针角速度 °/s（3 s 一圈）

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(120, 118)
        self._disp = 0.0       # 指针显示角 °
        self._true = 0.0       # 最新真实角 °
        self._rpm = 0.0
        self._revs = 0.0       # 累计圈数（含方向）
        self._last_feed = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)
        self.setToolTip("低速直读角度；高速慢放示意转向，显示累计圈数。双击清零圈数")

    def feed(self, angle_deg: float, rpm: float) -> None:
        now = time.time()
        if self._last_feed is not None:
            self._revs += rpm / 60.0 * min(now - self._last_feed, 1.0)
        self._last_feed = now
        self._true = angle_deg % 360.0
        self._rpm = rpm

    def reset(self) -> None:
        self._disp = self._true = self._rpm = self._revs = 0.0
        self._last_feed = None
        self.update()

    def _tick(self) -> None:
        if abs(self._rpm) < self.SLOW_RPM:
            # 低速直读：沿最短路径平滑跟随真实角
            err = (self._true - self._disp + 180.0) % 360.0 - 180.0
            self._disp = (self._disp + err * 0.35) % 360.0
        else:
            # 高速慢放：按实际转向匀速旋转
            self._disp = (self._disp + math.copysign(
                self.SLOW_MO_DPS * 0.04, self._rpm)) % 360.0
        if self.isVisible():
            self.update()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        self._revs = 0.0
        super().mouseDoubleClickEvent(ev)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        r = min(w, h - 30) / 2.0 - 6
        cx, cy = w / 2.0, r + 8
        # 表盘环 + 刻度（每 30°，0° 在正上方，顺时针）
        qp.setPen(QPen(QColor("#2c3442"), 2))
        qp.drawEllipse(QPointF(cx, cy), r, r)
        for k in range(12):
            a = math.radians(k * 30.0 - 90.0)
            major = (k % 3 == 0)
            r0 = r - (7 if major else 4)
            qp.setPen(QPen(QColor("#55627a"), 2 if major else 1))
            qp.drawLine(QPointF(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                        QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
        # 指针
        slow_mo = abs(self._rpm) >= self.SLOW_RPM
        color = QColor("#ffb74d") if slow_mo else QColor("#4fc3f7")
        a = math.radians(self._disp - 90.0)
        qp.setPen(QPen(color, 2.5))
        qp.drawLine(QPointF(cx, cy),
                    QPointF(cx + (r - 9) * math.cos(a),
                            cy + (r - 9) * math.sin(a)))
        qp.setBrush(color)
        qp.setPen(Qt.NoPen)
        qp.drawEllipse(QPointF(cx, cy), 3, 3)
        # 数字区：低速显示角度，高速显示慢放标记；累计圈数常显
        qp.setPen(QPen(QColor("#dfe6ee")))
        top = "慢放示意" if slow_mo else f"θ = {self._true:.1f}°"
        qp.drawText(0, int(cy + r + 2), w, 14, Qt.AlignHCenter, top)
        qp.setPen(QPen(QColor("#8fa3b8")))
        qp.drawText(0, int(cy + r + 16), w, 14, Qt.AlignHCenter,
                    f"累计 {self._revs:+.1f} 圈")


class _StatItem(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.addWidget(QLabel(title, alignment=Qt.AlignCenter))
        self._max = QLabel("最大：--", alignment=Qt.AlignCenter)
        self._min = QLabel("最小：--", alignment=Qt.AlignCenter)
        v.addWidget(self._max)
        v.addWidget(self._min)
        self._mn = float("inf")
        self._mx = float("-inf")

    def feed(self, value: float) -> None:
        self._mn = min(self._mn, value)
        self._mx = max(self._mx, value)
        self._max.setText(f"最大：{self._mx:.2f}")
        self._min.setText(f"最小：{self._mn:.2f}")


class MonitorPage(QWidget):
    def __init__(self, comm: CommManager, control_page=None) -> None:
        super().__init__()
        self._comm = comm
        self._ctrl = control_page
        self._latest: TelemetryFrame = TelemetryFrame()
        self._last_telemetry_time: float = 0.0

        root = QVBoxLayout(self)

        # ---------- 标题 ----------
        title_row = QHBoxLayout()
        title = QLabel("电机实时监控")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._datasrc_label = QLabel("[ 电机未启动 ]")
        self._datasrc_label.setStyleSheet("color: #90a4ae; font-weight: bold;")
        title_row.addWidget(self._datasrc_label)
        btn_save_all = QPushButton("保存所有波形")
        btn_save_all.clicked.connect(self._save_all_curves)
        title_row.addWidget(btn_save_all)
        btn_report = QPushButton("AI 运行报告")
        btn_report.setToolTip("汇总本次运行的统计数据并附波形截图，由 AI 生成格式化实验报告")
        btn_report.clicked.connect(self._on_ai_report)
        title_row.addWidget(btn_report)
        root.addLayout(title_row)

        # 窄屏下将运行控制拆成独立一行，避免与标题/报告按钮互相挤压。
        control_row = QHBoxLayout()
        self._btn_start = QPushButton("启动"); self._btn_start.setObjectName("PrimaryButton")
        self._btn_stop = QPushButton("停止")
        self._btn_emerg = QPushButton("紧急停止"); self._btn_emerg.setObjectName("EmergencyButton")
        self._btn_sim = QPushButton("启动仿真环境")
        self._btn_quick_sim = QPushButton("快速仿真演示")
        self._btn_quick_sim.setToolTip(
            "仅用于无功率级数字孪生：自动建立仿真环境并启动电机；"
            "正式实验请使用实验页预检。")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_emerg.clicked.connect(self._on_emergency)
        self._btn_sim.clicked.connect(self._on_toggle_sim)
        self._btn_quick_sim.clicked.connect(self._on_quick_sim)
        self._sim_running = False
        # 在线调速：运行中直接改目标转速，无需切换页面
        control_row.addWidget(QLabel("目标转速"))
        self._speed_spin = QSpinBox()
        self._speed_spin.setRange(0, 20000)
        self._speed_spin.setValue(1500)
        self._speed_spin.setSuffix(" rpm")
        control_row.addWidget(self._speed_spin)
        # 目标转速全局唯一入口：控制页启动电机时读取的也是这个框
        if self._ctrl is not None:
            self._ctrl._target_speed = self._speed_spin
        btn_set_speed = QPushButton("设定")
        btn_set_speed.clicked.connect(self._on_set_speed)
        control_row.addWidget(btn_set_speed)
        control_row.addStretch(1)
        for b in (self._btn_sim, self._btn_quick_sim, self._btn_start,
                  self._btn_stop, self._btn_emerg):
            control_row.addWidget(b)
        root.addLayout(control_row)

        # ---------- 实时数据：按物理量分类分框，2 行 × 3 框 ----------
        self._speed_actual = _DataItem("实际", "rpm")
        self._speed_target = _DataItem("给定", "rpm")
        self._current_actual = _DataItem("实际", "A")
        self._current_target = _DataItem("给定", "A")
        self._torque_actual = _DataItem("实际", "Nm")
        self._torque_target = _DataItem("给定", "Nm")
        self._angle_dial = _AngleDial()
        self._electrical_frequency = _DataItem("电频率", "Hz")
        self._electrical_frequency.setToolTip(
            "f_e = |转速| × 极对数 / 60。10 Hz遥测下电角度容易发生"
            "采样混叠（频闪静止），因此主监控显示不混叠的电频率。")
        self._vdc_item = _DataItem("电压", "V")
        self._temperature = _TemperaturePanel()
        self._bus_state = QLabel("状态：--")
        self._bus_state.setAlignment(Qt.AlignCenter)

        def _category_box(title: str, *widgets: QWidget) -> QGroupBox:
            box = QGroupBox(title)
            h = QHBoxLayout(box)
            for w in widgets:
                h.addWidget(w, 1)
            return box

        rt_grid = QGridLayout()
        rt_grid.addWidget(_category_box("转速", self._speed_actual,
                                        self._speed_target), 0, 0)
        rt_grid.addWidget(_category_box("电流", self._current_actual,
                                        self._current_target), 0, 1)
        rt_grid.addWidget(_category_box("直流母线", self._vdc_item,
                                        self._bus_state), 0, 2)
        rt_grid.addWidget(_category_box("转矩", self._torque_actual,
                                        self._torque_target), 1, 0)
        rt_grid.addWidget(_category_box("角度", self._angle_dial,
                                        self._electrical_frequency), 1, 1)

        # ---------- 传感器状态 ----------
        sensor_box = QGroupBox("传感器状态（悬停指标看说明）")
        sensor_grid = QGridLayout(sensor_box)
        self._sensor_source = QLabel("来源：--")
        self._sensor_source.setToolTip("当前提供转子位置的传感器/估算方法")
        self._sensor_quality = QProgressBar()
        self._sensor_quality.setRange(0, 100)
        self._sensor_quality.setValue(100)
        self._sensor_quality.setFormat("质量 %p%")
        self._sensor_quality.setToolTip(
            "质量：这一帧角度数据的置信度/信噪比（0~100%）。\n"
            "反映传感器特性与工况：QEP≈99%、Resolver≈95%、\n"
            "Hall≈70%（60°分辨率，扇区间靠插值）、无传感器法随转速在 40~90% 波动。\n"
            "低于 50% 视为异常——依赖角度的控制（FOC 换相/位置控制）精度会下降。")
        self._sensor_convergence = QLabel("收敛度：--")
        self._sensor_convergence.setToolTip(
            "收敛度：无位置传感器观测器（SMO/EKF/MRAS/HFI）追上真实转子位置的程度（0~1）。\n"
            "1.0 = 已锁定，位置估计可信，可放心闭环；\n"
            "偏低 = 仍在收敛或已发散，此时角度估计不可用于换相。\n"
            "物理传感器（Hall/QEP/Resolver）直接测量、无收敛过程，恒为 1.0。")
        self._sensor_warn = QLabel("低速警告：正常")
        self._sensor_warn.setToolTip(
            "反电动势类无传感器方法（SMO/EKF/MRAS）在低速段信噪比不足、位置估计失效，\n"
            "进入该转速区时此处报警（HFI 例外，可工作到零速）。")
        sensor_grid.addWidget(self._sensor_source, 0, 0)
        sensor_grid.addWidget(self._sensor_quality, 0, 1)
        sensor_grid.addWidget(self._sensor_convergence, 1, 0)
        sensor_grid.addWidget(self._sensor_warn, 1, 1)
        rt_grid.addWidget(_category_box("电机实际温度", self._temperature), 1, 2)
        rt_grid.addWidget(sensor_box, 2, 0, 1, 3)
        root.addLayout(rt_grid)

        # ---------- 统计 ----------
        stat_box = QGroupBox("统计（最大/最小）")
        stat_h = QHBoxLayout(stat_box)
        self._stat_speed = _StatItem("转速")
        self._stat_current = _StatItem("电流")
        self._stat_torque = _StatItem("转矩")
        for w in (self._stat_speed, self._stat_current, self._stat_torque):
            stat_h.addWidget(w)
        root.addWidget(stat_box)

        # ---------- 曲线标签页（同屏只显示一排，高度翻倍）----------
        self._c_speed = TrendCurve("转速 rpm", {"实际": "#4fc3f7", "给定": "#ffb74d"}, y_label="rpm")
        self._c_current = TrendCurve("电流 A", {"实际": "#81c784"}, y_label="A")
        self._c_torque = TrendCurve("转矩 Nm", {"实际": "#ba68c8"}, y_label="Nm")
        self._c_angle = TrendCurve("角度 °", {"估算/实际": "#f48fb1", "原始": "#80cbc4"}, y_label="°")
        self._c_sensor_q = TrendCurve("传感器诊断 (0-1)", {"质量": "#ffcc80", "收敛度": "#ce93d8"}, y_label="")

        trend_tab = QWidget()
        curve_h = QHBoxLayout(trend_tab)
        curve_h.addWidget(_make_curve_panel(self._c_speed, "转速 rpm"))
        curve_h.addWidget(_make_curve_panel(self._c_current, "电流 A"))
        curve_h.addWidget(_make_curve_panel(self._c_torque, "转矩 Nm"))

        sensor_tab = QWidget()
        sensor_curve_h = QHBoxLayout(sensor_tab)
        sensor_curve_h.addWidget(_make_curve_panel(self._c_angle, "角度 °"))
        sensor_curve_h.addWidget(_make_curve_panel(self._c_sensor_q, "传感器诊断"))

        tabs = QTabWidget()
        tabs.setObjectName("CurveTabs")
        tabs.addTab(trend_tab, "📈 趋势曲线（最近 100 点）")
        tabs.addTab(sensor_tab, "🧭 传感器波形")
        root.addWidget(tabs, 1)

        # ---------- 连接信号 ----------
        comm.telemetryReceived.connect(self._on_telemetry)

        # ---------- 刷新定时器 ----------
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(MONITOR_REFRESH_MS)


    # ---- slots ----
    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        self._last_telemetry_time = datetime.datetime.now().timestamp()
        if (not self._comm.is_sim_running() and not self._comm.is_connected() and
                abs(frame.speed_actual) < 1e-9 and abs(frame.angle_actual) < 1e-9):
            self._angle_dial.reset()

    def _refresh(self) -> None:
        import time
        idle = (time.time() - self._last_telemetry_time) > 1.0
        if idle:
            self._refresh_datasource_label("idle")
            return
        f = self._latest
        self._speed_actual.set_value(f.speed_actual)
        self._speed_target.set_value(f.speed_target)
        self._current_actual.set_value(f.current_actual)
        self._current_target.set_value(f.current_target)
        self._torque_actual.set_value(f.torque_actual)
        self._torque_target.set_value(f.torque_target)
        self._angle_dial.feed(f.angle_actual, f.speed_actual)
        pole_pairs = (self._ctrl._pole_pairs.value()
                      if self._ctrl is not None else 1)
        self._electrical_frequency.set_value(
            abs(f.speed_actual) * pole_pairs / 60.0)
        self._vdc_item.set_value(f.vdc)
        rated_temperature = (self._ctrl._rated_temperature.value()
                             if self._ctrl is not None else 0.0)
        self._temperature.set_values(f.temperature, rated_temperature)
        state_text, style = {
            "brake": ("回馈泵升\n制动斩波中", "color: #ffb74d; font-weight: bold;"),
            "uv": ("欠压告警", "color: #ffd740; font-weight: bold;"),
            "ov": ("过压跳闸\n（已封管）", "color: #ff5252; font-weight: bold;"),
        }.get(f.bus_state, ("状态：正常", "color: #69f0ae;"))
        self._bus_state.setText(state_text)
        self._bus_state.setStyleSheet(style)

        self._sensor_source.setText(f"来源：{f.sensor_source or '--'}")
        self._sensor_quality.setValue(max(0, min(100, int(f.sensor_quality * 100))))
        self._sensor_convergence.setText(f"收敛度：{f.convergence:.2f}")
        if f.low_speed_warn:
            self._sensor_warn.setText("低速警告：不可用")
            self._sensor_warn.setStyleSheet("color: #ff5252; font-weight: bold;")
        else:
            self._sensor_warn.setText("低速警告：正常")
            self._sensor_warn.setStyleSheet("")

        self._stat_speed.feed(f.speed_actual)
        self._stat_current.feed(f.current_actual)
        self._stat_torque.feed(f.torque_actual)

        self._c_speed.append({"实际": f.speed_actual, "给定": f.speed_target})
        self._c_current.append({"实际": f.current_actual})
        self._c_torque.append({"实际": f.torque_actual})

        raw_norm = self._normalize_angle_raw(f.sensor_source, f.angle_raw)
        self._c_angle.append({"估算/实际": f.angle_actual, "原始": raw_norm})
        self._c_sensor_q.append({"质量": f.sensor_quality, "收敛度": f.convergence})
        self._refresh_datasource_label(getattr(f, "data_source", "sim"))

    @staticmethod
    def _normalize_angle_raw(source: str, raw: float) -> float:
        if "Hall" in source or "霍尔" in source:
            return raw * 60.0
        if "QEP" in source or "编码器" in source:
            return (raw % 2500.0) / 2500.0 * 360.0
        return raw

    def _refresh_datasource_label(self, source: str) -> None:
        if source == "idle":
            self._datasrc_label.setText("[ 电机未启动 ]")
            self._datasrc_label.setStyleSheet("color: #90a4ae; font-weight: bold;")
        elif source == "real":
            self._datasrc_label.setText("[ 真机数据 ]")
            self._datasrc_label.setStyleSheet("color: #69f0ae; font-weight: bold;")
        elif source == "real_partial":
            self._datasrc_label.setText("[ 真机数据（部分）]")
            self._datasrc_label.setStyleSheet("color: #ffd740; font-weight: bold;")
        else:
            self._datasrc_label.setText("[ 仿真数据 ]")
            self._datasrc_label.setStyleSheet("color: #ff8a65; font-weight: bold;")

    def render_waveforms_png(self) -> bytes:
        """把全部趋势曲线离屏渲染成一张 PNG，返回字节流。

        未安装 pyqtgraph 或尚无数据时返回 b""。
        """
        try:
            import pyqtgraph as pg
        except ImportError:
            return b""
        curves = [
            (self._c_speed,    "转速"),
            (self._c_current,  "电流"),
            (self._c_torque,   "转矩"),
            (self._c_angle,    "角度"),
            (self._c_sensor_q, "传感器诊断"),
        ]
        if not any(len(src._times) for src, _ in curves):
            return b""
        win = pg.GraphicsLayoutWidget()
        win.setBackground("#10131a")
        win.resize(900, 200 * len(curves))
        for i, (src, name) in enumerate(curves):
            p = win.addPlot(row=i, col=0, title=name)
            p.showGrid(x=True, y=True, alpha=0.3)
            p.setLabel("left", src._y_label)
            p.setLabel("bottom", "时间 (s)")
            ts = list(src._times)
            for sname, color in src._series.items():
                p.plot(ts, list(src._buffers[sname]),
                       pen=pg.mkPen(color=color, width=2), name=sname)
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        pixmap = win.grab()
        win.close()
        from PySide6.QtCore import QBuffer, QIODevice
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pixmap.save(buf, "PNG")
        return bytes(buf.data())

    def _save_all_curves(self) -> None:
        png = self.render_waveforms_png()
        if not png:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "提示", "暂无波形数据（或未安装 pyqtgraph）")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存所有波形",
            f"波形_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            "PNG (*.png)"
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(png)

    def _on_ai_report(self) -> None:
        """汇总运行数据 + 波形截图，交给 AI 生成实验报告。"""
        from widgets.report_dialog import ExperimentReportDialog
        if not self._c_speed._times:
            QMessageBox.information(self, "提示", "暂无运行数据，请先启动仿真或电机。")
            return
        f = self._latest
        duration = (self._c_speed._times[-1] - self._c_speed._times[0]
                    if len(self._c_speed._times) > 1 else 0.0)
        ctx = (
            "实验类型：电机运行实验（监控数据汇总）\n"
            f"实验时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"数据来源：{self._datasrc_label.text()}\n"
            f"记录时长：约 {duration:.0f} s（趋势曲线窗口内）\n\n"
            "── 结束时刻状态 ──\n"
            f"转速：实际 {f.speed_actual:.1f} / 给定 {f.speed_target:.1f} rpm\n"
            f"电流：实际 {f.current_actual:.2f} / 给定 {f.current_target:.2f} A\n"
            f"转矩：实际 {f.torque_actual:.2f} / 给定 {f.torque_target:.2f} Nm\n"
            f"母线电压：{f.vdc:.1f} V（状态 {f.bus_state}）  温度：{f.temperature:.1f} °C\n"
            f"位置传感器：{f.sensor_source or '--'}，质量 {f.sensor_quality:.2f}，"
            f"收敛度 {f.convergence:.2f}\n\n"
            "── 运行统计（本次会话） ──\n"
            f"转速：最大 {self._stat_speed._mx:.1f} / 最小 {self._stat_speed._mn:.1f} rpm\n"
            f"电流：最大 {self._stat_current._mx:.2f} / 最小 {self._stat_current._mn:.2f} A\n"
            f"转矩：最大 {self._stat_torque._mx:.2f} / 最小 {self._stat_torque._mn:.2f} Nm\n"
        )
        png = self.render_waveforms_png()
        images = [("image/png", png)] if png else []
        ExperimentReportDialog("电机运行实验", ctx, images, parent=self).exec()

    def _on_set_speed(self) -> None:
        """把新目标转速下发给下位机/虚拟电机（运行中即时生效）。"""
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            QMessageBox.warning(self, "无法设定", "请先启动仿真或连接通信。")
            return
        state_machine = getattr(self._ctrl, "_state_machine", None)
        if (state_machine is not None and
                state_machine.state.value != "running"):
            QMessageBox.warning(self, "状态不允许设定",
                                "只有状态机处于 RUNNING 时才能修改目标转速。")
            return
        target = float(self._speed_spin.value())
        payload = f"target={target}".encode("utf-8")
        self._comm.send_frame(encode_frame(CMD_START, payload))

    def _on_start(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_start()

    def _on_stop(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_stop()

    def _on_emergency(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_emergency()

    def _on_toggle_sim(self) -> None:
        state_machine = getattr(self._ctrl, "_state_machine", None)
        if not self._sim_running:
            self._comm.start_simulation()
            self._sim_running = True
            if state_machine is not None:
                state_machine.connection_changed(True, "数字孪生已连接")
            self._btn_sim.setText("停止仿真环境")
        else:
            if (state_machine is not None and state_machine.state.value in
                    ("running", "stopping")):
                QMessageBox.warning(self, "不能停止仿真",
                                    "电机仍在运行，请先执行正常停机或紧急停止。")
                return
            self._comm.stop_simulation()
            self._sim_running = False
            if state_machine is not None:
                state_machine.connection_changed(False, "数字孪生已断开")
            self._btn_sim.setText("启动仿真环境")

    def _on_quick_sim(self) -> None:
        """一键启动数字孪生演示，但不伪装成完整实验预检。"""
        if self._comm.is_connected():
            QMessageBox.warning(self, "不可用",
                                "已连接真实设备，快速仿真演示已禁用。")
            return
        state_machine = getattr(self._ctrl, "_state_machine", None)
        if state_machine is not None and state_machine.state.value == "fault_locked":
            QMessageBox.warning(self, "故障锁定",
                                "请先确认并复位故障，不能用演示模式绕过锁定。")
            return
        if not self._sim_running:
            self._on_toggle_sim()
        if state_machine is not None and state_machine.state.value == "connected":
            try:
                state_machine.begin_precheck("快速仿真基础检查")
                if self._ctrl._current_limit.value() <= 0:
                    raise ValueError("电流限幅必须大于0")
                if abs(self._speed_spin.value()) > self._ctrl._max_rpm.value():
                    raise ValueError("目标转速超过电机最高转速")
                state_machine.pass_precheck("快速仿真基础检查通过（非实验预检）")
            except Exception as exc:
                if state_machine.state.value == "precheck":
                    state_machine.fail_precheck(str(exc))
                QMessageBox.warning(self, "快速仿真失败", str(exc))
                return
        if state_machine is None or state_machine.state.value == "ready":
            self._on_start()
