"""监控页面：实时数据、统计、趋势曲线。"""
import datetime
import os
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from config.config import MONITOR_REFRESH_MS
from widgets.trend_curve import TrendCurve


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
        self._btn_start = QPushButton("启动"); self._btn_start.setObjectName("PrimaryButton")
        self._btn_stop = QPushButton("停止")
        self._btn_emerg = QPushButton("紧急停止"); self._btn_emerg.setObjectName("EmergencyButton")
        self._btn_sim = QPushButton("启动仿真")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_emerg.clicked.connect(self._on_emergency)
        self._btn_sim.clicked.connect(self._on_toggle_sim)
        self._sim_running = False
        for b in (self._btn_sim, self._btn_start, self._btn_stop, self._btn_emerg):
            title_row.addWidget(b)
        root.addLayout(title_row)

        # ---------- 实时数据 ----------
        rt_box = QGroupBox("实时数据")
        rt_grid = QGridLayout(rt_box)
        self._speed_actual = _DataItem("实际转速", "rpm")
        self._speed_target = _DataItem("给定转速", "rpm")
        self._current_actual = _DataItem("实际电流", "A")
        self._current_target = _DataItem("给定电流", "A")
        self._torque_actual = _DataItem("实际转矩", "Nm")
        self._torque_target = _DataItem("给定转矩", "Nm")
        self._angle_actual = _DataItem("实际角度", "°")
        self._angle_raw = _DataItem("原始角度/计数", "")

        for col, w in enumerate([self._speed_actual, self._speed_target,
                                 self._current_actual, self._current_target]):
            rt_grid.addWidget(w, 0, col)
        for col, w in enumerate([self._torque_actual, self._torque_target,
                                 self._angle_actual, self._angle_raw]):
            rt_grid.addWidget(w, 1, col)
        root.addWidget(rt_box)

        # ---------- 传感器状态 ----------
        sensor_box = QGroupBox("传感器状态")
        sensor_grid = QGridLayout(sensor_box)
        self._sensor_source = QLabel("来源：--")
        self._sensor_quality = QProgressBar()
        self._sensor_quality.setRange(0, 100)
        self._sensor_quality.setValue(100)
        self._sensor_convergence = QLabel("收敛度：--")
        self._sensor_warn = QLabel("低速警告：正常")
        sensor_grid.addWidget(self._sensor_source, 0, 0)
        sensor_grid.addWidget(self._sensor_quality, 0, 1)
        sensor_grid.addWidget(self._sensor_convergence, 1, 0)
        sensor_grid.addWidget(self._sensor_warn, 1, 1)
        root.addWidget(sensor_box)

        # ---------- 统计 ----------
        stat_box = QGroupBox("统计（最大/最小）")
        stat_h = QHBoxLayout(stat_box)
        self._stat_speed = _StatItem("转速")
        self._stat_current = _StatItem("电流")
        self._stat_torque = _StatItem("转矩")
        for w in (self._stat_speed, self._stat_current, self._stat_torque):
            stat_h.addWidget(w)
        root.addWidget(stat_box)

        # ---------- 趋势曲线 ----------
        curve_box = QGroupBox("趋势曲线（最近 100 点）")
        curve_h = QHBoxLayout(curve_box)
        self._c_speed = TrendCurve("转速 rpm", {"实际": "#4fc3f7", "给定": "#ffb74d"}, y_label="rpm")
        self._c_current = TrendCurve("电流 A", {"实际": "#81c784"}, y_label="A")
        self._c_torque = TrendCurve("转矩 Nm", {"实际": "#ba68c8"}, y_label="Nm")
        curve_h.addWidget(_make_curve_panel(self._c_speed, "转速 rpm"))
        curve_h.addWidget(_make_curve_panel(self._c_current, "电流 A"))
        curve_h.addWidget(_make_curve_panel(self._c_torque, "转矩 Nm"))
        root.addWidget(curve_box, 1)

        # ---------- 传感器波形 ----------
        sensor_curve_box = QGroupBox("传感器波形")
        sensor_curve_h = QHBoxLayout(sensor_curve_box)
        self._c_angle = TrendCurve("角度 °", {"估算/实际": "#f48fb1", "原始": "#80cbc4"}, y_label="°")
        self._c_sensor_q = TrendCurve("传感器诊断 (0-1)", {"质量": "#ffcc80", "收敛度": "#ce93d8"}, y_label="")
        sensor_curve_h.addWidget(_make_curve_panel(self._c_angle, "角度 °"))
        sensor_curve_h.addWidget(_make_curve_panel(self._c_sensor_q, "传感器诊断"))
        root.addWidget(sensor_curve_box, 1)

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
        self._angle_actual.set_value(f.angle_actual)
        self._angle_raw.set_value(f.angle_raw)

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

    def _save_all_curves(self) -> None:
        try:
            import pyqtgraph as pg
        except ImportError:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "提示", "未安装 pyqtgraph，无法保存波形")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存所有波形",
            f"波形_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            "PNG (*.png)"
        )
        if not path:
            return
        curves = [
            (self._c_speed,    "转速"),
            (self._c_current,  "电流"),
            (self._c_torque,   "转矩"),
            (self._c_angle,    "角度"),
            (self._c_sensor_q, "传感器诊断"),
        ]
        win = pg.GraphicsLayoutWidget()
        win.setBackground("#2b2f38")
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
        win.show()
        win.grab().save(path)
        win.close()

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
        if not self._sim_running:
            self._comm.start_simulation()
            self._sim_running = True
            self._btn_sim.setText("停止仿真")
        else:
            self._comm.stop_simulation()
            self._sim_running = False
            self._btn_sim.setText("启动仿真")
