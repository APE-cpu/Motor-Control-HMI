"""监控页面：实时数据、统计、趋势曲线。"""
import csv
import datetime
import math
import os
import time
from collections import deque
from PySide6.QtCore import Qt, QThread, QTimer, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QDoubleSpinBox, QLabel, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from communications.native_telemetry import run_offline_rls_analysis
from config.config import (
    CMD_SET_PARAMS, F1_CURRENT_A_PER_DIGIT, MONITOR_PLOT_REFRESH_MS,
)
from rls_offline import (
    RlsCaptureBuffer, RlsCaptureSnapshot, load_rls_capture_csv,
)
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


def _write_curve_csv_snapshot(path: str, curves: list[dict]) -> None:
    """在不访问 Qt 控件的情况下写出已固化的曲线数据。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "channel", "time_s", "series", "value", "sampling_rate_hz",
            "source_filter", "display_filter", "unit",
        ])
        for item in curves:
            times = item["times"]
            for series, samples in item["series"]:
                sample_times = times[-len(samples):] if samples else ()
                for timestamp, value in zip(sample_times, samples):
                    writer.writerow([
                        item["channel"], f"{timestamp:.6f}", series,
                        f"{float(value):.9g}",
                        (f"{item['sampling_rate_hz']:.9g}"
                         if item["sampling_rate_hz"] > 0 else ""),
                        item["source_filter"], item["display_filter"],
                        item["unit"],
                    ])


class _WaveformSaveWorker(QThread):
    """将大 CSV 和 PNG 的磁盘 I/O 移出 GUI 线程。"""

    completed = Signal(object)

    def __init__(self, png_path: str, png: bytes, csv_path: str,
                 curve_snapshot: list[dict], rls_csv_path: str,
                 rls_snapshot: RlsCaptureSnapshot, parent=None) -> None:
        super().__init__(parent)
        self._png_path = png_path
        self._png = png
        self._csv_path = csv_path
        self._curve_snapshot = curve_snapshot
        self._rls_csv_path = rls_csv_path
        self._rls_snapshot = rls_snapshot

    def run(self) -> None:
        result = {
            "error": "",
            "png_path": self._png_path,
            "csv_path": self._csv_path,
            "rls_csv_path": self._rls_csv_path,
            "rls_exported": False,
            "rls_count": self._rls_snapshot.sample_count,
            "rls_duration_s": self._rls_snapshot.duration_s,
        }
        try:
            with open(self._png_path, "wb") as stream:
                stream.write(self._png)
            _write_curve_csv_snapshot(self._csv_path, self._curve_snapshot)
            if self._rls_snapshot.sample_count > 0:
                self._rls_snapshot.write_csv(self._rls_csv_path)
                result["rls_exported"] = True
        except Exception as exc:
            result["error"] = str(exc)
        self.completed.emit(result)


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


class _RlsCoefficientDialog(QDialog):
    """完整显示 ARX(3,1) 的 d/q 各 7 个原始系数，不占主页面高度。"""

    _COLORS = (
        "#4fc3f7", "#81c784", "#ba68c8", "#ffb74d",
        "#ff8a65", "#80cbc4", "#f06292",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("RLS 完整 ARX 系数（d/q 各7项）")
        self.resize(1180, 760)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        root = QVBoxLayout(self)
        note = QLabel(
            "原始系数顺序：θ=[a1, a2, a3, b_d0, b_d1, b_q0, b_q1]。"
            "a 无量纲，b 为 A/V；它们是模型系数，不等于直接的 R/L。")
        note.setStyleSheet("color:#90a4ae;")
        note.setWordWrap(True)
        root.addWidget(note)
        d_names = (
            "a1_d", "a2_d", "a3_d", "b_dd0", "b_dd1", "b_dq0", "b_dq1")
        q_names = (
            "a1_q", "a2_q", "a3_q", "b_qd0", "b_qd1", "b_qq0", "b_qq1")
        self._d_curve = TrendCurve(
            "d轴 ARX 完整7系数",
            dict(zip(d_names, self._COLORS)),
            y_label="a:无量纲 / b:A/V")
        self._q_curve = TrendCurve(
            "q轴 ARX 完整7系数",
            dict(zip(q_names, self._COLORS)),
            y_label="a:无量纲 / b:A/V")
        for curve in (self._d_curve, self._q_curve):
            curve.set_source_processing(
                "上位机C++ RLS原始系数；未做显示平滑")
            curve.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
            root.addWidget(curve, 1)

    def append_coefficients(self, theta_d, theta_q) -> bool:
        try:
            d = [float(value) for value in theta_d[:7]]
            q = [float(value) for value in theta_q[:7]]
        except (TypeError, ValueError, IndexError):
            return False
        if len(d) != 7 or len(q) != 7 or not all(
                math.isfinite(value) for value in (*d, *q)):
            return False
        self._d_curve.append(dict(zip(self._d_curve._buffers, d)),
                             redraw=self.isVisible())
        self._q_curve.append(dict(zip(self._q_curve._buffers, q)),
                             redraw=self.isVisible())
        return True

    def clear(self) -> None:
        self._d_curve.clear()
        self._q_curve.clear()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self._d_curve.redraw()
        self._q_curve.redraw()
        super().showEvent(event)


class _OfflineRlsWorker(QThread):
    """在后台线程中读取大 CSV 并重放 C++ RLS，避免冻结界面。"""

    completed = Signal(object, object, str)

    def __init__(self, path: str = "", snapshot: RlsCaptureSnapshot | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._path = path
        self._snapshot = snapshot

    def run(self) -> None:
        try:
            dataset = (self._snapshot.to_columns() if self._snapshot is not None
                       else load_rls_capture_csv(self._path))
            analysis = run_offline_rls_analysis(dataset)
        except Exception as exc:  # 错误需回到 UI 线程显示
            self.completed.emit(None, None, str(exc))
            return
        self.completed.emit(dataset, analysis, "")


class _OfflineRlsDialog(QDialog):
    """离线 RLS 重放结果；独立窗口不占监控页高度。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("离线辨识（真机匹配ESO + IV物理验证）")
        self.resize(1240, 760)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._worker = None
        self._source_path = ""
        root = QVBoxLayout(self)
        self._status = QLabel(
            "选择“RLS辨识数据.csv”后：ESO按真机采样率、"
            "Lnom=0.66mH、ωo=4000rad/s与1拍电压延迟运行；"
            "物理R/L仍只由独立F1/40闭环IV链路判定。")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#90a4ae;")
        root.addWidget(self._status)
        bar = QHBoxLayout()
        self._btn_coeff = QPushButton("完整系数(7×2) ↗")
        self._btn_coeff.setEnabled(False)
        self._btn_coeff.clicked.connect(self._show_coefficients)
        bar.addStretch(1)
        bar.addWidget(self._btn_coeff)
        root.addLayout(bar)

        self._c_a = TrendCurve(
            "离线 ARX 分母系数和 Σa",
            {"Σa_d": "#4fc3f7", "Σa_q": "#ba68c8"})
        self._c_l = TrendCurve(
            "离线 ARX 本轴输入系数 b0",
            {"b0_d": "#4db6ac", "b0_q": "#ff8a65"}, y_label="A/V")
        self._c_r = TrendCurve(
            "离线 ARX 本轴延迟输入系数 b1",
            {"b1_d": "#81c784", "b1_q": "#f06292"}, y_label="A/V")
        self._c_id = TrendCurve(
            "d轴电流：原始 vs ESO",
            {"原始 id": "#90a4ae", "ESO id_hat": "#4fc3f7"},
            y_label="A", buffer_size=5000)
        self._c_iq = TrendCurve(
            "q轴电流：原始 vs ESO",
            {"原始 iq": "#90a4ae", "ESO iq_hat": "#ff8a65"},
            y_label="A", buffer_size=5000)
        tabs = QTabWidget()
        current_tab = QWidget()
        current_layout = QHBoxLayout(current_tab)
        current_layout.setContentsMargins(0, 0, 0, 0)
        for curve, title in (
                (self._c_id, "d轴 ESO 对照"),
                (self._c_iq, "q轴 ESO 对照")):
            curve.set_source_processing(
                "真机匹配ESO（16kHz/Lnom=0.66mH/ωo=4000）电流对照")
            current_layout.addWidget(_make_curve_panel(curve, title))
        tabs.addTab(current_tab, "电流 / ESO 复现")

        parameter_tab = QWidget()
        panels = QHBoxLayout(parameter_tab)
        panels.setContentsMargins(0, 0, 0, 0)
        for curve, title in (
                (self._c_a, "ARX Σa"),
                (self._c_l, "本轴 b0"),
                (self._c_r, "本轴 b1")):
            curve.set_source_processing(
                "离线 CSV → 真机匹配C++ ESO→三阶RLS；原始ARX系数，"
                "不冒充物理R/L")
            panels.addWidget(_make_curve_panel(curve, title))
        tabs.addTab(parameter_tab, "RLS 参数收敛")
        root.addWidget(tabs, 1)
        self._coeff_dialog = _RlsCoefficientDialog(self)

    def start(self, path: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._source_path = path
        self._status.setText(
            f"正在读取 {os.path.basename(path)} 并执行真机ESO/RLS与IV验证…")
        self._status.setStyleSheet("color:#ffcc80;")
        self._btn_coeff.setEnabled(False)
        for curve in (self._c_a, self._c_l, self._c_r,
                      self._c_id, self._c_iq):
            curve.clear()
        self._coeff_dialog.clear()
        self.show()
        self.raise_()
        self._worker = _OfflineRlsWorker(path=path, parent=self)
        self._worker.completed.connect(self._complete)
        self._worker.start()

    def start_snapshot(self, snapshot: RlsCaptureSnapshot,
                       label: str = "当前冻结采集") -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._source_path = label
        self._status.setText(
            f"正在分析{label}，无需CSV写盘…")
        self._status.setStyleSheet("color:#ffcc80;")
        self._btn_coeff.setEnabled(False)
        for curve in (self._c_a, self._c_l, self._c_r,
                      self._c_id, self._c_iq):
            curve.clear()
        self._coeff_dialog.clear()
        self.show()
        self.raise_()
        self._worker = _OfflineRlsWorker(snapshot=snapshot, parent=self)
        self._worker.completed.connect(self._complete)
        self._worker.start()

    def _complete(self, dataset, analysis, error: str) -> None:
        if error:
            self._status.setText(f"离线辨识失败：{error}")
            self._status.setStyleSheet("color:#ff8a80;")
            return
        results = list((analysis or {}).get("results", ()))
        if not results:
            self._status.setText("离线辨识未产生结果")
            self._status.setStyleSheet("color:#ff8a80;")
            return
        ticks = [int(item.get("tick_ms", 0)) for item in results]
        if ticks and any(ticks[index] != ticks[0]
                         for index in range(1, len(ticks))):
            # tick_ms 是 uint32，长时运行后可在文件中跨越回绕点。
            times = [((tick - ticks[0]) & 0xFFFFFFFF) / 1000.0
                     for tick in ticks]
        else:
            times = [index * 0.1 for index in range(len(results))]
        for item in results:
            theta_d = item.get("theta_d", ())
            theta_q = item.get("theta_q", ())
            try:
                asum_d = sum(float(value) for value in theta_d[:3])
                asum_q = sum(float(value) for value in theta_q[:3])
            except (TypeError, ValueError, IndexError):
                asum_d = float(item.get("a1_d", 0.0))
                asum_q = float(item.get("a1_q", 0.0))
            self._c_a.append({"Σa_d": asum_d, "Σa_q": asum_q}, redraw=False)
            try:
                b0_d = float(theta_d[3])
                b1_d = float(theta_d[4])
                b0_q = float(theta_q[5])
                b1_q = float(theta_q[6])
            except (TypeError, ValueError, IndexError):
                b0_d = b1_d = b0_q = b1_q = float("nan")
            self._c_l.append({
                "b0_d": b0_d, "b0_q": b0_q,
            }, redraw=False)
            self._c_r.append({
                "b1_d": b1_d, "b1_q": b1_q,
            }, redraw=False)
            self._coeff_dialog.append_coefficients(theta_d, theta_q)
        trace = dict((analysis or {}).get("trace", {}))
        trace_indices = list(trace.get("sample_index", ()))
        for id_a, iq_a, id_hat, iq_hat in zip(
                trace.get("id_a", ()), trace.get("iq_a", ()),
                trace.get("id_hat_a", ()), trace.get("iq_hat_a", ())):
            self._c_id.append({"原始 id": id_a, "ESO id_hat": id_hat},
                              redraw=False)
            self._c_iq.append({"原始 iq": iq_a, "ESO iq_hat": iq_hat},
                              redraw=False)
        trace_times = [index / float(dataset["rate_hz"])
                       for index in trace_indices]
        for curve in (self._c_id, self._c_iq):
            curve._times.clear()
            curve._times.extend(trace_times[-curve._times.maxlen:])
        for curve in (self._c_a, self._c_l, self._c_r,
                      self._coeff_dialog._d_curve,
                      self._coeff_dialog._q_curve):
            curve._times.clear()
            curve._times.extend(times[-curve._times.maxlen:])
            curve.redraw()
        final = results[-1]
        duration = float(dataset["count"]) / float(dataset["rate_hz"])
        raw_finite = all(math.isfinite(float(value)) for value in (
            *final.get("theta_d", ()), *final.get("theta_q", ())))
        diagnostics = dict((analysis or {}).get("diagnostics", {}))
        physical = dict((analysis or {}).get("physical_iv", {}))
        reference = dict((analysis or {}).get("simulink_reference", {}))
        legacy = dict((analysis or {}).get("legacy_evidence", {}))
        physical_usable = physical.get("verdict") == "usable"
        nominal_match = bool(physical.get("nominal_match", False))
        identifiable = diagnostics.get("verdict") == "usable"
        if not raw_finite:
            verdict = "⚠ 原始系数出现 NaN/Inf"
        elif physical_usable and nominal_match:
            verdict = "真机闭环辨识通过，且与固件理论参数一致"
        elif physical_usable:
            verdict = "⚠ 物理辨识可重复，但偏离固件理论参数"
        elif identifiable:
            verdict = "数值重放完成；基础可辨识性检查通过"
        else:
            verdict = "⚠ 数值稳定不等于物理收敛；本数据不可用于R/L结论"
        current_note = f"ESO电流对照 {len(trace_indices)} 点"
        diagnostic_reasons = "；".join(
            str(item) for item in diagnostics.get("reasons", ()))
        diagnostic_line = (
            f"\n可辨识性：{diagnostic_reasons}"
            if diagnostic_reasons else "")
        if physical_usable:
            median_vdda = float(physical.get("median_vdda_v", float("nan")))
            vdda_note = (f"VDDA={median_vdda:.4g} V，"
                         if math.isfinite(median_vdda) else "")
            physical_line = (
                "\n真机IV物理结果（参考注入→电压/电流交叉谱）："
                f"Ld/Lq={float(physical['ld_mh']):.6g}/"
                f"{float(physical['lq_mh']):.6g} mH，"
                f"Rd/Rq={float(physical['rd_ohm']):.6g}/"
                f"{float(physical['rq_ohm']):.6g} Ω，"
                f"离散极点a(d/q)={float(physical['d_axis']['a']):.6f}/"
                f"{float(physical['q_axis']['a']):.6f}，"
                f"延迟d/q={float(physical['d_axis']['delay_samples']):.3g}/"
                f"{float(physical['q_axis']['delay_samples']):.3g} 拍，"
                f"{vdda_note}"
                f"电频率={float(physical['electrical_frequency_hz_from_angle']):.3g} Hz，"
                f"转速P5～P95跨度="
                f"{float(physical.get('speed_p90_span_rpm', float('nan'))):.3g} rpm，"
                f"激励相关={float(physical['probe_reference_correlation']):.3f}，"
                f"反馈滤波差异RMS(d/q)="
                f"{float(physical['feedback_vs_raw_id_rmse_a']):.4g}/"
                f"{float(physical['feedback_vs_raw_iq_rmse_a']):.4g} A；"
                f"标称Rs/Ls={float(physical.get('nominal_r_ohm', .59)):.4g} Ω/"
                f"{float(physical.get('nominal_l_mh', .66)):.4g} mH；"
                f"标称判定={'通过' if nominal_match else '未通过'}")
            if not nominal_match:
                nominal_reasons = "；".join(
                    str(item) for item in physical.get("nominal_reasons", ()))
                if nominal_reasons:
                    physical_line += f"（{nominal_reasons}）"
        else:
            physical_reasons = "；".join(
                str(item) for item in physical.get("reasons", ()))
            scale_parts = []
            for key, label in (("median_vbus_v", "Vbus"),
                               ("median_vdda_v", "VDDA")):
                value = float(physical.get(key, float("nan")))
                if math.isfinite(value):
                    scale_parts.append(f"{label}={value:.4g} V")
            candidate_parts = []
            for key, label in (("d_axis", "d"), ("q_axis", "q")):
                axis = physical.get(key)
                if not isinstance(axis, dict):
                    continue
                r_value = float(axis.get("resistance_ohm", float("nan")))
                l_value = float(axis.get("inductance_mh", float("nan")))
                fit = float(axis.get("fit_nrmse", float("nan")))
                delay = float(axis.get("delay_samples", float("nan")))
                coherence = float(axis.get("median_coherence", float("nan")))
                if math.isfinite(r_value) and math.isfinite(l_value):
                    candidate_parts.append(
                        f"{label}候选R/L={r_value:.4g}Ω/{l_value:.4g}mH，"
                        f"残差={fit:.3f}，延迟={delay:.3g}拍，"
                        f"相干={coherence:.3f}")
            evidence = "；".join((*scale_parts, *candidate_parts))
            details = "；".join(
                item for item in (physical_reasons, evidence) if item)
            physical_line = (f"\n真机IV物理辨识未通过：{details}"
                             if details else "")
        reference_warning = str(reference.get("warning", ""))
        reference_line = (f"\n仿真复现边界：{reference_warning}"
                          if reference_warning else "")
        legacy_line = ""
        if legacy.get("verdict") == "diagnostic_only":
            park = dict(legacy.get("park_reconstruction", {}))
            r_range = legacy.get("resistance_ohm_range", ())
            l_range = legacy.get("shared_inductance_mh_range", ())
            if len(r_range) == 2 and len(l_range) == 2:
                legacy_line = (
                    "\n旧格式证据（仅诊断）："
                    f"相电流重建Iq相关={float(park.get('correlation', float('nan'))):.6f}，"
                    f"RMSE={float(park.get('iq_rmse_a', float('nan'))):.4g} A；"
                    f"升速拟合R范围={float(r_range[0]):.4g}～{float(r_range[1]):.4g} Ω，"
                    f"共享L范围={float(l_range[0]):.4g}～{float(l_range[1]):.4g} mH；"
                    "范围不稳定，不能作为Ld/Lq收敛值。")
        self._status.setText(
            f"{os.path.basename(self._source_path)}｜{dataset['count']} 点｜"
            f"{dataset['rate_hz']} Hz｜{duration:.3f} s｜"
            f"RLS更新 {int(final.get('updates', 0))}｜{current_note}｜{verdict}\n"
            f"真机ESO：Lnom=0.66mH，ωo=4000，1拍电压延迟→ARX(3)，"
            f"λ=1，P0=1e6｜电压源={analysis.get('voltage_source', '--')}｜"
            f"末值Σa(d/q)={sum(map(float, final.get('theta_d', ())[:3])):.6g}/"
            f"{sum(map(float, final.get('theta_q', ())[:3])):.6g}，"
            f"b0(d/q)={float(final.get('theta_d', (float('nan'),) * 7)[3]):.6g}/"
            f"{float(final.get('theta_q', (float('nan'),) * 7)[5]):.6g} A/V；"
            "高阶ARX不直接换算物理R/L"
            f"{diagnostic_line}{physical_line}{legacy_line}{reference_line}")
        self._status.setStyleSheet(
            "color:#81c784;" if raw_finite and physical_usable and nominal_match else
            "color:#ffb74d;" if raw_finite else "color:#ff8a80;")
        self._btn_coeff.setEnabled(True)

    def _show_coefficients(self) -> None:
        self._coeff_dialog.show()
        self._coeff_dialog.raise_()
        self._coeff_dialog.activateWindow()


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
                "ib_a", "vd_raw", "vq_raw", "vbus_v",
                "vd_applied_v", "vq_applied_v")
        }
        self._high_rate_voltage_is_applied = False
        self._high_rate_rate_hz = 200
        # F1每个FOC样本都携带speed_rpm，但编码器速度只在500 Hz中频任务
        # 更新。用跨批次相位连续的抽取器恢复真实速度时间基准，不能把
        # 16 kHz零阶保持的重复值当成独立测速样本。
        self._speed_f1_source_rate_hz = 0
        self._speed_f1_decimation_phase = 0
        self._speed_curve_source = "none"
        self._f0_speed_pending = False
        self._last_high_angle_time = 0.0
        self._last_telemetry_time: float = 0.0
        self._latest_vbus_v = 0.0
        self._latest_rls: dict = {}
        self._last_rls_updates: int | None = None
        # 绘图只留 5000 点；辨识数据用紧凑分批缓冲单独保留
        # 最近 60 s，避免 16 kHz 下用 Python 逐点对象占用过多内存。
        self._rls_capture = RlsCaptureBuffer(max_seconds=60.0)
        self._rls_capture_active = True
        self._rls_capture_had_probe = False
        self._last_rls_probe_state = False

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
        self._btn_save_all = QPushButton("保存所有波形")
        self._btn_save_all.clicked.connect(self._save_all_curves)
        title_row.addWidget(self._btn_save_all)
        self._save_worker = None
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
        self._c_speed = TrendCurve(
            "转速 rpm", {"实际": "#4fc3f7", "给定": "#ffb74d"},
            y_label="rpm", buffer_size=30000)
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
        # 新 F1/40 由最终PWM比较值重构平均施加电压；旧帧回退到
        # 受限PI命令的伏特换算，但会在数据源说明中明确标注。
        self._c_voltage = TrendCurve(
            "PWM平均施加电压 Vd/Vq", {"Vd": "#80cbc4", "Vq": "#ffab91"},
            y_label="V", buffer_size=5000)
        # 上位机 C++ 在线 ARX/RLS：主页面只显示原始ARX摘要。一般的
        # ARX(3,2输入)不能唯一映射成物理R/L；物理结果只由离线闭环IV给出。
        self._c_rls_L = TrendCurve(
            "ARX 本轴输入系数 b0 (A/V)",
            {"b0_d": "#4db6ac", "b0_q": "#ff8a65"},
            y_label="A/V")
        self._c_rls_a1 = TrendCurve(
            "ARX 分母系数和 Σa（一阶参考≈0.944）",
            {"Σa_d": "#4fc3f7", "Σa_q": "#ba68c8"},
            y_label="")
        self._c_rls_R = TrendCurve(
            "ARX 本轴延迟输入系数 b1 (A/V)",
            {"b1_d": "#81c784", "b1_q": "#f06292"},
            y_label="A/V")
        self._rls_coeff_dialog = _RlsCoefficientDialog(self)
        self._offline_rls_dialog = _OfflineRlsDialog(self)

        # 慢速量没有额外的上位机采集滤波；F1 高速量会在收到
        # 数据后根据当前的“原始连续流 / 固件箱式平均”模式覆盖。
        self._c_speed.set_source_processing(
            "等待F1速度；无F1时降级为F0 10 Hz单帧记录")
        for curve in (self._c_torque, self._c_sensor_q, self._c_position,
                      self._c_position_speed, self._c_position_state):
            curve.set_source_processing("F0常规遥测，上位机不做采集滤波")
        for curve in (self._c_rls_a1, self._c_rls_L, self._c_rls_R):
            curve.set_source_processing(
                "上位机C++真机匹配ESO（16kHz/Lnom=0.66mH/ωo=4000）"
                "→三阶RLS（λ=1，P0=1e6）；"
                "显示原始ARX摘要，不作为物理R/L")
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
            "上位机RLS：0帧｜在通信页启用辨识档后会自动启动")
        self._rls_status.setStyleSheet("color:#90a4ae;")
        self._rls_status.setToolTip(
            "橙色=真机匹配ESO/ARX递推数值有效，但尚未形成物理R/L证据；"
            "红色=原始系数数值无效。物理绿色只在离线独立激励IV验证通过时显示。")
        self._rls_status.setWordWrap(True)
        self._rls_status.setMaximumHeight(58)
        rls_bar = QHBoxLayout()
        rls_bar.addWidget(self._rls_status, 1)
        self._btn_enable_rls = QPushButton("重置辨识")
        self._btn_enable_rls.setToolTip(
            "在上位机C++核心中运行RLS；固件仅发送F1采样，不再计算RLS")
        self._btn_enable_rls.clicked.connect(self._on_enable_rls)
        self._btn_rls_coefficients = QPushButton("完整系数(7×2) ↗")
        self._btn_rls_coefficients.setToolTip(
            "弹出 d/q 轴各7个 ARX 原始系数的完整波形，不改变主页高度")
        self._btn_rls_coefficients.clicked.connect(
            self._show_rls_coefficients)
        self._btn_offline_rls = QPushButton("离线辨识 CSV…")
        self._btn_offline_rls.setToolTip(
            "导入保存的 RLS辨识数据.csv，在后台按真机ESO配置重放；"
            "新F1/40数据优先使用PWM占空比重构电压，并执行独立"
            "激励闭环IV物理验证；不需要连接电机")
        self._btn_offline_rls.clicked.connect(self._select_offline_rls_csv)
        self._btn_analyze_rls_capture = QPushButton("分析当前采集")
        self._btn_analyze_rls_capture.setToolTip(
            "直接分析关闭辨识激励后冻结的内存数据；不先写CSV，不阻塞界面")
        self._btn_analyze_rls_capture.clicked.connect(
            self._analyze_current_rls_capture)
        self._probe_amplitude = QDoubleSpinBox()
        self._probe_amplitude.setRange(0.02, 0.15)
        self._probe_amplitude.setSingleStep(0.01)
        self._probe_amplitude.setDecimals(2)
        self._probe_amplitude.setValue(0.12)
        self._probe_amplitude.setSuffix(" A")
        self._probe_amplitude.setToolTip(
            "d/q轴同时注入的独立PRBS幅值；0.12 A为真机噪声压力测试后的默认值；"
            "运行中不允许改幅值")
        self._probe_amplitude.editingFinished.connect(
            self._on_probe_amplitude_edited)
        self._btn_rls_probe = QPushButton("辨识激励：关")
        self._btn_rls_probe.setCheckable(True)
        self._btn_rls_probe.setToolTip(
            "默认关闭。注入受限小幅PRBS参考，使闭环下的R/L可用工具变量辨识；"
            "停机/掉线/保护动作后固件自动关闭。")
        self._btn_rls_probe.clicked.connect(self._on_toggle_rls_probe)
        rls_bar.addWidget(self._btn_offline_rls)
        rls_bar.addWidget(self._btn_analyze_rls_capture)
        rls_bar.addWidget(self._btn_rls_coefficients)
        rls_bar.addWidget(self._probe_amplitude)
        rls_bar.addWidget(self._btn_rls_probe)
        rls_bar.addWidget(self._btn_enable_rls)
        rls_v.addLayout(rls_bar)
        rls_curve_h = QHBoxLayout()
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_a1, "ARX Σa（一阶参考≈0.944）"))
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_L, "本轴输入系数 b0 (A/V)"))
        rls_curve_h.addWidget(_make_curve_panel(
            self._c_rls_R, "本轴延迟输入系数 b1 (A/V)"))
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
        comm.statusChanged.connect(self._on_connection_status_changed)

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
        self._f0_speed_pending = True
        self._last_telemetry_time = datetime.datetime.now().timestamp()
        filter_state = (
            bool(getattr(frame, "current_filter_enabled", False)),
            int(getattr(frame, "current_filter_alpha_q15", 0) or 0),
        )
        if filter_state != getattr(self, "_last_firmware_filter_state", None):
            self._last_firmware_filter_state = filter_state
            self._update_f1_processing_labels(self._high_rate_rate_hz)
        if hasattr(self, "_btn_rls_probe"):
            probe_enabled = bool(getattr(frame, "rls_probe_enabled", False))
            if probe_enabled:
                if not self._last_rls_probe_state:
                    # The command being queued is not proof that the firmware
                    # accepted it.  Start the physical capture only after the
                    # device reports the probe active; this also excludes the
                    # unexcited command/ACK latency prefix.
                    self._rls_capture.clear()
                    self._rls_capture_active = True
                self._rls_capture_had_probe = True
            if self._last_rls_probe_state and not probe_enabled:
                # Freeze exactly the injected interval.  Otherwise the normal
                # current that arrives while the operator reaches the Save
                # button dilutes the late Welch windows and falsely looks like
                # a loss of excitation.
                self._rls_capture_active = False
            self._last_rls_probe_state = probe_enabled
            if self._btn_rls_probe.isChecked() != probe_enabled:
                self._btn_rls_probe.blockSignals(True)
                self._btn_rls_probe.setChecked(probe_enabled)
                self._btn_rls_probe.blockSignals(False)
            self._btn_rls_probe.setText(
                "辨识激励：开" if probe_enabled else "辨识激励：关")
            self._probe_amplitude.setEnabled(
                not probe_enabled and int(getattr(frame, "mc_state", 0)) != 6)
            amplitude_digit = int(getattr(
                frame, "rls_probe_amplitude_digit", 0) or 0)
            if (amplitude_digit > 0 and not probe_enabled and
                    not self._probe_amplitude.hasFocus()):
                self._probe_amplitude.setValue(
                    amplitude_digit * F1_CURRENT_A_PER_DIGIT)
        if (not self._comm.is_sim_running() and not self._comm.is_connected() and
                abs(frame.speed_actual) < 1e-9 and abs(frame.angle_actual) < 1e-9):
            self._angle_dial.reset()

    def _on_connection_status_changed(self, connected: bool, _message: str) -> None:
        """断链时冻结已注入数据；重连后的普通流不得污染它。"""
        if connected or not self._rls_capture_had_probe:
            return
        self._rls_capture_active = False
        self._last_rls_probe_state = False
        if hasattr(self, "_btn_rls_probe"):
            self._btn_rls_probe.blockSignals(True)
            self._btn_rls_probe.setChecked(False)
            self._btn_rls_probe.setText("辨识激励：关")
            self._btn_rls_probe.blockSignals(False)
        if self._rls_capture.sample_count > 0:
            self._rls_status.setText(
                f"通信断开；已冻结 {self._rls_capture.duration_s:.3f} s "
                "辨识数据，重连后可直接分析")
            self._rls_status.setStyleSheet("color:#ffcc80;")

    def _on_high_rate_telemetry(self, sample: dict, *, capture=True) -> None:
        if capture and self._rls_capture_active:
            capture_columns = {
                "count": 1,
                "rate_hz": int(sample.get("rate_hz", 200)),
                "tick_ms": [int(sample.get("tick_ms", 0))],
                **{name: [float(sample.get(name, 0.0))]
                   for name in (
                       "angle_deg", "speed_rpm", "iq_a", "iqref_a",
                       "ia_a", "ib_a", "vd_raw", "vq_raw", "vbus_v")},
            }
            if "id_a" in sample:
                capture_columns["id_a"] = [float(sample["id_a"])]
                capture_columns["idref_a"] = [float(
                    sample.get("idref_a", 0.0))]
                capture_columns["id_source_direct"] = bool(
                    sample.get("id_source_direct", True))
            if "sample_seq" in sample:
                capture_columns["sample_seq"] = [int(sample["sample_seq"])]
                capture_columns["sequence_source_direct"] = bool(
                    sample.get("sequence_source_direct", True))
            if "vdda_v" in sample:
                capture_columns["vdda_v"] = [float(sample["vdda_v"])]
                capture_columns["vdda_source_direct"] = bool(
                    sample.get("vdda_source_direct", True))
            if all(name in sample for name in (
                    "actuation_angle_deg", "duty_a", "duty_b", "duty_c",
                    "vd_applied_v", "vq_applied_v")):
                for name in ("actuation_angle_deg", "vd_applied_v",
                             "vq_applied_v"):
                    capture_columns[name] = [float(sample[name])]
                for name in ("duty_a", "duty_b", "duty_c"):
                    capture_columns[name] = [int(sample[name])]
                capture_columns["applied_voltage_source_direct"] = bool(
                    sample.get("applied_voltage_source_direct", True))
            self._rls_capture.append_columns(capture_columns)
        self._high_rate_samples.append({
            "angle_deg": float(sample["angle_deg"]),
            "speed_rpm": float(sample.get("speed_rpm", 0.0)),
            "iq_a": float(sample["iq_a"]),
            "iqref_a": float(sample["iqref_a"]),
            "ia_a": float(sample.get("ia_a", 0.0)),
            "ib_a": float(sample.get("ib_a", 0.0)),
            # 施加电压原始码值（旧固件缺省 0）+ 母线电压，供离线卡尔曼建模。
            "vd_raw": float(sample.get("vd_raw", 0.0)),
            "vq_raw": float(sample.get("vq_raw", 0.0)),
            "vbus_v": float(sample.get("vbus_v", 0.0)),
            "vd_applied_v": float(sample.get("vd_applied_v", 0.0)),
            "vq_applied_v": float(sample.get("vq_applied_v", 0.0)),
            "applied_voltage_source_direct": bool(
                sample.get("applied_voltage_source_direct", False)),
            "rate_hz": int(sample.get("rate_hz", 200)),
        })
        self._high_rate_voltage_is_applied = bool(
            sample.get("applied_voltage_source_direct", False))
        self._latest_vbus_v = float(sample.get("vbus_v", self._latest_vbus_v))
        self._last_high_angle_time = time.time()

    def _extract_f1_speed(self, values, source_rate_hz: int) -> tuple[list[float], int]:
        """把F1中的零阶保持速度恢复为测速器真实更新节拍。

        标准连续流只允许1/2/4/8/16 kHz，均可整除500 Hz；兼容200 Hz
        已低于测速器更新率，直接保留。抽取相位跨UI批次保持连续，避免
        每33 ms重新从批首取点而制造时间轴抖动。
        """
        source_rate = max(1, int(source_rate_hz))
        samples = [float(value) for value in values]
        if not samples:
            return [], min(500, source_rate)
        if source_rate <= 500:
            self._speed_f1_source_rate_hz = source_rate
            self._speed_f1_decimation_phase = 0
            return samples, source_rate

        stride = max(1, round(source_rate / 500.0))
        if self._speed_f1_source_rate_hz != source_rate:
            self._speed_f1_source_rate_hz = source_rate
            self._speed_f1_decimation_phase = 0
        phase = self._speed_f1_decimation_phase
        selected = []
        for value in samples:
            if phase == 0:
                selected.append(value)
            phase += 1
            if phase >= stride:
                phase = 0
        self._speed_f1_decimation_phase = phase
        return selected, max(1, round(source_rate / stride))

    def _on_high_rate_telemetry_batch(self, samples: list[dict]) -> None:
        if samples and self._rls_capture_active:
            capture_columns = {
                "count": len(samples),
                "rate_hz": int(samples[-1].get("rate_hz", 200)),
                "tick_ms": [int(sample.get("tick_ms", 0))
                            for sample in samples],
                **{name: [float(sample.get(name, 0.0))
                          for sample in samples]
                   for name in (
                       "angle_deg", "speed_rpm", "iq_a", "iqref_a",
                       "ia_a", "ib_a", "vd_raw", "vq_raw", "vbus_v")},
            }
            if all("id_a" in sample for sample in samples):
                capture_columns["id_a"] = [float(sample["id_a"])
                                             for sample in samples]
                capture_columns["idref_a"] = [float(
                    sample.get("idref_a", 0.0)) for sample in samples]
                capture_columns["id_source_direct"] = all(
                    bool(sample.get("id_source_direct", True))
                    for sample in samples)
            if all("sample_seq" in sample for sample in samples):
                capture_columns["sample_seq"] = [
                    int(sample["sample_seq"]) for sample in samples]
                capture_columns["sequence_source_direct"] = all(
                    bool(sample.get("sequence_source_direct", True))
                    for sample in samples)
            if all("vdda_v" in sample for sample in samples):
                capture_columns["vdda_v"] = [
                    float(sample["vdda_v"]) for sample in samples]
                capture_columns["vdda_source_direct"] = all(
                    bool(sample.get("vdda_source_direct", True))
                    for sample in samples)
            applied_names = (
                "actuation_angle_deg", "duty_a", "duty_b", "duty_c",
                "vd_applied_v", "vq_applied_v")
            if all(all(name in sample for name in applied_names)
                   for sample in samples):
                for name in ("actuation_angle_deg", "vd_applied_v",
                             "vq_applied_v"):
                    capture_columns[name] = [
                        float(sample[name]) for sample in samples]
                for name in ("duty_a", "duty_b", "duty_c"):
                    capture_columns[name] = [
                        int(sample[name]) for sample in samples]
                capture_columns["applied_voltage_source_direct"] = all(
                    bool(sample.get("applied_voltage_source_direct", True))
                    for sample in samples)
            self._rls_capture.append_columns(capture_columns)
        for sample in samples:
            self._on_high_rate_telemetry(sample, capture=False)

    def _on_high_rate_telemetry_columns(self, columns: dict) -> None:
        count = int(columns.get("count", 0))
        if count <= 0:
            return
        if self._rls_capture_active:
            self._rls_capture.append_columns(columns)
        for name, buffer in self._high_rate_columns.items():
            values = columns.get(name, ())
            buffer.extend(values[:count])
        self._high_rate_voltage_is_applied = bool(
            columns.get("applied_voltage_source_direct", False))
        self._high_rate_rate_hz = max(1, int(columns.get("rate_hz", 200)))
        self._update_f1_processing_labels(self._high_rate_rate_hz)
        vbus = columns.get("vbus_v", ())
        if vbus:
            self._latest_vbus_v = float(vbus[min(count, len(vbus)) - 1])
        self._last_high_angle_time = time.time()

    def _on_rls_coeff(self, sample: dict) -> None:
        """上位机在线辨识系数：忠实显示模型同构RLS原始摘要。"""
        self._latest_rls = sample
        self._rls_rx_frames = getattr(self, "_rls_rx_frames", 0) + 1
        updates = int(sample.get("updates", 0))
        held = (self._last_rls_updates is not None and
                updates == self._last_rls_updates)
        self._last_rls_updates = updates
        innov = sample.get("innov_rms_a", sample.get("innov_rms_digit", 0.0))
        p_trace = sample.get("p_trace", 0.0)
        a1_d, a1_q = sample.get("a1_d"), sample.get("a1_q")
        theta_d = sample.get("theta_d")
        theta_q = sample.get("theta_q")
        self._rls_coeff_dialog.append_coefficients(theta_d, theta_q)
        try:
            asum_d = sum(map(float, theta_d[:3]))
            asum_q = sum(map(float, theta_q[:3]))
        except (TypeError, IndexError):
            asum_d, asum_q = a1_d, a1_q
        def _number(x):
            return isinstance(x, (int, float)) and math.isfinite(float(x))

        def _fmt(x, spec=".4g"):
            return format(float(x), spec) if _number(x) else "NaN/Inf"

        try:
            b0_d, b1_d = float(theta_d[3]), float(theta_d[4])
            b0_q, b1_q = float(theta_q[5]), float(theta_q[6])
        except (TypeError, ValueError, IndexError):
            b0_d = b1_d = b0_q = b1_q = float("nan")
        rejected = []
        if theta_d is not None and theta_q is not None:
            try:
                coefficients = [*theta_d[:7], *theta_q[:7]]
            except (TypeError, IndexError):
                rejected.append("ARX系数数组不完整")
            else:
                if len(coefficients) != 14 or not all(
                        _number(value) for value in coefficients):
                    rejected.append("ARX系数含NaN/Inf或数组不完整")
        elif not (_number(b0_d) and _number(b0_q)):
            rejected.append("本轴电压系数含NaN/Inf")

        if "p_trace" in sample and not (
                _number(p_trace) and float(p_trace) >= 0.0):
            rejected.append(f"P迹无效({_fmt(p_trace)})")
        if not (_number(innov) and float(innov) >= 0.0):
            rejected.append(f"创新RMS无效({_fmt(innov)})")

        # 保持原因稳定且简洁，避免同一问题由a1和完整theta重复刷屏。
        rejected = list(dict.fromkeys(rejected))

        arx_summary_available = all(
            _number(value) for value in (b0_d, b1_d, b0_q, b1_q))
        if rejected:
            reason = "；".join(rejected)
            validity = "整帧无效"
            status_color = "#ff8a80"
        else:
            reason = ("ARX摘要可计算" if arx_summary_available else
                      "ARX摘要暂不可计算；7×2原始系数仍有效")
            validity = "模型同构数值有效（不代表物理R/L收敛）"
            # 物理绿色只属于离线独立激励IV验证。在线ARX即使数值有限，
            # 也只证明递推正常，因此保持警示橙色。
            status_color = "#ffb74d"
        self._rls_status.setText(
            f"本地结果：{self._rls_rx_frames}帧｜RLS更新：{updates}｜"
            f"递推：{'保持(未收到新递推)' if held else '更新'}｜"
            f"{validity}：{reason}\n"
            f"真机ESO：Lnom=0.66mH/ωo=4000/1拍电压延迟"
            f"→ARX(3)/λ=1/P0=1e6/无投影｜"
            f"原始SI：a1={_fmt(a1_d)}/{_fmt(a1_q)}｜"
            f"Σa={_fmt(asum_d)}/{_fmt(asum_q)}｜"
            f"本轴b0={_fmt(b0_d)}/{_fmt(b0_q)} A/V｜"
            f"本轴b1={_fmt(b1_d)}/{_fmt(b1_q)} A/V｜"
            f"innov={_fmt(innov)} A｜P迹={_fmt(p_trace)}")
        self._rls_status.setStyleSheet(f"color:{status_color};")

        # 原始14系数有限就持续画；不在在线图中伪装成物理R/L。
        if not rejected:
            self._c_rls_a1.append(
                {"Σa_d": asum_d, "Σa_q": asum_q},
                redraw=self._curve_is_active(self._c_rls_a1))
            self._c_rls_L.append(
                {"b0_d": b0_d, "b0_q": b0_q},
                redraw=self._curve_is_active(self._c_rls_L))
            self._c_rls_R.append(
                {"b1_d": b1_d, "b1_q": b1_q},
                redraw=self._curve_is_active(self._c_rls_R))

    def _show_rls_coefficients(self) -> None:
        self._rls_coeff_dialog.show()
        self._rls_coeff_dialog.raise_()
        self._rls_coeff_dialog.activateWindow()

    def _select_offline_rls_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载离线 RLS 辨识数据", "",
            "RLS 辨识数据 (*.csv);;所有文件 (*)")
        if path:
            self._offline_rls_dialog.start(path)

    def _analyze_current_rls_capture(self) -> None:
        """直接分析冻结的F1/40内存快照，避免大CSV往返。"""
        snapshot = self._rls_capture.snapshot()
        problems = []
        if self._rls_capture_active:
            problems.append("请先关闭辨识激励，使采集区冻结")
        if not self._rls_capture_had_probe:
            problems.append("当前采集未记录到辨识PRBS开启状态")
        if snapshot.rate_hz != 16000:
            problems.append(f"采样率为{snapshot.rate_hz} Hz，不是16 kHz")
        if snapshot.duration_s < 3.0:
            problems.append(
                f"有效时长仅{snapshot.duration_s:.3f} s，至少需要3 s")
        if snapshot.dropped_samples:
            problems.append(
                f"主机环形缓冲已丢弃{snapshot.dropped_samples}点")
        if (not snapshot.batches or not all(
                all(name in batch for name in
                    ("id_a", "idref_a", "sample_seq"))
                for batch in snapshot.batches)):
            problems.append("数据不是含Id/IdRef/连续序号的新F1格式")
        if problems:
            QMessageBox.warning(
                self, "当前采集不能辨识", "\n".join(problems))
            return
        label = f"当前冻结采集（{snapshot.duration_s:.3f} s）"
        self._offline_rls_dialog.start_snapshot(snapshot, label)

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
        ia = (np.asarray(data.get("ia", []), dtype=float) *
              F1_CURRENT_A_PER_DIGIT)   # 码值→A
        ib = (np.asarray(data.get("ib", []), dtype=float) *
              F1_CURRENT_A_PER_DIGIT)
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
            self._c_rls_R: 1,
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
            ("RLS 本轴b0", self._c_rls_L, 3),
            ("RLS 本轴b1", self._c_rls_R, 3),
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
        self._c_phase_current.set_source_processing(
            current_processing, rate_hz)
        voltage_source = (
            "最终PWM比较值+Vbus+执行Park角重构的平均dq电压"
            if self._high_rate_voltage_is_applied else
            "兼容回退：受限PI电压命令换算（非最终PWM电压）")
        self._c_voltage.set_source_processing(
            f"{current_processing}；{voltage_source}", rate_hz)
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
            ("PWM平均施加电压 Vd", "vd"),
            ("PWM平均施加电压 Vq", "vq"),
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
            "vd": (self._c_voltage, "Vd", "V"),
            "vq": (self._c_voltage, "Vq", "V"),
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
        self._rls_capture.clear()
        self._rls_capture_had_probe = bool(
            hasattr(self, "_btn_rls_probe") and
            self._btn_rls_probe.isChecked())
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
        self._last_rls_updates = None
        self._rls_coeff_dialog.clear()
        self._rls_status.setText(
            "上位机RLS：0帧｜在通信页启用辨识档后会自动启动")
        self._rls_status.setStyleSheet("color:#90a4ae;")
        self._last_high_angle_time = 0.0
        self._speed_f1_source_rate_hz = 0
        self._speed_f1_decimation_phase = 0
        self._speed_curve_source = "none"
        self._f0_speed_pending = False
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

    def _on_toggle_rls_probe(self, checked: bool) -> None:
        """显式切换真机辨识激励；从不随RLS按钮隐式启用。"""
        if not self._comm.is_connected():
            self._btn_rls_probe.blockSignals(True)
            self._btn_rls_probe.setChecked(not checked)
            self._btn_rls_probe.blockSignals(False)
            QMessageBox.warning(self, "无法下发", "请先连接真实F407控制器。")
            return
        running = int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6
        parts = [f"rls_probe_enabled={1 if checked else 0}"]
        if checked and not running:
            parts.extend((
                f"rls_probe_amplitude_a={self._probe_amplitude.value():.3f}",
                "rls_probe_chip_divider=8",
            ))
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(
            CMD_SET_PARAMS, ";".join(parts).encode("utf-8")))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        if not (sent or pending_after > pending_before):
            self._btn_rls_probe.blockSignals(True)
            self._btn_rls_probe.setChecked(not checked)
            self._btn_rls_probe.blockSignals(False)
            QMessageBox.warning(self, "未发送", "设备未接受辨识激励命令。")
            return
        self._btn_rls_probe.setText(
            "辨识激励：开" if checked else "辨识激励：关")
        self._probe_amplitude.setEnabled(not checked and not running)
        if checked:
            self._rls_capture.clear()
            self._rls_capture_active = False
            self._rls_capture_had_probe = False
            self._comm.start_host_rls()
            self._rls_status.setText(
                "真机辨识激励命令已提交；等待固件确认后开始采集，"
                "确认后请连续运行5～10 s")
            self._rls_status.setStyleSheet("color:#ffcc80;")
        else:
            self._rls_capture_active = False
            self._rls_status.setText(
                f"辨识激励已关闭；已冻结 {self._rls_capture.duration_s:.3f} s "
                "同步数据，可直接分析或保存CSV")
            self._rls_status.setStyleSheet("color:#ffcc80;")

    def _on_probe_amplitude_edited(self) -> None:
        """停机时预配置激励幅值；不隐式开启PRBS。"""
        if (not self._comm.is_connected() or
                self._btn_rls_probe.isChecked() or
                int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6):
            return
        payload = (
            f"rls_probe_amplitude_a={self._probe_amplitude.value():.3f};"
            "rls_probe_chip_divider=8").encode("utf-8")
        self._comm.send_frame(encode_frame(CMD_SET_PARAMS, payload))

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
            voltage_is_applied = all(bool(sample.get(
                "applied_voltage_source_direct", False))
                for sample in high_rate)
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
                high_columns["vd_applied_v"].append(
                    float(sample.get("vd_applied_v", 0.0)))
                high_columns["vq_applied_v"].append(
                    float(sample.get("vq_applied_v", 0.0)))
            self._high_rate_rate_hz = max(
                1, int(high_rate[-1].get("rate_hz", 200)))
            self._update_f1_processing_labels(self._high_rate_rate_hz)
        else:
            voltage_is_applied = self._high_rate_voltage_is_applied
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
            speed_values = (high_columns["speed_rpm"]
                            if len(high_columns["speed_rpm"]) == high_count
                            else [])
            f1_speed, speed_rate_hz = self._extract_f1_speed(
                speed_values, self._high_rate_rate_hz)
            if f1_speed:
                if (self._speed_curve_source != "f1" or
                    (self._c_speed._sample_rate_hz > 0.0 and
                     abs(self._c_speed._sample_rate_hz - speed_rate_hz) > 0.5)):
                    # 不允许F0不规则时间轴与F1固定500 Hz时间轴混在同一
                    # FFT缓冲中；F1档位改变采样率时也重新建立时间基准。
                    self._c_speed.clear()
                    self._speed_curve_source = "f1"
                self._c_speed.set_source_processing(
                    f"F1连续流携带速度；按测速器500 Hz更新节拍从"
                    f"{self._high_rate_rate_hz:g} Hz传输流抽取",
                    speed_rate_hz)
                self._c_speed.append_columns({
                    "实际": f1_speed,
                    # 给定转速来自F0；在F1速度时间轴上作零阶保持，便于
                    # 时域对照。它不是500 Hz产生的新给定样本。
                    "给定": [float(f.speed_target)] * len(f1_speed),
                }, 1.0 / speed_rate_hz,
                    redraw=self._curve_is_active(self._c_speed))
                self._f0_speed_pending = False
            elif (self._f0_speed_pending and
                  time.time() - self._last_high_angle_time > 1.0):
                # 串口无F1或仿真模式降级：每个F0帧只写一次，绝不再按
                # 30 Hz UI定时器重复写入同一个慢遥测值。
                if self._speed_curve_source != "f0":
                    self._c_speed.clear()
                    self._c_speed.reset_sample_rate()
                    self._speed_curve_source = "f0"
                self._c_speed.set_source_processing(
                    "无F1速度，降级为F0常规遥测单帧记录（标称10 Hz）")
                self._c_speed.append(
                    {"实际": f.speed_actual, "给定": f.speed_target},
                    redraw=self._curve_is_active(self._c_speed))
                self._f0_speed_pending = False
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
                if (voltage_is_applied and
                        len(high_columns["vd_applied_v"]) == high_count and
                        len(high_columns["vq_applied_v"]) == high_count):
                    voltage_d = high_columns["vd_applied_v"]
                    voltage_q = high_columns["vq_applied_v"]
                else:
                    scale = [float(vbus) / (math.sqrt(3.0) * 32768.0)
                             for vbus in high_columns["vbus_v"]]
                    voltage_d = [raw * factor for raw, factor in zip(
                        high_columns["vd_raw"], scale)]
                    voltage_q = [raw * factor for raw, factor in zip(
                        high_columns["vq_raw"], scale)]
                self._c_voltage.append_columns({
                    "Vd": voltage_d, "Vq": voltage_q,
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
        if self._save_worker is not None and self._save_worker.isRunning():
            QMessageBox.information(self, "正在保存", "上一份波形数据仍在后台写盘。")
            return
        if not any(len(item["times"])
                   for item in self._curve_csv_snapshot()):
            QMessageBox.warning(self, "提示", "暂无波形数据")
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
        # Qt/pyqtgraph 的离屏渲染必须在 GUI 线程完成；其余大文件
        # 写盘全部移交工作线程。
        png = self.render_waveforms_png()
        if not png:
            QMessageBox.warning(self, "提示", "无法渲染波形图（或未安装 pyqtgraph）")
            return
        csv_path = os.path.join(os.path.dirname(path), "原始数据.csv")
        rls_csv_path = os.path.join(
            os.path.dirname(path), "RLS辨识数据.csv")
        curve_snapshot = self._curve_csv_snapshot()
        rls_snapshot = self._rls_capture.snapshot()
        self._save_worker = _WaveformSaveWorker(
            path, png, csv_path, curve_snapshot, rls_csv_path,
            rls_snapshot, self)
        self._save_worker.completed.connect(self._on_waveform_save_complete)
        self._btn_save_all.setEnabled(False)
        self._btn_save_all.setText("后台保存中…")
        self._datasrc_label.setText("[ 波形正在后台写盘 ]")
        self._datasrc_label.setStyleSheet(
            "color:#ffcc80; font-weight:bold;")
        self._save_worker.start()

    def _on_waveform_save_complete(self, result: dict) -> None:
        self._btn_save_all.setEnabled(True)
        self._btn_save_all.setText("保存所有波形")
        error = str(result.get("error", ""))
        if error:
            self._datasrc_label.setText("[ 波形保存失败 ]")
            self._datasrc_label.setStyleSheet(
                "color:#ff8a80; font-weight:bold;")
            QMessageBox.warning(self, "保存失败", error)
            return
        path = str(result["png_path"])
        csv_path = str(result["csv_path"])
        rls_csv_path = str(result["rls_csv_path"])
        self._datasrc_label.setText("[ 波形后台保存完成 ]")
        self._datasrc_label.setStyleSheet(
            "color:#69f0ae; font-weight:bold;")
        rls_note = (
            f"离线RLS：{os.path.basename(rls_csv_path)}\n"
            f"  {int(result['rls_count'])} 点 / "
            f"{float(result['rls_duration_s']):.3f} s"
            if result.get("rls_exported") else
            "离线RLS：未收到含电压和母线电压的同步F1帧")
        QMessageBox.information(
            self, "保存成功",
            f"本次实验已独立保存到：\n{os.path.dirname(path)}\n\n"
            f"波形图：{os.path.basename(path)}\n"
            f"原始数据：{os.path.basename(csv_path)}\n{rls_note}")

    def _write_rls_capture_csv(self, path: str) -> None:
        """导出可由 C++ RLS 逐帧重放的对齐宽表。"""
        self._rls_capture.write_csv(path)

    def _curve_csv_snapshot(self) -> list[dict]:
        """在 GUI 线程快速固化曲线，供后台写盘。"""
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
            (self._c_rls_L, "rls_own_b0_a_per_v"),
            (self._c_rls_R, "rls_own_b1_a_per_v"),
            (self._rls_coeff_dialog._d_curve, "rls_theta_d"),
            (self._rls_coeff_dialog._q_curve, "rls_theta_q"),
        ]
        snapshot = []
        for curve, channel in curves:
            snapshot_meta = curve.raw_snapshot(next(iter(curve._buffers)))
            snapshot.append({
                "channel": channel,
                "times": tuple(curve._times),
                "series": tuple(
                    (series, tuple(values))
                    for series, values in curve._buffers.items()),
                "sampling_rate_hz": float(snapshot_meta["sample_rate_hz"]),
                "source_filter": str(snapshot_meta["source_processing"]),
                "display_filter": str(snapshot_meta["display_filter"]),
                "unit": curve._y_label,
            })
        return snapshot

    def _write_curves_csv(self, path: str) -> None:
        """按原始采样时间导出所有曲线，不对不同采样率做伪对齐。"""
        _write_curve_csv_snapshot(path, self._curve_csv_snapshot())

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
