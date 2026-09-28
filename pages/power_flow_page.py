"""功率流页面：电源 → 母线 → 逆变器 → 电机 → 转轴 的能量链路可视化。

上半部分为自绘功率流图：主链箭头粗细随功率大小变化，回馈制动时
箭头反向并变色；各级损耗（电源内阻、制动电阻、铜损、摩擦）以向下
支路标注。下半部分为功率趋势曲线。

数据来自遥测帧的 powers 快照（仿真），或根据真机F0/F1的
Iq、Vq、Vbus、转速和转矩做主机侧估算。
逆变器开关损耗暂忽略，直流侧输入 ≈ 电机电功率。
"""
import math
import time

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QScrollArea, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from core.power_estimate import inverter_power, voltage_from_raw
from widgets.formula_view import Eq, FormulaImage
from widgets.power_playback import PowerPlayback
from widgets.power_sankey import PowerSankey
from widgets.trend_curve import TrendCurve


class _CalculationPanel(QGroupBox):
    """能量链旁的公式、实时数值和守恒诊断。"""

    _FORMULAS = (
        ("逆变器电气输入", Eq(r"P_{\mathrm{inv}} = \dfrac{3}{2}\,(v_d i_d + v_q i_q)")),
        ("定子铜损", Eq(r"P_{\mathrm{Cu}} = \dfrac{3}{2}\,R_s\,(i_d^2 + i_q^2)")),
        ("电磁功率", Eq(r"P_{\mathrm{em}} = T_e\,\omega_m")),
        ("转轴动能", Eq(r"P_{\mathrm{kin}} = P_{\mathrm{em}} - P_{\mathrm{fric/load}}")),
        ("电源与制动", Eq(r"P_{\mathrm{src}} = V_{\mathrm{src}}\,I_{\mathrm{src}},\quad"
                          r"P_{\mathrm{brake}} = \dfrac{V_{dc}^2}{R_{\mathrm{brake}}}")),
    )
    _VALUES = (
        ("supply", "电源输入"), ("inv", "逆变器输入"),
        ("em", "电磁功率"), ("kinetic", "动能变化"),
        ("loss_src", "电源内阻"), ("cu", "定子铜损"),
        ("fric", "摩擦/负载"), ("brake", "制动泄放"),
    )

    def __init__(self) -> None:
        super().__init__("实时计算")
        self.setMinimumWidth(350)
        root = QVBoxLayout(self)
        root.setSpacing(7)

        for title, equation in self._FORMULAS:
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #171d27; border: 1px solid #2f3b4d; "
                "border-radius: 5px; }")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(9, 5, 9, 6)
            layout.setSpacing(2)
            heading = QLabel(title)
            heading.setStyleSheet("color: #b8c6d8; font-weight: 600; border: none;")
            formula = FormulaImage(equation.latex)
            layout.addWidget(heading)
            layout.addWidget(formula)
            root.addWidget(card)

        values_title = QLabel("当前功率")
        values_title.setStyleSheet("font-weight: 600; color: #dfe6ee;")
        root.addWidget(values_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(3)
        self._value_labels: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(self._VALUES):
            row, column = divmod(index, 2)
            label = QLabel(f"{title}  -- W")
            label.setStyleSheet("color: #b8c6d8;")
            self._value_labels[key] = label
            grid.addWidget(label, row, column)
        root.addLayout(grid)

        self._direction = QLabel("能量方向：—")
        self._direction.setStyleSheet("font-weight: 600; color: #90a4ae;")
        self._bus_balance = QLabel("母线储能变化率：—")
        self._mech_balance = QLabel("机械平衡误差：—")
        self._motor_balance = QLabel("电机储能/未建模项：—")
        for label in (self._direction, self._bus_balance,
                      self._mech_balance, self._motor_balance):
            label.setWordWrap(True)
            root.addWidget(label)

        note = QLabel(
            "橙色：电源→转轴　蓝色：回馈　红色：损耗/制动\n"
            "当前忽略开关损耗、铁耗和杂散损耗；瞬态差值可进入母线电容或电机储能。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #ffcc80; font-size: 11px;")
        root.addWidget(note)
        root.addStretch(1)

    def set_data(self, powers: dict) -> None:
        if not powers:
            for key, title in self._VALUES:
                self._value_labels[key].setText(f"{title}  -- W")
            self._direction.setText("能量方向：—")
            self._bus_balance.setText("母线储能变化率：—")
            self._mech_balance.setText("机械平衡误差：—")
            self._motor_balance.setText("电机储能/未建模项：—")
            return
        p = powers
        for key, title in self._VALUES:
            value = float(p.get(key, 0.0))
            self._value_labels[key].setText(f"{title}  {value:+.1f} W")
        inv = float(p.get("inv", 0.0))
        if inv < -0.5:
            direction, color = "回馈：转轴 → 母线", "#4fc3f7"
        elif inv > 0.5:
            direction, color = "驱动：电源 → 转轴", "#ffb74d"
        else:
            direction, color = "近似零功率", "#90a4ae"
        self._direction.setText("能量方向：" + direction)
        self._direction.setStyleSheet(f"font-weight: 600; color: {color};")

        bus_storage = (float(p.get("supply", 0.0)) -
                       float(p.get("loss_src", 0.0)) - inv -
                       float(p.get("brake", 0.0)))
        mech_error = (float(p.get("em", 0.0)) -
                      float(p.get("fric", 0.0)) -
                      float(p.get("kinetic", 0.0)))
        motor_storage = (inv - float(p.get("cu", 0.0)) -
                         float(p.get("em", 0.0)))
        self._bus_balance.setText(
            f"母线储能变化率 ≈ {bus_storage:+.2f} W "
            "（+充电 / −放电）")
        self._mech_balance.setText(
            f"机械平衡误差 = {mech_error:+.3f} W")
        self._motor_balance.setText(
            f"电机储能/未建模项 ≈ {motor_storage:+.2f} W")


class PowerFlowPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._analysis_enabled = False
        self._latest = TelemetryFrame()
        self._last_telemetry_at = 0.0
        self._last_f1_at = 0.0
        self._real_inv_w: float | None = None
        self._real_iq_rms_a: float | None = None
        self._previous_omega = 0.0
        self._previous_speed_at = 0.0
        self._kinetic_power_w = 0.0

        root = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("功率流")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._chk_enabled = QCheckBox("启用功率流")
        self._chk_enabled.setChecked(False)
        self._chk_enabled.setToolTip(
            "默认关闭以避免在后台持续计算和绘制功率流。")
        title_row.addWidget(self._chk_enabled)
        self._chk_offline = QCheckBox("离线回放")
        self._chk_offline.setToolTip("回放实验目录里保存的 高速数据.csv；回放期间暂停实时数据")
        title_row.addWidget(self._chk_offline)
        self._eff_label = QLabel("效率 η = --")
        self._eff_label.setStyleSheet("color: #69f0ae; font-weight: bold;")
        title_row.addWidget(self._eff_label)
        self._source_label = QLabel("数据源：等待")
        self._source_label.setStyleSheet("color:#90a4ae;")
        title_row.addWidget(self._source_label)
        root.addLayout(title_row)

        hint = QLabel(
            "电源 → 直流母线 → 逆变器 → 电机 → 转轴 的实时能量桑基图："
            "能量带宽度 ∝ 功率，红色支路为损耗，紫色为转子动能储存/释放；"
            "回馈制动时主链变蓝、粒子反向，能量经母线泵升由制动电阻泄放。逆变器开关损耗暂忽略。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #90a4ae;")
        root.addWidget(hint)

        diag_box = QGroupBox("能量链路与功率守恒")
        dv = QHBoxLayout(diag_box)
        self._diagram = PowerSankey()
        self._calculation = _CalculationPanel()
        # 计算面板很高：单独滚动，窗口不够高时不把下方的趋势/回放区挤出屏幕
        calc_scroll = QScrollArea()
        calc_scroll.setWidgetResizable(True)
        calc_scroll.setFrameShape(QScrollArea.NoFrame)
        calc_scroll.setWidget(self._calculation)
        calc_scroll.setMinimumWidth(self._calculation.minimumWidth() + 16)
        dv.addWidget(self._diagram, 3)
        dv.addWidget(calc_scroll, 2)
        root.addWidget(diag_box, 3)

        curve_box = QGroupBox("功率趋势")
        cv = QVBoxLayout(curve_box)
        self._curve = TrendCurve(
            "功率 W",
            {"电源输入": "#ffb74d", "电磁功率": "#4fc3f7",
             "制动泄放": "#ef5350", "总损耗": "#81c784"},
            y_label="W")
        self._curve.set_source_processing(
            "仿真功率快照，或真机F0/F1主机侧估算")
        cv.addWidget(self._curve)
        root.addWidget(curve_box, 2)
        self._curve_box = curve_box
        self._playback = PowerPlayback(comm.motor_sim_params)
        self._playback.frameChanged.connect(self._on_playback_frame)
        self._playback.hide()
        root.addWidget(self._playback, 2)
        self._chk_offline.toggled.connect(self._set_offline)

        comm.telemetryReceived.connect(self._on_telemetry)
        comm.highRateTelemetryReceived.connect(self._on_high_rate)
        comm.highRateTelemetryBatchReceived.connect(self._on_high_rate_batch)
        comm.highRateTelemetryColumnsReceived.connect(self._on_high_rate_columns)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.setInterval(200)
        self._chk_enabled.toggled.connect(self._set_analysis_enabled)

    def _set_offline(self, offline: bool) -> None:
        """离线回放与实时功率流互斥：回放时停掉实时估算，结束后恢复空白待机。"""
        if offline:
            self._chk_enabled.setChecked(False)
        self._chk_enabled.setEnabled(not offline)
        self._curve_box.setVisible(not offline)
        self._playback.setVisible(offline)
        self._diagram.set_active(offline)
        if offline:
            if self._playback.loaded:
                self._playback.seek(self._playback._t)
            else:
                self._source_label.setText("数据源：离线回放，请打开高速数据")
        else:
            self._playback.stop()
            self._diagram.set_data({}, 0.0, "normal")
            self._calculation.set_data({})
            self._source_label.setText("数据源：等待")
            self._eff_label.setText("效率 η = --")

    def _on_playback_frame(self, powers: dict, vbus: float, t: float) -> None:
        if not self._chk_offline.isChecked():
            return
        self._diagram.set_data(powers, vbus, "normal")
        self._calculation.set_data(powers)
        self._source_label.setText(f"数据源：离线回放 t = {t:.2f} s")
        self._update_efficiency(powers)

    def _set_analysis_enabled(self, enabled: bool) -> None:
        """按需启用功率估算与动画；关闭时不再消费高速F1数据。"""
        self._analysis_enabled = bool(enabled)
        self._diagram.set_active(self._analysis_enabled)
        if self._analysis_enabled:
            self._timer.start()
            return
        self._timer.stop()
        self._latest = TelemetryFrame()
        self._last_telemetry_at = 0.0
        self._last_f1_at = 0.0
        self._real_inv_w = None
        self._real_iq_rms_a = None
        self._kinetic_power_w = 0.0
        self._diagram.set_data({}, 0.0, "normal")
        self._calculation.set_data({})
        self._curve.clear()
        self._source_label.setText("数据源：已关闭")
        self._eff_label.setText("效率 η = --")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        if not self._analysis_enabled:
            return
        self._latest = frame
        now = time.monotonic()
        omega = float(frame.speed_actual) * math.pi / 30.0
        if self._previous_speed_at > 0.0:
            dt = now - self._previous_speed_at
            if 0.02 <= dt <= 1.0:
                inertia = float(self._comm.motor_sim_params().J)
                raw = inertia * omega * (omega - self._previous_omega) / dt
                self._kinetic_power_w = 0.75 * self._kinetic_power_w + 0.25 * raw
        self._previous_omega = omega
        self._previous_speed_at = now
        self._last_telemetry_at = now

    def _consume_f1(self, iq_values, vq_values, vbus_values) -> None:
        if not self._analysis_enabled:
            return
        count = min(len(iq_values), len(vq_values), len(vbus_values))
        if count <= 0:
            return
        # 功率页只需要每批的短时平均，取最新256点避免隐藏页
        # 也在Python中扫描整个16 kHz队列。
        start = max(0, count - 256)
        iq = np.asarray(iq_values[start:count], float)
        vq = voltage_from_raw(vq_values[start:count], vbus_values[start:count])
        # 与离线回放同一公式（core/power_estimate）；实时流不带 id，按 id≈0 估算
        self._real_inv_w = float(np.mean(inverter_power(0.0, 0.0, vq, iq)))
        self._real_iq_rms_a = float(np.sqrt(np.mean(iq * iq)))
        self._last_f1_at = time.monotonic()

    def _on_high_rate(self, sample: dict) -> None:
        self._consume_f1(
            [sample.get("iq_a", 0.0)], [sample.get("vq_raw", 0.0)],
            [sample.get("vbus_v", self._latest.vdc)])

    def _on_high_rate_batch(self, samples: list[dict]) -> None:
        if not samples:
            return
        self._consume_f1(
            [sample.get("iq_a", 0.0) for sample in samples],
            [sample.get("vq_raw", 0.0) for sample in samples],
            [sample.get("vbus_v", self._latest.vdc) for sample in samples])

    def _on_high_rate_columns(self, columns: dict) -> None:
        self._consume_f1(
            columns.get("iq_a", ()), columns.get("vq_raw", ()),
            columns.get("vbus_v", ()))

    def _estimate_real_powers(self) -> tuple[dict, str]:
        f = self._latest
        params = self._comm.motor_sim_params()
        iq_rms = (self._real_iq_rms_a
                  if self._real_iq_rms_a is not None
                  else abs(float(f.current_actual)))
        cu = 1.5 * float(params.Rs) * iq_rms * iq_rms
        omega = float(f.speed_actual) * math.pi / 30.0
        em = float(f.torque_actual) * omega
        fresh_f1 = (self._real_inv_w is not None and
                    time.monotonic() - self._last_f1_at < 1.0)
        inv = float(self._real_inv_w) if fresh_f1 else em + cu
        brake = max(-inv, 0.0) if f.bus_state == "brake" else 0.0
        supply = inv + brake
        kinetic = self._kinetic_power_w
        load_and_friction = em - kinetic
        powers = {
            "supply": supply,
            "loss_src": 0.0,       # 协议暂无母线输入电流
            "inv": inv,
            "brake": brake,
            "cu": cu,
            "em": em,
            "fric": load_and_friction,
            "kinetic": kinetic,
        }
        source = ("真机估算：F1 Vq·Iq + F0转速/转矩"
                  if fresh_f1 else
                  "真机降级估算：F0电磁功率+铜损（等待F1电压）")
        return powers, source

    def _update_efficiency(self, p: dict) -> None:
        supply, em = p.get("supply", 0.0), p.get("em", 0.0)
        if em < -1.0:
            self._eff_label.setText("回馈制动中")
            self._eff_label.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        elif supply > 5.0 and em > 0.0:
            self._eff_label.setText(f"效率 η = {min(em / supply, 1.0):.1%}")
            self._eff_label.setStyleSheet("color: #69f0ae; font-weight: bold;")
        else:
            self._eff_label.setText("效率 η = --（轻载）")
            self._eff_label.setStyleSheet("color: #90a4ae;")

    def _refresh(self) -> None:
        if not self._analysis_enabled:
            return
        f = self._latest
        p = f.powers or {}
        source = "仿真模型功率"
        if (not p and self._comm.is_connected() and
                not self._comm.is_sim_running() and
                time.monotonic() - self._last_telemetry_at < 1.0):
            p, source = self._estimate_real_powers()
        elif not p:
            source = "等待数据"
        self._source_label.setText("数据源：" + source)
        self._diagram.set_data(p, f.vdc, f.bus_state)
        self._calculation.set_data(p)
        if not p:
            self._eff_label.setText("效率 η = --")
            return
        self._update_efficiency(p)
        supply, em = p.get("supply", 0.0), p.get("em", 0.0)
        loss = (p.get("loss_src", 0.0) + p.get("brake", 0.0)
                + p.get("cu", 0.0) + p.get("fric", 0.0))
        self._curve.append({"电源输入": supply, "电磁功率": em,
                            "制动泄放": p.get("brake", 0.0), "总损耗": loss})
