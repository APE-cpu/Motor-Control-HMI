"""监控页面：实时数据、统计、趋势曲线。"""
import csv
import datetime
import math
import os
import time
from collections import deque
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import CMD_SET_PARAMS, MONITOR_PLOT_REFRESH_MS
from widgets.trend_curve import TrendCurve
from waveform_storage import category_for_control_mode, create_waveform_record_dir
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
    # pyqtgraph 默认会申请约 600x480 的显示区。横纵两个方向都必须
    # 忽略它的 sizeHint，只使用父布局已经分配的空间；否则曲线会
    # 反过来撑大整个监控页。
    panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
    curve.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
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
        pop_curve.set_source_processing(
            curve._source_processing,
            curve._sample_rate_hz if curve._sample_rate_hz > 0 else None)
        pop_curve.set_smoothing(curve._smooth_n)
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

        def _sync_columns(columns, interval_s, pc=pop_curve):
            pc.append_columns(columns, interval_s)

        curve.add_popout_callback(_sync)
        curve.add_popout_batch_callback(_sync_batch)
        curve.add_popout_columns_callback(_sync_columns)

        def _detach_popout_callbacks():
            if _sync in curve._popout_callbacks:
                curve._popout_callbacks.remove(_sync)
            if _sync_batch in curve._popout_batch_callbacks:
                curve._popout_batch_callbacks.remove(_sync_batch)
            if _sync_columns in curve._popout_columns_callbacks:
                curve._popout_columns_callbacks.remove(_sync_columns)

        win.destroyed.connect(_detach_popout_callbacks)
        lv = QVBoxLayout(win)
        lv.addWidget(pop_curve, 1)
        win.show()
        btn._wins = getattr(btn, '_wins', [])
        btn._wins.append(win)

    btn.clicked.connect(_popout)
    curve.add_header_widget(btn)
    v.addWidget(curve, 1)
    return panel


class _WaveformFilterDialog(QDialog):
    """逐曲线管理上位机显示滤波；原始缓冲始终不改。"""

    def __init__(self, specs: list[tuple[str, TrendCurve, int]],
                 changed_callback, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("波形滤波控制")
        self.resize(660, 560)
        self._specs = specs
        self._changed_callback = changed_callback
        self._rows: dict[TrendCurve, tuple[QCheckBox, QSpinBox, QLabel]] = {}
        root = QVBoxLayout(self)
        note = QLabel(
            "滤波只用于上位机波形显示，不改动原始缓冲、CSV、FFT和电机控制。"
            "16 kHz 下16点箱式平均等效窗长为1 ms。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#90a4ae;")
        root.addWidget(note)
        grid = QGridLayout()
        grid.addWidget(QLabel("波形"), 0, 0)
        grid.addWidget(QLabel("启用"), 0, 1)
        grid.addWidget(QLabel("箱式平均点数"), 0, 2)
        grid.addWidget(QLabel("当前等效窗长"), 0, 3)
        for row, (name, curve, default_points) in enumerate(specs, 1):
            enabled = QCheckBox()
            points = QSpinBox()
            points.setRange(2, 257)
            points.setValue(max(2, int(default_points)))
            detail = QLabel("")
            detail.setStyleSheet("color:#80cbc4;")
            enabled.toggled.connect(
                lambda _checked, c=curve: self._apply_curve(c))
            points.valueChanged.connect(
                lambda _value, c=curve: self._apply_curve(c))
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(enabled, row, 1, Qt.AlignCenter)
            grid.addWidget(points, row, 2)
            grid.addWidget(detail, row, 3)
            self._rows[curve] = (enabled, points, detail)
        root.addLayout(grid)
        actions = QHBoxLayout()
        raw_btn = QPushButton("全部关闭（原始显示）")
        raw_btn.clicked.connect(self._disable_all)
        current_btn = QPushButton("启用电流/电压16点平均")
        current_btn.clicked.connect(self._current_preset)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        actions.addWidget(raw_btn)
        actions.addWidget(current_btn)
        actions.addStretch(1)
        actions.addWidget(close_btn)
        root.addLayout(actions)
        self.refresh_details()

    def _apply_curve(self, curve: TrendCurve) -> None:
        enabled, points, _detail = self._rows[curve]
        curve.set_smoothing(points.value() if enabled.isChecked() else 1)
        self.refresh_details()
        self._changed_callback()

    def _disable_all(self) -> None:
        for curve, (enabled, _points, _detail) in self._rows.items():
            enabled.blockSignals(True)
            enabled.setChecked(False)
            enabled.blockSignals(False)
            curve.set_smoothing(1)
        self.refresh_details()
        self._changed_callback()

    def _current_preset(self) -> None:
        names = {"高速Iq", "相电流Ia/Ib", "施加电压Vd/Vq"}
        for name, curve, _default in self._specs:
            enabled, points, _detail = self._rows[curve]
            enabled.blockSignals(True)
            points.blockSignals(True)
            enabled.setChecked(name in names)
            if name in names:
                points.setValue(16)
            points.blockSignals(False)
            enabled.blockSignals(False)
            curve.set_smoothing(points.value() if enabled.isChecked() else 1)
        self.refresh_details()
        self._changed_callback()

    def refresh_details(self) -> None:
        for curve, (enabled, points, detail) in self._rows.items():
            points.setEnabled(enabled.isChecked())
            rate = float(curve._sample_rate_hz)
            if not enabled.isChecked():
                detail.setText("未滤波")
            elif rate > 0.0:
                detail.setText(
                    f"{points.value() / rate * 1000.0:.3g} ms · 仅显示")
            else:
                detail.setText(f"{points.value()}点 · 采样率非固定")

    def enabled_count(self) -> int:
        return sum(enabled.isChecked()
                   for enabled, _points, _detail in self._rows.values())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self.refresh_details()
        super().showEvent(event)


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
    """位置三环的相对机械角表盘。

    角度来自下位机 ``position_actual_deg``：每次位置三环 START 时，
    下位机捕获当前位置为 0°，之后按 QEP 增量累计机械角。这里不再用
    电角度或转速积分估算圈数，避免把历史运行和采样误差带入位置读数。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(120, 118)
        self._disp = 0.0          # 一圈内的指针角 °
        self._true = 0.0          # 兼容既有内部字段：一圈内机械角 °
        self._position_deg = 0.0  # 相对启动零点的连续机械角 °
        self._revs = 0.0          # QEP机械角直接换算的累计圈数
        self._valid = False
        self.setToolTip(
            "机械角度 θm：位置三环每次启动时，把当时轴位置作为本次0°；"
            "当前为增量编码器，未执行机械原点回零，因此没有持久绝对零点。")

    def feed(self, position_deg: float) -> None:
        self._position_deg = float(position_deg)
        self._true = self._position_deg % 360.0
        self._disp = self._true
        self._revs = self._position_deg / 360.0
        self._valid = True
        self.update()

    def reset(self) -> None:
        self._disp = self._true = self._position_deg = self._revs = 0.0
        self._valid = False
        self.update()

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
        # 指针：只表示一圈内机械位置；数字区保留连续多圈角度。
        color = QColor("#4fc3f7") if self._valid else QColor("#55627a")
        a = math.radians(self._disp - 90.0)
        qp.setPen(QPen(color, 2.5))
        qp.drawLine(QPointF(cx, cy),
                    QPointF(cx + (r - 9) * math.cos(a),
                            cy + (r - 9) * math.sin(a)))
        qp.setBrush(color)
        qp.setPen(Qt.NoPen)
        qp.drawEllipse(QPointF(cx, cy), 3, 3)
        # 数字区：连续机械角与机械圈数都直接来自位置反馈，不做转速积分。
        qp.setPen(QPen(QColor("#dfe6ee") if self._valid else QColor("#8fa3b8")))
        top = (f"θm = {self._position_deg:+.1f}°"
               if self._valid else "θm = 0.0°")
        qp.drawText(0, int(cy + r + 2), w, 14, Qt.AlignHCenter, top)
        qp.setPen(QPen(QColor("#8fa3b8")))
        bottom = (f"零点=启动点 · {self._revs:+.2f}圈"
                  if self._valid else "绝对零点：未标定")
        qp.drawText(0, int(cy + r + 16), w, 14, Qt.AlignHCenter, bottom)


class _EnergyOrb(QWidget):
    """监控页背景层：电机剖面三态素材交叉淡入并叠加动态光效。"""

    TICK_MS = 40
    HUB_REL = (0.621, 0.50)
    IMG_ASPECT = 795.0 / 941.0
    _IMG = {
        "stopped": "motor_bg_stopped.png",
        "running": "motor_bg_running.png",
        "fault": "motor_bg_fault.png",
    }
    _OVERLAY = {
        "running": QColor("#4de8cf"),
        "fault": QColor("#ff7043"),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._state = "stopped"
        self._rpm = 0.0
        self._fade = {"stopped": 1.0, "running": 0.0, "fault": 0.0}
        self._angle = 0.0
        self._t = 0.0
        self._pixmaps = {}
        from PySide6.QtGui import QPixmap
        from runtime_paths import resource_path
        for key, name in self._IMG.items():
            pm = QPixmap(str(resource_path("assets", name)))
            if not pm.isNull():
                self._pixmaps[key] = pm
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.TICK_MS)

    def set_state(self, state: str, rpm: float = 0.0) -> None:
        if state not in self._IMG:
            state = "stopped"
        self._state = state
        self._rpm = rpm

    def _tick(self) -> None:
        dt = self.TICK_MS / 1000.0
        self._t += dt
        # 三态素材平滑交叉淡入淡出。
        for key in self._fade:
            target = 1.0 if key == self._state else 0.0
            self._fade[key] += (target - self._fade[key]) * 0.10
        if self._state == "running":
            # 流光方向跟随转向，速度随转速由 90°/s 增至约 200°/s。
            dps = 90.0 + min(abs(self._rpm), 3000.0) / 3000.0 * 110.0
            self._angle = (self._angle + math.copysign(
                dps * dt, self._rpm or 1.0)) % 360.0
        if self.isVisible():
            self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        if not self._pixmaps:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect()
        for key, pm in self._pixmaps.items():
            fade = self._fade[key]
            if fade <= 0.01:
                continue
            p.setOpacity(fade * 0.9)
            p.drawPixmap(rect, pm)
        p.setOpacity(1.0)

        w, h = self.width(), self.height()
        hub = QPointF(w * self.HUB_REL[0], h * self.HUB_REL[1])
        ring_radius = h * 0.285
        if self._state == "running":
            color = self._OVERLAY["running"]
            # 两条对置流光弧，以短弧序列形成拖尾。
            for base in (self._angle, self._angle + 180.0):
                for j in range(16):
                    angle_deg = base - j * 4.0
                    fade = (1.0 - j / 16.0) ** 2
                    c = QColor(color)
                    c.setAlphaF(0.55 * fade)
                    p.setPen(QPen(c, max(2.0, h * 0.006),
                                  Qt.SolidLine, Qt.RoundCap))
                    p.setBrush(Qt.NoBrush)
                    arc = QRectF(hub.x() - ring_radius,
                                 hub.y() - ring_radius,
                                 ring_radius * 2, ring_radius * 2)
                    start = int((90.0 - angle_deg - 2.2) * 16)
                    p.drawArc(arc, start, int(4.4 * 16))
            breath = 0.10 + 0.06 * math.sin(self._t * 3.2)
            c = QColor(color)
            c.setAlphaF(breath)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            hub_radius = h * 0.10
            p.drawEllipse(hub, hub_radius, hub_radius)
        elif self._state == "fault":
            color = self._OVERLAY["fault"]
            pulse = 0.5 + 0.5 * math.sin(self._t * 9.4)
            for radius, alpha in (
                    (ring_radius * 1.04, 0.14 + 0.30 * pulse),
                    (ring_radius * 1.10, 0.05 + 0.12 * pulse)):
                c = QColor(color)
                c.setAlphaF(alpha)
                p.setPen(QPen(c, max(2.0, h * 0.008),
                              Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(hub, radius, radius)
        p.end()


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

    def reset(self) -> None:
        """清除本次会话统计，供新实验从干净状态开始。"""
        self._mn = float("inf")
        self._mx = float("-inf")
        self._max.setText("最大：--")
        self._min.setText("最小：--")


class MonitorPage(QWidget):
    def __init__(self, comm: CommManager, control_page=None,
                 runtime_state=None) -> None:
        super().__init__()
        self._comm = comm
        self._ctrl = control_page
        self._latest: TelemetryFrame = TelemetryFrame()
        self._high_rate_samples = deque(maxlen=5000)
        self._high_rate_columns = {
            name: deque(maxlen=5000) for name in (
                "angle_deg", "speed_rpm", "iq_a", "iqref_a", "ia_a",
                "ib_a", "vd_raw", "vq_raw", "vbus_v")
        }
        self._high_rate_rate_hz = 200
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
        self._btn_filter_panel = QPushButton("波形滤波：全关")
        self._btn_filter_panel.setToolTip(
            "打开逐波形滤波控制面板；滤波只影响显示，"
            "原始缓冲、CSV和FFT保持不变。")
        self._btn_filter_panel.clicked.connect(self._show_filter_panel)
        title_row.addWidget(self._btn_filter_panel)
        self._btn_clear_curves = QPushButton("清空已有波形")
        self._btn_clear_curves.setToolTip(
            "清空全部趋势曲线、高速待处理样本和最大/最小统计；"
            "不会停止电机，也不会修改控制参数。")
        self._btn_clear_curves.clicked.connect(self._clear_all_curves)
        title_row.addWidget(self._btn_clear_curves)
        btn_save_all = QPushButton("保存所有波形")
        btn_save_all.clicked.connect(self._save_all_curves)
        title_row.addWidget(btn_save_all)
        root.addLayout(title_row)

        # 窄屏下将运行控制拆成独立一行，避免与标题/报告按钮互相挤压。
        control_row = QHBoxLayout()
        self._btn_start = QPushButton("启动"); self._btn_start.setObjectName("PrimaryButton")
        self._btn_stop = QPushButton("停止")
        self._btn_emerg = QPushButton("紧急停止"); self._btn_emerg.setObjectName("EmergencyButton")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_emerg.clicked.connect(self._on_emergency)
        # 在线调速：运行中直接改目标转速，无需切换页面
        control_row.addWidget(QLabel("目标转速"))
        self._speed_spin = QSpinBox()
        # Allow reverse for diagnostics (FOC + encoder sign check).
        # Was 0..20000, so operators could not command -rpm at all.
        self._speed_spin.setRange(-4000, 4000)
        self._speed_spin.setValue(1000)
        self._speed_spin.setSuffix(" rpm")
        self._speed_spin.setToolTip(
            "F407绝对范围±4000 rpm；实际命令还受电机控制页“最高转速”限制")
        control_row.addWidget(self._speed_spin)
        # 目标转速全局唯一入口：控制页启动电机时读取的也是这个框
        if self._ctrl is not None:
            self._ctrl._target_speed = self._speed_spin
        btn_set_speed = QPushButton("设定")
        btn_set_speed.clicked.connect(self._on_set_speed)
        control_row.addWidget(btn_set_speed)
        control_row.addStretch(1)
        for b in (self._btn_start, self._btn_stop, self._btn_emerg):
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
        rt_grid.addWidget(_category_box("机械角度", self._angle_dial,
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
        for widget in (self._stat_speed, self._stat_current, self._stat_torque):
            stat_h.addWidget(widget)
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
        self._c_position = TrendCurve(
            "位置环角度", {
                "实际位置": "#4fc3f7",
                "轨迹位置": "#81c784",
                "目标位置": "#ffb74d",
                "位置误差": "#f48fb1",
            }, y_label="°", buffer_size=5000)
        self._c_position_speed = TrendCurve(
            "位置环速度输出", {
                "速度给定": "#81c784",
                "速度前馈": "#ba68c8",
            }, y_label="rpm", buffer_size=5000)
        self._c_position_state = TrendCurve(
            "位置环限幅状态", {"速度限幅饱和": "#ff5252"},
            y_label="0/1", buffer_size=5000)
        # 施加电压 Vd/Vq（PI 输出，MCSDK 内部码值）——卡尔曼建模的控制输入 u。
        self._c_voltage = TrendCurve(
            "施加电压 Vd/Vq (码值)", {"Vd": "#80cbc4", "Vq": "#ffab91"},
            y_label="digit", buffer_size=5000)
        # 上位机 C++ 在线 ARX/RLS：用完整 ARX(3,1) 系数的直流增益与低频
        # 一阶矩换算等效 R/L，避免把三阶模型误当成只含 a1/b0 的一阶模型。
        self._c_rls_L = TrendCurve(
            "ARX 全系数等效电感 Ld/Lq (mH)",
            {"Ld": "#4db6ac", "Lq": "#ff8a65"},
            y_label="mH")
        self._c_rls_a1 = TrendCurve(
            "ARX 分母系数和 Σa（一阶参考≈0.944）",
            {"Σa_d": "#4fc3f7", "Σa_q": "#ba68c8"},
            y_label="")
        self._c_rls_R = TrendCurve(
            "ARX 全系数等效电阻 Rd/Rq (Ω)",
            {"Rd": "#81c784", "Rq": "#f06292"},
            y_label="Ω")

        # 慢速量没有额外的上位机采集滤波；F1 高速量会在收到
        # 数据后根据当前的“原始连续流 / 固件箱式平均”模式覆盖。
        for curve in (self._c_speed, self._c_torque, self._c_sensor_q,
                      self._c_position, self._c_position_speed,
                      self._c_position_state):
            curve.set_source_processing("F0常规遥测，上位机不做采集滤波")
        for curve in (self._c_rls_a1, self._c_rls_L, self._c_rls_R):
            curve.set_source_processing(
                "上位机C++在线辨识（F1原始量；不占用电流环ISR）")
        self._filter_dialog = _WaveformFilterDialog(
            self._filter_specs(), self._refresh_filter_button, self)

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

        position_tab = QWidget()
        position_curve_h = QHBoxLayout(position_tab)
        position_curve_h.addWidget(_make_curve_panel(
            self._c_position, "位置实际/目标/误差 °"))
        position_curve_h.addWidget(_make_curve_panel(
            self._c_position_speed, "位置环速度输出 rpm"))
        position_curve_h.addWidget(_make_curve_panel(
            self._c_position_state, "位置环限幅状态"))

        rls_tab = QWidget()
        rls_v = QVBoxLayout(rls_tab)
        self._rls_status = QLabel(
            "上位机RLS：0帧｜未收到数据｜点“启动/重置辨识”后启动电机")
        self._rls_status.setStyleSheet("color:#90a4ae;")
        self._rls_status.setWordWrap(True)
        self._rls_status.setMaximumHeight(58)
        rls_bar = QHBoxLayout()
        rls_bar.addWidget(self._rls_status, 1)
        self._btn_enable_rls = QPushButton("启动/重置辨识")
        self._btn_enable_rls.setToolTip(
            "在上位机C++核心中运行RLS；固件仅发送F1采样，不再计算RLS")
        self._btn_enable_rls.clicked.connect(self._on_enable_rls)
        rls_bar.addWidget(self._btn_enable_rls)
        rls_v.addLayout(rls_bar)
        rls_curve_h = QHBoxLayout()
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_a1, "ARX Σa（一阶参考≈0.944）"))
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_L, "全系数等效电感 (mH)"))
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_R, "全系数等效电阻 (Ω)"))
        rls_v.addLayout(rls_curve_h, 1)

        burst_tab = self._build_burst_tab()

        tabs = QTabWidget()
        tabs.setObjectName("CurveTabs")
        # 曲线页占用监控页剩余高度，不把内部 pyqtgraph 的
        # 默认高度传递给主窗口。
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        tabs.setMinimumHeight(0)
        tabs.addTab(trend_tab, "📈 趋势曲线（最近 1000 点）")
        tabs.addTab(sensor_tab, "🧭 传感器波形")
        tabs.addTab(power_tab, "⚡ 转矩与电压")
        tabs.addTab(position_tab, "🎯 位置三环")
        tabs.addTab(rls_tab, "🔬 在线辨识 (RLS)")
        tabs.addTab(burst_tab, "📸 抓取波形 (16kHz)")
        self._curve_tabs = tabs
        self._tab_curves = {
            0: (self._c_speed, self._c_current, self._c_phase_current),
            1: (self._c_angle, self._c_sensor_q),
            2: (self._c_torque, self._c_voltage),
            3: (self._c_position, self._c_position_speed,
                self._c_position_state),
            4: (self._c_rls_a1, self._c_rls_L, self._c_rls_R),
        }
        tabs.currentChanged.connect(self._on_curve_tab_changed)
        root.addWidget(tabs, 1)

        # 电机剖面背景衬在整张监控页右侧，低于所有内容。
        self._orb = _EnergyOrb(parent=self)
        self._orb.lower()

        # ---------- 连接信号 ----------
        comm.telemetryReceived.connect(self._on_telemetry)
        comm.highRateTelemetryReceived.connect(self._on_high_rate_telemetry)
        comm.highRateTelemetryBatchReceived.connect(
            self._on_high_rate_telemetry_batch)
        comm.highRateTelemetryColumnsReceived.connect(
            self._on_high_rate_telemetry_columns)
        comm.rlsCoeffReceived.connect(self._on_rls_coeff)
        comm.burstReceived.connect(self._on_burst)

        # ---------- 刷新定时器 ----------
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(MONITOR_PLOT_REFRESH_MS)


    # ---- slots ----
    def stop_visual_animations(self) -> None:
        """主窗口关闭前停止背景帧循环。"""
        self._orb._timer.stop()

    def _curve_is_active(self, curve: TrendCurve) -> bool:
        return curve in self._tab_curves.get(
            self._curve_tabs.currentIndex(), ())

    def _on_curve_tab_changed(self, index: int) -> None:
        """隐藏页只积累数据，切回可见时一次性同步到最新缓冲。"""
        for curve in self._tab_curves.get(index, ()):
            curve.redraw()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        # 按素材宽高比铺满右侧，垂直居中并让右缘轻微出血。
        height = int(self.height() * 1.12)
        width = int(height * _EnergyOrb.IMG_ASPECT)
        self._orb.setGeometry(self.width() - int(width * 0.96),
                              int((self.height() - height) / 2),
                              width, height)
        super().resizeEvent(ev)

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        self._last_telemetry_time = datetime.datetime.now().timestamp()
        filter_state = (
            bool(getattr(frame, "current_filter_enabled", False)),
            int(getattr(frame, "current_filter_alpha_q15", 0) or 0),
        )
        if filter_state != getattr(self, "_last_firmware_filter_state", None):
            self._last_firmware_filter_state = filter_state
            self._update_f1_processing_labels(self._high_rate_rate_hz)
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

    def _on_high_rate_telemetry_columns(self, columns: dict) -> None:
        count = int(columns.get("count", 0))
        if count <= 0:
            return
        for name, buffer in self._high_rate_columns.items():
            values = columns.get(name, ())
            buffer.extend(values[:count])
        self._high_rate_rate_hz = max(1, int(columns.get("rate_hz", 200)))
        self._update_f1_processing_labels(self._high_rate_rate_hz)
        vbus = columns.get("vbus_v", ())
        if vbus:
            self._latest_vbus_v = float(vbus[min(count, len(vbus)) - 1])
        self._last_high_angle_time = time.time()

    def _on_rls_coeff(self, sample: dict) -> None:
        """上位机在线辨识系数：画合理值，同时明示原始值和过滤原因。"""
        self._latest_rls = sample
        self._rls_rx_frames = getattr(self, "_rls_rx_frames", 0) + 1
        updates = int(sample.get("updates", 0))
        innov = sample.get("innov_rms_a", sample.get("innov_rms_digit", 0.0))
        p_trace = sample.get("p_trace", 0.0)
        a1_d, a1_q = sample.get("a1_d"), sample.get("a1_q")
        theta_d = sample.get("theta_d")
        theta_q = sample.get("theta_q")
        try:
            asum_d = sum(map(float, theta_d[:3]))
            asum_q = sum(map(float, theta_q[:3]))
        except (TypeError, IndexError):
            asum_d, asum_q = a1_d, a1_q
        ld, lq = sample.get("ld_mh"), sample.get("lq_mh")

        def _number(x):
            return isinstance(x, (int, float)) and math.isfinite(float(x))

        def _finite(x, lo, hi):
            return _number(x) and lo <= float(x) <= hi

        def _fmt(x, spec=".4g"):
            return format(float(x), spec) if _number(x) else "NaN/Inf"

        b_d = sample.get("b_dd0_si")
        b_q = sample.get("b_qq0_si")
        rd, rq = sample.get("rd_ohm"), sample.get("rq_ohm")
        rejected = []
        if not (_finite(a1_d, -8.0, 8.0) and _finite(a1_q, -8.0, 8.0)):
            rejected.append(
                f"a1越界[-8,8]({ _fmt(a1_d)}/{_fmt(a1_q)})")
        if not (_finite(ld, 0.05, 10.0) and _finite(lq, 0.05, 10.0)):
            rejected.append(
                f"L越界[0.05,10]mH({_fmt(ld, '.6g')}/{_fmt(lq, '.6g')})")
        if not (_finite(rd, 0.01, 20.0) and _finite(rq, 0.01, 20.0)):
            rejected.append(
                f"R越界[0.01,20]Ω({_fmt(rd)}/{_fmt(rq)})")

        # ARX(3,1)任一原始系数或协方差诊断已经退化时，换算出的L/R即使
        # 因巨大分子分母相消而落在物理范围内，也没有辨识意义。整帧统一
        # 拒绝，避免出现“a1已发散但L/R看起来正常”的误导性曲线。
        if theta_d is not None and theta_q is not None:
            try:
                ar_coeffs = [*theta_d[:3], *theta_q[:3]]
                voltage_coeffs = [*theta_d[3:7], *theta_q[3:7]]
            except (TypeError, IndexError):
                rejected.append("ARX系数数组不完整")
            else:
                if not all(_finite(value, -8.0, 8.0)
                           for value in ar_coeffs):
                    rejected.append("AR系数整体越界[-8,8]")
                if not all(_finite(value, -100.0, 100.0)
                           for value in voltage_coeffs):
                    rejected.append("电压系数整体越界[-100,100]A/V")
        elif not (_finite(b_d, -100.0, 100.0) and
                  _finite(b_q, -100.0, 100.0)):
            rejected.append("本轴电压系数越界[-100,100]A/V")

        if "p_trace" in sample and not _finite(p_trace, 1e-20, 1e12):
            rejected.append(f"P迹无效({_fmt(p_trace)})")
        if not _finite(innov, 0.0, 1e6):
            rejected.append(f"创新RMS无效({_fmt(innov)})")

        # 保持原因稳定且简洁，避免同一问题由a1和完整theta重复刷屏。
        rejected = list(dict.fromkeys(rejected))

        ld_s, lq_s = _fmt(ld, ".6g"), _fmt(lq, ".6g")
        reason = "；".join(rejected) if rejected else "无（三组曲线均已接收）"
        validity = "整帧无效" if rejected else "有效"
        self._rls_status.setText(
            f"本地结果：{self._rls_rx_frames}帧｜RLS更新：{updates}｜"
            f"全系数等效 Ld/Lq：{ld_s}/{lq_s} mH｜{validity}：{reason}\n"
            f"原始SI：a1={_fmt(a1_d)}/{_fmt(a1_q)}｜"
            f"Σa={_fmt(asum_d)}/{_fmt(asum_q)}｜"
            f"b0={_fmt(b_d)}/{_fmt(b_q)} A/V｜"
            f"innov={_fmt(innov)} A｜P迹={_fmt(p_trace)}")
        self._rls_status.setStyleSheet(
            "color:#ff8a80;" if rejected else "color:#81c784;")

        # 三组结果来自同一个ARX模型，必须整帧有效后一起接收。
        if not rejected:
            self._c_rls_a1.append(
                {"Σa_d": asum_d, "Σa_q": asum_q},
                redraw=self._curve_is_active(self._c_rls_a1))
            self._c_rls_L.append(
                {"Ld": ld, "Lq": lq},
                redraw=self._curve_is_active(self._c_rls_L))
            self._c_rls_R.append(
                {"Rd": rd, "Rq": rq},
                redraw=self._curve_is_active(self._c_rls_R))

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
            self._c_position: 1,
            self._c_position_speed: 5,
            self._c_position_state: 1,
            self._c_voltage: 9,
            self._c_rls_a1: 1,          # 已是慢变量，无需平滑
            self._c_rls_L: 1,
            self._c_rls_R: 5,           # R 反解噪声大，轻平滑便于读趋势
        }
        for curve, n in windows.items():
            curve.set_smoothing(n if on else 1)

    def _filter_specs(self) -> list[tuple[str, TrendCurve, int]]:
        return [
            ("转速", self._c_speed, 15),
            ("高速Iq", self._c_current, 16),
            ("相电流Ia/Ib", self._c_phase_current, 16),
            ("转矩", self._c_torque, 15),
            ("电角度", self._c_angle, 2),
            ("传感器诊断", self._c_sensor_q, 3),
            ("位置角度", self._c_position, 3),
            ("位置环速度", self._c_position_speed, 5),
            ("位置限幅", self._c_position_state, 2),
            ("施加电压Vd/Vq", self._c_voltage, 16),
            ("RLS Σa", self._c_rls_a1, 3),
            ("RLS 电感", self._c_rls_L, 3),
            ("RLS 电阻", self._c_rls_R, 5),
        ]

    def _show_filter_panel(self) -> None:
        self._filter_dialog.refresh_details()
        self._filter_dialog.show()
        self._filter_dialog.raise_()
        self._filter_dialog.activateWindow()

    def _refresh_filter_button(self) -> None:
        count = self._filter_dialog.enabled_count()
        self._btn_filter_panel.setText(
            "波形滤波：全关" if count == 0 else f"波形滤波：{count}路开启")

    def _update_f1_processing_labels(self, rate_hz: int) -> None:
        """按当前 F1 固件路径明示采样/滤波细节。"""
        rate_hz = max(1, int(rate_hz))
        filter_enabled = bool(getattr(
            self._latest, "current_filter_enabled", False))
        alpha_q15 = int(getattr(
            self._latest, "current_filter_alpha_q15", 0) or 0)
        if filter_enabled and 0 < alpha_q15 < 32768:
            alpha = alpha_q15 / 32768.0
            cutoff = (-16000.0 / (2.0 * math.pi) *
                      math.log(max(1e-9, 1.0 - alpha)))
            control_filter = (
                f"Iq为PI实际反馈：固件IIR已启用（fc≈{cutoff:.0f} Hz）")
        elif filter_enabled:
            control_filter = "Iq为PI实际反馈：固件IIR已启用"
        else:
            control_filter = "Iq为PI实际反馈：固件滤波已旁路"
        configured_stream_rate = int(
            getattr(self._comm, "_f1_stream_rate_hz", 0) or 0)
        raw_stream = configured_stream_rate > 0 or rate_hz > 1000
        if raw_stream:
            divider = max(1, round(16000 / rate_hz))
            if divider == 1:
                current_processing = (
                    "16 kHz FOC每周期反馈点")
            else:
                current_processing = (
                    f"16 kHz FOC反馈点每{divider}点抽1点"
                    "（无抗混叠滤波）")
            angle_processing = current_processing
        else:
            current_processing = (
                "固件16点箱式平均（16 kHz下窗长1.00 ms）；"
                "用于兼容遥测")
            angle_processing = "无滤波；按 F1 发送时刻采样电角度"
        self._c_current.set_source_processing(
            f"{current_processing}；{control_filter}",
            rate_hz)
        for curve in (self._c_phase_current, self._c_voltage):
            curve.set_source_processing(current_processing, rate_hz)
        self._c_angle.set_source_processing(angle_processing, rate_hz)
        dialog = getattr(self, "_filter_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.refresh_details()

    @staticmethod
    def fourier_source_items() -> list[tuple[str, str]]:
        """离线傅里叶页的当前缓冲信号列表。"""
        return [
            ("相电流 Ia", "phase_ia"), ("相电流 Ib", "phase_ib"),
            ("q轴电流 Iq", "iq"), ("q轴电流给定 Iqref", "iqref"),
            ("施加电压 Vd（码值）", "vd"),
            ("施加电压 Vq（码值）", "vq"),
            ("实际转速", "speed"), ("转速给定", "speedref"),
            ("估算转矩", "torque"),
        ]

    def fourier_snapshot(self, key: str) -> dict:
        """为离线分析复制原始缓冲，不传递显示平滑后的数据。"""
        sources = {
            "phase_ia": (self._c_phase_current, "Ia", "A"),
            "phase_ib": (self._c_phase_current, "Ib", "A"),
            "iq": (self._c_current, "实际 Iq", "A"),
            "iqref": (self._c_current, "给定 Iq", "A"),
            "vd": (self._c_voltage, "Vd", "digit"),
            "vq": (self._c_voltage, "Vq", "digit"),
            "speed": (self._c_speed, "实际", "rpm"),
            "speedref": (self._c_speed, "给定", "rpm"),
            "torque": (self._c_torque, "实际", "Nm"),
        }
        if key not in sources:
            raise KeyError(key)
        curve, series, unit = sources[key]
        snapshot = curve.raw_snapshot(series)
        snapshot["unit"] = unit
        snapshot["analysis_kind"] = (
            "ac" if key in ("phase_ia", "phase_ib") else "dc")
        return snapshot

    def _clear_all_curves(self) -> None:
        """清空监控页全部实验波形和派生统计，不影响设备运行状态。"""
        self._high_rate_samples.clear()
        for buffer in self._high_rate_columns.values():
            buffer.clear()
        self._angle_dial.reset()
        for curve in (
                self._c_speed, self._c_current, self._c_phase_current,
                self._c_torque, self._c_angle, self._c_sensor_q,
                self._c_position, self._c_position_speed,
                self._c_position_state, self._c_voltage,
                self._c_rls_a1, self._c_rls_L, self._c_rls_R):
            curve.clear()
        for stat in (self._stat_speed, self._stat_current, self._stat_torque):
            stat.reset()
        self._latest_rls = {}
        self._rls_rx_frames = 0
        self._rls_status.setText(
            "上位机RLS：0帧｜未收到数据｜点“启动/重置辨识”后启动电机")
        self._rls_status.setStyleSheet("color:#90a4ae;")
        self._last_high_angle_time = 0.0
        self._curves_were_active = False
        if _MP_PG_OK:
            for item_name in (
                    "_burst_raw_ia", "_burst_raw_ib",
                    "_burst_avg_ia", "_burst_avg_ib"):
                item = getattr(self, item_name, None)
                if item is not None:
                    item.setData([], [])
            self._burst_status.setText(
                "波形已清空；电机运行中可重新抓取 16kHz 波形。")
        self._datasrc_label.setText("[ 波形已清空 ]")
        self._datasrc_label.setStyleSheet(
            "color: #90a4ae; font-weight: bold;")

    def _on_enable_rls(self) -> None:
        """复位并启动上位机 C++ RLS；固件仅负责高速采样。"""
        if not self._comm.is_connected():
            self._rls_status.setText("上位机RLS：通信未连接，无法启动辨识")
            self._rls_status.setStyleSheet("color:#ff8a80;")
            return
        if self._comm.start_host_rls():
            self._rls_status.setText(
                "上位机RLS：等待｜C++辨识已复位；启动后等待首个10 Hz结果")
            self._rls_status.setStyleSheet("color:#ffcc80;")
        else:
            self._rls_status.setText("上位机RLS：C++核心不可用，启动失败")
            self._rls_status.setStyleSheet("color:#ff8a80;")

    def _orb_state(self) -> str:
        """由运行状态机和母线状态推导电机背景的故障优先级。"""
        state_machine = getattr(self._ctrl, "_state_machine", None)
        if (state_machine is not None and
                getattr(state_machine.state, "value", "") == "fault_locked"):
            return "fault"
        if self._latest.bus_state == "ov":
            return "fault"
        return ""

    def _refresh(self) -> None:
        import time
        idle = (time.time() - self._last_telemetry_time) > 1.0
        if idle:
            self._refresh_datasource_label("idle")
            self._orb.set_state(self._orb_state() or "stopped")
            return
        f = self._latest
        # 三态优先级：故障 > 运行（|转速|>5rpm）> 停止。
        orb_state = self._orb_state()
        if not orb_state:
            orb_state = "running" if abs(f.speed_actual) > 5.0 else "stopped"
        self._orb.set_state(orb_state, f.speed_actual)
        high_rate = list(self._high_rate_samples)
        self._high_rate_samples.clear()
        high_columns = {
            name: list(buffer)
            for name, buffer in self._high_rate_columns.items()
        }
        for buffer in self._high_rate_columns.values():
            buffer.clear()
        if high_rate:
            for sample in high_rate:
                high_columns["angle_deg"].append(float(sample["angle_deg"]))
                high_columns["speed_rpm"].append(
                    float(sample.get("speed_rpm", 0.0)))
                high_columns["iq_a"].append(float(sample["iq_a"]))
                high_columns["iqref_a"].append(float(sample["iqref_a"]))
                high_columns["ia_a"].append(float(sample.get("ia_a", 0.0)))
                high_columns["ib_a"].append(float(sample.get("ib_a", 0.0)))
                high_columns["vd_raw"].append(
                    float(sample.get("vd_raw", 0.0)))
                high_columns["vq_raw"].append(
                    float(sample.get("vq_raw", 0.0)))
                high_columns["vbus_v"].append(
                    float(sample.get("vbus_v", 0.0)))
            self._high_rate_rate_hz = max(
                1, int(high_rate[-1].get("rate_hz", 200)))
            self._update_f1_processing_labels(self._high_rate_rate_hz)
        high_count = len(high_columns["angle_deg"])
        self._speed_actual.set_value(f.speed_actual)
        self._speed_target.set_value(f.speed_target)
        self._current_actual.set_value(f.current_actual)
        self._current_target.set_value(f.current_target)
        self._torque_actual.set_value(f.torque_actual)
        self._torque_target.set_value(f.torque_target)
        position_mode_selected = False
        if self._ctrl is not None and hasattr(self._ctrl, "_current_mode"):
            try:
                position_mode_selected = (
                    self._ctrl._current_mode() == "位置三环控制")
            except Exception:
                position_mode_selected = False
        position_signal_present = any(abs(float(value)) > 1e-9 for value in (
            f.position_actual_deg, f.position_target_deg,
            f.position_error_deg, f.position_trajectory_deg,
            f.position_speed_target_rpm,
            f.position_speed_ff_rpm,
        )) or bool(f.position_saturated)
        position_active = (
            int(getattr(f, "mc_state", 0)) == 6 and
            (position_mode_selected or position_signal_present))
        if position_active:
            self._angle_dial.feed(f.position_actual_deg)
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
        curves_active = bool(high_count) or position_active or any(
            abs(value) > 1e-9 for value in (
                f.speed_actual, f.speed_target, f.current_actual,
                f.current_target, f.torque_actual))
        # 新一次运行开始：解除上次停机后用户缩放造成的时间轴定格，
        # 否则新数据画在可视窗口之外，看起来像"曲线不再更新"。
        if curves_active and not getattr(self, "_curves_were_active", False):
            for curve in (self._c_speed, self._c_current,
                          self._c_phase_current, self._c_torque,
                          self._c_angle, self._c_sensor_q, self._c_voltage,
                          self._c_position, self._c_position_speed,
                          self._c_position_state,
                          self._c_rls_a1, self._c_rls_L, self._c_rls_R):
                curve.resume_follow()
        self._curves_were_active = curves_active
        if curves_active:
            self._c_speed.append(
                {"实际": f.speed_actual, "给定": f.speed_target},
                redraw=self._curve_is_active(self._c_speed))
            if high_count:
                interval_s = 1.0 / max(self._high_rate_rate_hz, 1)
                self._c_current.append_columns({
                    "实际 Iq": high_columns["iq_a"],
                    "给定 Iq": high_columns["iqref_a"],
                }, interval_s, redraw=self._curve_is_active(self._c_current))
                self._c_phase_current.append_columns({
                    "Ia": high_columns["ia_a"],
                    "Ib": high_columns["ib_a"],
                }, interval_s,
                    redraw=self._curve_is_active(self._c_phase_current))
                self._c_voltage.append_columns({
                    "Vd": high_columns["vd_raw"],
                    "Vq": high_columns["vq_raw"],
                }, interval_s, redraw=self._curve_is_active(self._c_voltage))
            else:
                self._c_current.append(
                    {"实际 Iq": f.current_actual, "给定 Iq": f.current_target},
                    redraw=self._curve_is_active(self._c_current))
            self._c_torque.append(
                {"实际": f.torque_actual},
                redraw=self._curve_is_active(self._c_torque))

            if high_count:
                self._c_angle.append_columns({
                    "高速电角度": high_columns["angle_deg"],
                }, interval_s, redraw=self._curve_is_active(self._c_angle))
            elif time.time() - self._last_high_angle_time > 1.0:
                self._c_angle.append(
                    {"高速电角度": f.angle_actual},
                    redraw=self._curve_is_active(self._c_angle))
            self._c_sensor_q.append(
                {"质量": f.sensor_quality, "收敛度": f.convergence},
                redraw=self._curve_is_active(self._c_sensor_q))
            if position_active:
                self._c_position.append({
                    "实际位置": f.position_actual_deg,
                    "轨迹位置": f.position_trajectory_deg,
                    "目标位置": f.position_target_deg,
                    "位置误差": f.position_error_deg,
                }, redraw=self._curve_is_active(self._c_position))
                self._c_position_speed.append({
                    "速度给定": f.position_speed_target_rpm,
                    "速度前馈": f.position_speed_ff_rpm,
                }, redraw=self._curve_is_active(self._c_position_speed))
                self._c_position_state.append({
                    "速度限幅饱和": 1.0 if f.position_saturated else 0.0,
                }, redraw=self._curve_is_active(self._c_position_state))
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
            (self._c_position, "位置环角度"),
            (self._c_position_speed, "位置环速度输出"),
            (self._c_position_state, "位置环限幅状态"),
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
        mode = (self._ctrl._current_mode()
                if self._ctrl is not None and hasattr(self._ctrl, "_current_mode")
                else None)
        record_dir = create_waveform_record_dir(
            category_for_control_mode(mode), datetime.datetime.now())
        path, _ = QFileDialog.getSaveFileName(
            self, "保存所有波形",
            str(record_dir / "波形.png"),
            "PNG (*.png)"
        )
        if not path:
            record_dir.rmdir()
            return
        with open(path, "wb") as f:
            f.write(png)
        csv_path = os.path.join(os.path.dirname(path), "原始数据.csv")
        self._write_curves_csv(csv_path)
        QMessageBox.information(
            self, "保存成功",
            f"本次实验已独立保存到：\n{os.path.dirname(path)}\n\n"
            f"波形图：{os.path.basename(path)}\n"
            f"原始数据：{os.path.basename(csv_path)}")

    def _write_curves_csv(self, path: str) -> None:
        """按原始采样时间导出所有曲线，不对不同采样率做伪对齐。"""
        curves = [
            (self._c_speed, "speed"),
            (self._c_current, "iq_current"),
            (self._c_phase_current, "phase_current"),
            (self._c_torque, "torque"),
            (self._c_angle, "electrical_angle"),
            (self._c_sensor_q, "sensor_diagnostics"),
            (self._c_position, "position_angle_deg"),
            (self._c_position_speed, "position_speed_rpm"),
            (self._c_position_state, "position_state"),
            (self._c_voltage, "applied_voltage"),
            (self._c_rls_a1, "rls_a1"),
            (self._c_rls_L, "rls_inductance_mh"),
            (self._c_rls_R, "rls_resistance_ohm"),
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "channel", "time_s", "series", "value", "sampling_rate_hz",
                "source_filter", "display_filter", "unit",
            ])
            for curve, channel in curves:
                times = list(curve._times)
                snapshot_meta = curve.raw_snapshot(next(iter(curve._buffers)))
                sample_rate_hz = float(snapshot_meta["sample_rate_hz"])
                source_filter = str(snapshot_meta["source_processing"])
                display_filter = str(snapshot_meta["display_filter"])
                for series, values in curve._buffers.items():
                    samples = list(values)
                    sample_times = times[-len(samples):] if samples else []
                    for timestamp, value in zip(sample_times, samples):
                        writer.writerow([
                            channel, f"{timestamp:.6f}", series,
                            f"{float(value):.9g}",
                            f"{sample_rate_hz:.9g}" if sample_rate_hz > 0 else "",
                            source_filter, display_filter, curve._y_label,
                        ])

    def _on_set_speed(self) -> None:
        """在 READY 预设目标，或在 RUNNING 在线修改目标。"""
        if self._comm.is_sim_running() and not self._comm.is_connected():
            QMessageBox.information(
                self, "请使用数字孪生工作台",
                "仿真目标转速已经移至“数字孪生”页面，监控页不再修改仿真模型。")
            return
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

    def _on_start(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_start()

    def _on_stop(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_stop()

    def _on_emergency(self) -> None:
        if self._ctrl is not None:
            self._ctrl._on_emergency()
