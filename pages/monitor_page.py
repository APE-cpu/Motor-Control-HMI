"""监控页面：实时数据、统计、趋势曲线。"""
import csv
import datetime
import math
import os
import time
from collections import deque
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import CMD_SET_PARAMS, MONITOR_REFRESH_MS
from widgets.trend_curve import TrendCurve
from runtime_paths import writable_path
from widgets.temperature_label import TemperatureLabel
from config.config import TEMP_HIGH_THRESHOLD, TEMP_NORMAL_THRESHOLD

try:
    import numpy as np
    import pyqtgraph as pg
    _MP_PG_OK = True
except Exception:  # pragma: no cover
    _MP_PG_OK = False


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
        pop_curve = TrendCurve(curve._title, curve._series, curve._y_label,
                               curve._buffer_size)
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

        def _sync_batch(samples, interval_s, pc=pop_curve):
            pc.append_batch(samples, interval_s)

        curve.add_popout_callback(_sync)
        curve.add_popout_batch_callback(_sync_batch)

        def _detach_popout_callbacks():
            if _sync in curve._popout_callbacks:
                curve._popout_callbacks.remove(_sync)
            if _sync_batch in curve._popout_batch_callbacks:
                curve._popout_batch_callbacks.remove(_sync_batch)

        win.destroyed.connect(_detach_popout_callbacks)
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

    def set_unavailable(self) -> None:
        self.actual.setText("不可用")
        self.rated.setText("额定工作点：未设置")
        self.delta.setText("与额定点偏差：—")
        self.status.setText("状态：温度采样电路未供电")
        self.status.setStyleSheet("color: #ffd740; font-weight: 600;")


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
        self._high_rate_samples = deque(maxlen=5000)
        self._last_high_angle_time = 0.0
        self._last_telemetry_time: float = 0.0
        self._latest_vbus_v = 0.0
        self._latest_rls: dict = {}

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
        self._smooth_chk = QCheckBox("显示平滑")
        self._smooth_chk.setToolTip(
            "只平滑显示波形，不影响控制/导出/统计。\n"
            "相电流用轻平滑保留正弦；电角度锯齿不平滑。")
        self._smooth_chk.toggled.connect(self._on_smooth_toggled)
        title_row.addWidget(self._smooth_chk)
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
        self._btn_sim = QPushButton("启动数字孪生")
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
        comm.statusChanged.connect(self._sync_sim_button)
        # 在线调速：运行中直接改目标转速，无需切换页面
        control_row.addWidget(QLabel("目标转速"))
        self._speed_spin = QSpinBox()
        self._speed_spin.setRange(0, 20000)
        self._speed_spin.setValue(1000)
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
        self._current_actual = _DataItem("实际 Iq", "A")
        self._current_target = _DataItem("给定 Iq", "A")
        self._torque_actual = _DataItem("估算", "Nm")
        self._torque_target = _DataItem("估算给定", "Nm")
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
        rt_grid.addWidget(_category_box("估算电磁转矩", self._torque_actual,
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
        self._c_current = TrendCurve(
            "q轴电流 Iq A", {"实际 Iq": "#81c784", "给定 Iq": "#ffb74d"},
            y_label="A", buffer_size=5000)
        self._c_phase_current = TrendCurve(
            "相电流 Ia / Ib A", {"Ia": "#4fc3f7", "Ib": "#f48fb1"},
            y_label="A", buffer_size=5000)
        # 只显示最近约 80ms（1000rpm/4对极 时约 5 个电周期），正弦看得清而不是
        # 几千点糊成一片。缓冲仍是 5000 点，导出/统计/THD 不受影响；滚轮缩放后
        # 恢复此视窗由双击触发。带载或变速时电周期数随速度变化，属正常。
        self._c_phase_current.set_view_window(0.08)
        self._c_torque = TrendCurve("转矩 Nm", {"实际": "#ba68c8"}, y_label="Nm")
        self._c_angle = TrendCurve(
            "高速电角度", {"高速电角度": "#f48fb1"}, y_label="°",
            buffer_size=5000)
        self._c_sensor_q = TrendCurve("传感器诊断 (0-1)", {"质量": "#ffcc80", "收敛度": "#ce93d8"}, y_label="")
        # 施加电压 Vd/Vq（PI 输出，MCSDK 内部码值）——卡尔曼建模的控制输入 u。
        self._c_voltage = TrendCurve(
            "施加电压 Vd/Vq (码值)", {"Vd": "#80cbc4", "Vq": "#ffab91"},
            y_label="digit", buffer_size=5000)
        # F3 在线 ARX/RLS 辨识：反解出的物理参数。Ld/Lq 应收敛到 ~0.66mH，
        # a1 应收敛到 ~0.944；R 由 a1 反解、对电流噪声敏感，需带载提 SNR。
        self._c_rls_L = TrendCurve(
            "辨识电感 Ld/Lq (mH)", {"Ld": "#4db6ac", "Lq": "#ff8a65"},
            y_label="mH", buffer_size=2000)
        self._c_rls_a1 = TrendCurve(
            "ARX a1 系数 (→0.944)", {"a1_d": "#4fc3f7", "a1_q": "#ba68c8"},
            y_label="", buffer_size=2000)
        self._c_rls_R = TrendCurve(
            "辨识电阻 Rd/Rq (Ω)", {"Rd": "#81c784", "Rq": "#f06292"},
            y_label="Ω", buffer_size=2000)

        trend_tab = QWidget()
        curve_h = QHBoxLayout(trend_tab)
        curve_h.addWidget(_make_curve_panel(self._c_speed, "转速 rpm"))
        curve_h.addWidget(_make_curve_panel(self._c_current, "q轴电流 Iq A"))
        curve_h.addWidget(_make_curve_panel(
            self._c_phase_current, "相电流 Ia / Ib A"))

        sensor_tab = QWidget()
        sensor_curve_h = QHBoxLayout(sensor_tab)
        sensor_curve_h.addWidget(_make_curve_panel(self._c_angle, "角度 °"))
        sensor_curve_h.addWidget(_make_curve_panel(self._c_sensor_q, "传感器诊断"))

        power_tab = QWidget()
        power_curve_h = QHBoxLayout(power_tab)
        power_curve_h.addWidget(_make_curve_panel(self._c_torque, "转矩 Nm"))
        power_curve_h.addWidget(_make_curve_panel(self._c_voltage, "施加电压 Vd/Vq"))

        rls_tab = QWidget()
        rls_curve_h = QHBoxLayout(rls_tab)
        rls_curve_h.addWidget(_make_curve_panel(self._c_rls_a1, "ARX a1 (→0.944)"))
        rls_curve_h.addWidget(_make_curve_panel(self._c_rls_L, "辨识电感 (mH)"))
        rls_curve_h.addWidget(_make_curve_panel(self._c_rls_R, "辨识电阻 (Ω)"))

        burst_tab = self._build_burst_tab()

        tabs = QTabWidget()
        tabs.setObjectName("CurveTabs")
        tabs.addTab(trend_tab, "📈 趋势曲线（最近 1000 点）")
        tabs.addTab(sensor_tab, "🧭 传感器波形")
        tabs.addTab(power_tab, "⚡ 转矩与电压")
        tabs.addTab(rls_tab, "🔬 在线辨识 (RLS)")
        tabs.addTab(burst_tab, "📸 抓取波形 (16kHz)")
        root.addWidget(tabs, 1)

        # ---------- 连接信号 ----------
        comm.telemetryReceived.connect(self._on_telemetry)
        comm.highRateTelemetryReceived.connect(self._on_high_rate_telemetry)
        comm.highRateTelemetryBatchReceived.connect(
            self._on_high_rate_telemetry_batch)
        comm.rlsCoeffReceived.connect(self._on_rls_coeff)
        comm.burstReceived.connect(self._on_burst)

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

    def _on_high_rate_telemetry(self, sample: dict) -> None:
        self._high_rate_samples.append({
            "angle_deg": float(sample["angle_deg"]),
            "iq_a": float(sample["iq_a"]),
            "iqref_a": float(sample["iqref_a"]),
            "ia_a": float(sample.get("ia_a", 0.0)),
            "ib_a": float(sample.get("ib_a", 0.0)),
            # 施加电压原始码值（旧固件缺省 0）+ 母线电压，供离线卡尔曼建模。
            "vd_raw": float(sample.get("vd_raw", 0.0)),
            "vq_raw": float(sample.get("vq_raw", 0.0)),
            "vbus_v": float(sample.get("vbus_v", 0.0)),
            "rate_hz": int(sample.get("rate_hz", 200)),
        })
        self._latest_vbus_v = float(sample.get("vbus_v", self._latest_vbus_v))
        self._last_high_angle_time = time.time()

    def _on_high_rate_telemetry_batch(self, samples: list[dict]) -> None:
        for sample in samples:
            self._on_high_rate_telemetry(sample)

    def _on_rls_coeff(self, sample: dict) -> None:
        """F3 在线辨识系数（50Hz）。只在有激励（updates 增长、b>0）时才画物理量，
        否则温启动的 b≈0 会让 L 反解成 inf。原始 θ 缓存供导出。"""
        self._latest_rls = sample
        if int(sample.get("updates", 0)) <= 0:
            return

        def _finite(x, lo, hi):
            return isinstance(x, float) and x == x and lo <= x <= hi

        # a1 放宽到 ±8：匀速无激励时 θ 不收敛会飘，但仍应显示以证明辨识在跑
        # （空面板反而分不清"没激励"还是"F3没到"）。收敛时 a1 应趋近 0.944。
        a1_d, a1_q = sample.get("a1_d"), sample.get("a1_q")
        if _finite(a1_d, -8.0, 8.0) and _finite(a1_q, -8.0, 8.0):
            self._c_rls_a1.append({"a1_d": a1_d, "a1_q": a1_q})
        ld, lq = sample.get("ld_mh"), sample.get("lq_mh")
        if _finite(ld, 0.0, 10.0) and _finite(lq, 0.0, 10.0):
            self._c_rls_L.append({"Ld": ld, "Lq": lq})
        rd, rq = sample.get("rd_ohm"), sample.get("rq_ohm")
        if _finite(rd, -5.0, 20.0) and _finite(rq, -5.0, 20.0):
            self._c_rls_R.append({"Rd": rd, "Rq": rq})

    # ------- 突发抓取波形 (16kHz) -------
    def _build_burst_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout(tab)
        bar = QHBoxLayout()
        self._btn_burst = QPushButton("📸 抓取一帧波形（16kHz 真实）")
        self._btn_burst.setObjectName("PrimaryButton")
        self._btn_burst.clicked.connect(self._on_burst_click)
        self._burst_status = QLabel("电机运行中点“抓取”，下位机录 128ms 原始 16kHz "
                                    "电流再慢速回传（不占心跳）。")
        self._burst_status.setStyleSheet("color:#90a4ae;")
        bar.addWidget(self._btn_burst)
        bar.addWidget(self._burst_status, 1)
        v.addLayout(bar)
        if _MP_PG_OK:
            self._burst_raw = pg.PlotWidget(title="真实波形（16kHz 原始）")
            self._burst_avg = pg.PlotWidget(title="同步平均单周期（按电角度折叠去噪）")
            for plot, xlab in ((self._burst_raw, "时间 (ms)"),
                               (self._burst_avg, "电角度 (°)")):
                plot.setBackground("#10131a")
                plot.showGrid(x=True, y=True, alpha=0.3)
                plot.addLegend()
                plot.setLabel("left", "相电流 (A)")
                plot.setLabel("bottom", xlab)
            self._burst_raw_ia = self._burst_raw.plot([], [], pen=pg.mkPen("#4fc3f7", width=1), name="Ia")
            self._burst_raw_ib = self._burst_raw.plot([], [], pen=pg.mkPen("#f48fb1", width=1), name="Ib")
            self._burst_avg_ia = self._burst_avg.plot([], [], pen=pg.mkPen("#4fc3f7", width=2), name="Ia")
            self._burst_avg_ib = self._burst_avg.plot([], [], pen=pg.mkPen("#f48fb1", width=2), name="Ib")
            plots = QHBoxLayout()
            plots.addWidget(self._burst_raw)
            plots.addWidget(self._burst_avg)
            v.addLayout(plots, 1)
        else:
            v.addWidget(QLabel("[未安装 pyqtgraph，无法绘制抓取波形]"))
        return tab

    def _on_burst_click(self) -> None:
        if not self._comm.is_connected():
            self._burst_status.setText("未连接，无法抓取。")
            return
        ok = self._comm.send_burst_trigger()
        self._burst_status.setText(
            "已请求抓取，等待回传…（需电机运行中；约 0.3s 完成）" if ok
            else "抓取请求发送失败。")

    def _on_burst(self, data: dict) -> None:
        if not _MP_PG_OK:
            return
        n = int(data.get("n", 0))
        ia = np.asarray(data.get("ia", []), dtype=float) * 0.000629   # 码值→A
        ib = np.asarray(data.get("ib", []), dtype=float) * 0.000629
        ang = np.asarray(data.get("ang", []), dtype=float)            # 0..65535 = 0..360°
        if n == 0 or ia.size == 0:
            self._burst_status.setText("抓取回传为空。")
            return
        t_ms = np.arange(ia.size) / 16.0        # 16kHz → 1/16 ms 每点
        self._burst_raw_ia.setData(t_ms, ia)
        self._burst_raw_ib.setData(t_ms, ib)
        # 同步平均：按电角度折叠到 360 个格子，信号叠加、噪声相消
        bins = 360
        idx = np.clip((ang * bins / 65536.0).astype(int), 0, bins - 1)
        deg = np.arange(bins) + 0.5
        sum_ia = np.bincount(idx, weights=ia, minlength=bins)
        sum_ib = np.bincount(idx, weights=ib, minlength=bins)
        cnt = np.bincount(idx, minlength=bins).astype(float)
        valid = cnt > 0
        avg_ia = np.where(valid, sum_ia / np.maximum(cnt, 1), np.nan)
        avg_ib = np.where(valid, sum_ib / np.maximum(cnt, 1), np.nan)
        self._burst_avg_ia.setData(deg[valid], avg_ia[valid])
        self._burst_avg_ib.setData(deg[valid], avg_ib[valid])
        periods = int(np.sum(np.abs(np.diff(ang)) > 40000))   # 角度回卷次数≈电周期数
        self._burst_status.setText(
            f"已抓取 {n} 点（128ms），约 {periods} 个电周期；"
            f"左=真实波形，右=同步平均去噪单周期。")

    def _on_smooth_toggled(self, checked: bool) -> None:
        """显示平滑开关：每条曲线用合适窗口。仅平滑显示，不动缓冲/导出/统计。
        相电流用轻平滑保留正弦基波；电角度是锯齿波，不平滑。"""
        on = bool(checked)
        windows = {
            self._c_speed: 15,
            self._c_current: 15,
            self._c_phase_current: 3,   # 轻平滑，低速正弦基波几乎不衰减
            self._c_torque: 15,
            self._c_angle: 1,           # 0~360 锯齿，平滑会把回卷抹成斜坡
            self._c_sensor_q: 1,
            self._c_voltage: 9,
            self._c_rls_a1: 1,          # 已是慢变量，无需平滑
            self._c_rls_L: 1,
            self._c_rls_R: 5,           # R 反解噪声大，轻平滑便于读趋势
        }
        for curve, n in windows.items():
            curve.set_smoothing(n if on else 1)

    def _refresh(self) -> None:
        import time
        idle = (time.time() - self._last_telemetry_time) > 1.0
        if idle:
            self._refresh_datasource_label("idle")
            return
        f = self._latest
        high_rate = list(self._high_rate_samples)
        self._high_rate_samples.clear()
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
        if f.bus_state == "uv" or f.vdc < 1.0:
            self._temperature.set_unavailable()
        else:
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

        # 停机后设备遥测全部归零（固件在非 RUN 状态只发零值），此时继续追加
        # 只会让曲线滚动平直的零线，并在约一分钟内把刚跑完的实验数据挤出
        # 缓冲区。冻结曲线、保留数据，方便停机后缩放查看波形。
        curves_active = bool(high_rate) or any(
            abs(value) > 1e-9 for value in (
                f.speed_actual, f.speed_target, f.current_actual,
                f.current_target, f.torque_actual))
        # 新一次运行开始：解除上次停机后用户缩放造成的时间轴定格，
        # 否则新数据画在可视窗口之外，看起来像"曲线不再更新"。
        if curves_active and not getattr(self, "_curves_were_active", False):
            for curve in (self._c_speed, self._c_current,
                          self._c_phase_current, self._c_torque,
                          self._c_angle, self._c_sensor_q, self._c_voltage,
                          self._c_rls_a1, self._c_rls_L, self._c_rls_R):
                curve.resume_follow()
        self._curves_were_active = curves_active
        if curves_active:
            self._c_speed.append({"实际": f.speed_actual, "给定": f.speed_target})
            if high_rate:
                interval_s = 1.0 / max(high_rate[-1]["rate_hz"], 1)
                self._c_current.append_batch(
                    [{"实际 Iq": sample["iq_a"],
                      "给定 Iq": sample["iqref_a"]}
                     for sample in high_rate], interval_s)
                self._c_phase_current.append_batch(
                    [{"Ia": sample["ia_a"], "Ib": sample["ib_a"]}
                     for sample in high_rate], interval_s)
                self._c_voltage.append_batch(
                    [{"Vd": sample["vd_raw"], "Vq": sample["vq_raw"]}
                     for sample in high_rate], interval_s)
            else:
                self._c_current.append(
                    {"实际 Iq": f.current_actual, "给定 Iq": f.current_target})
            self._c_torque.append({"实际": f.torque_actual})

            if high_rate:
                self._c_angle.append_batch(
                    [{"高速电角度": sample["angle_deg"]}
                     for sample in high_rate], interval_s)
            elif time.time() - self._last_high_angle_time > 1.0:
                self._c_angle.append({"高速电角度": f.angle_actual})
            self._c_sensor_q.append(
                {"质量": f.sensor_quality, "收敛度": f.convergence})
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
            (self._c_current,  "q轴电流 Iq"),
            (self._c_phase_current, "相电流 Ia / Ib"),
            (self._c_torque,   "转矩"),
            (self._c_angle,    "角度"),
            (self._c_sensor_q, "传感器诊断"),
            (self._c_voltage,  "施加电压 Vd/Vq"),
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
        from PySide6.QtWidgets import QMessageBox
        png = self.render_waveforms_png()
        if not png:
            QMessageBox.warning(self, "提示", "暂无波形数据（或未安装 pyqtgraph）")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存所有波形",
            str(writable_path(
                "波形记录",
                f"波形_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")),
            "PNG (*.png)"
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(png)
        csv_path = os.path.splitext(path)[0] + ".csv"
        self._write_curves_csv(csv_path)
        QMessageBox.information(
            self, "保存成功",
            f"已保存波形图和原始数据：\n{path}\n{csv_path}")

    def _write_curves_csv(self, path: str) -> None:
        """按原始采样时间导出所有曲线，不对不同采样率做伪对齐。"""
        curves = [
            (self._c_speed, "speed"),
            (self._c_current, "iq_current"),
            (self._c_phase_current, "phase_current"),
            (self._c_torque, "torque"),
            (self._c_angle, "electrical_angle"),
            (self._c_sensor_q, "sensor_diagnostics"),
            (self._c_voltage, "applied_voltage"),
            (self._c_rls_a1, "rls_a1"),
            (self._c_rls_L, "rls_inductance_mh"),
            (self._c_rls_R, "rls_resistance_ohm"),
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["channel", "time_s", "series", "value"])
            for curve, channel in curves:
                times = list(curve._times)
                for series, values in curve._buffers.items():
                    samples = list(values)
                    sample_times = times[-len(samples):] if samples else []
                    for timestamp, value in zip(sample_times, samples):
                        writer.writerow([
                            channel, f"{timestamp:.6f}", series,
                            f"{float(value):.9g}",
                        ])

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
        """在 READY 预设目标，或在 RUNNING 在线修改目标。"""
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            QMessageBox.warning(self, "无法设定", "请先启动仿真或连接通信。")
            return
        state_machine = getattr(self._ctrl, "_state_machine", None)
        if (state_machine is not None and
                state_machine.state.value not in ("ready", "running")):
            QMessageBox.warning(self, "状态不允许设定",
                                "只有状态机处于 READY 或 RUNNING 时才能设定目标转速。")
            return
        target = float(self._speed_spin.value())
        max_rpm = float(getattr(getattr(self._ctrl, "_max_rpm", None),
                                "value", lambda: 0)())
        if max_rpm > 0 and abs(target) > max_rpm:
            QMessageBox.warning(
                self, "参数越界",
                f"在线目标 {target:.0f} rpm 超过最高转速 {max_rpm:.0f} rpm。")
            return
        payload = f"target={target}".encode("utf-8")
        if not self._comm.send_frame(encode_frame(CMD_SET_PARAMS, payload)):
            status = self._comm.protocol_status()
            if not (status.get("mode") == "negotiated-v2" and
                    status.get("pending_ack", 0) > 0):
                QMessageBox.warning(self, "设定未发送", "设备未接受在线调速命令。")

    def _sync_sim_button(self, _connected: bool, _message: str) -> None:
        self._sim_running = self._comm.is_sim_running()
        self._btn_sim.setText("停止数字孪生" if self._sim_running else "启动数字孪生")

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
        self._sim_running = self._comm.is_sim_running()
        if not self._sim_running:
            if self._comm.is_connected():
                QMessageBox.warning(
                    self, "不可用",
                    "真实设备已连接。请先断开真机通信，再启动数字孪生。")
                return
            self._comm.start_simulation()
            self._sim_running = True
            if state_machine is not None:
                state_machine.connection_changed(True, "数字孪生已连接")
            self._btn_sim.setText("停止数字孪生")
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
            self._btn_sim.setText("启动数字孪生")

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
