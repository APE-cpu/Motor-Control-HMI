"""功率流页面：电源 → 母线 → 逆变器 → 电机 → 转轴 的能量链路可视化。

上半部分为自绘功率流图：主链箭头粗细随功率大小变化，回馈制动时
箭头反向并变色；各级损耗（电源内阻、制动电阻、铜损、摩擦）以向下
支路标注。下半部分为功率趋势曲线。

数据来自遥测帧的 powers 快照（仿真由 MotorSim 逐步计算；真机协议
暂无功率字段，接真机后此页显示等待数据）。
逆变器开关损耗暂忽略，直流侧输入 ≈ 电机电功率。
"""
import math

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from widgets.trend_curve import TrendCurve

_BOX_FILL = QColor("#37474f")
_BOX_EDGE = QColor("#546e7a")
_TEXT = QColor("#eceff1")
_FWD = QColor("#ffb74d")     # 正向功率（电→机械）
_REV = QColor("#4fc3f7")     # 回馈功率（机械→电）
_LOSS = QColor("#ef9a9a")    # 损耗支路


class _CalculationPanel(QGroupBox):
    """能量链旁的公式、实时数值和守恒诊断。"""

    _FORMULAS = (
        ("逆变器电气输入", "P_inv = 3/2 · (v_d · i_d + v_q · i_q)"),
        ("定子铜损", "P_Cu = 3/2 · R_s · (i_d² + i_q²)"),
        ("电磁功率", "P_em = T_e · ω_m"),
        ("转轴动能", "P_kin = P_em − P_fric/load"),
        ("电源与制动", "P_src = V_src · I_src    P_brake = V_dc² / R_brake"),
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
            formula = QLabel(equation)
            formula.setTextFormat(Qt.PlainText)
            formula.setStyleSheet(
                "color: #80cbc4; font-family: Consolas, 'Courier New', monospace; "
                "font-size: 12px; border: none;")
            formula.setWordWrap(True)
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


class _FlowDiagram(QWidget):
    """自绘功率流图。set_data 后 update() 重绘。"""

    _NODES = ["电源", "直流母线", "逆变器", "电机", "转轴/负载"]

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(220)
        self._powers: dict = {}
        self._vdc = 48.0
        self._bus_state = "normal"
        # 能量脉冲动画：虚线相位随时间推进，流速 ∝ 功率
        self._t = 0.0
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(40)

    def _tick(self) -> None:
        self._t += 0.04
        if self._powers and self.isVisible():
            self.update()

    def set_data(self, powers: dict, vdc: float, bus_state: str) -> None:
        self._powers = powers or {}
        self._vdc = vdc
        self._bus_state = bus_state
        self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if not self._powers:
            qp.setPen(QPen(QColor("#90a4ae")))
            qp.drawText(self.rect(), Qt.AlignCenter,
                        "暂无功率数据（启动仿真后显示；真机协议暂不支持）")
            return

        p = self._powers
        n = len(self._NODES)
        box_w = min(120, int(w / n * 0.62))
        box_h = 44
        y_mid = int(h * 0.38)
        gap = (w - n * box_w) / (n + 1)
        centers = [gap + box_w / 2 + i * (box_w + gap) for i in range(n)]

        # 节点框（母线框附带电压值）
        for i, name in enumerate(self._NODES):
            x = centers[i] - box_w / 2
            qp.setPen(QPen(_BOX_EDGE, 1.5))
            qp.setBrush(_BOX_FILL)
            qp.drawRoundedRect(int(x), y_mid - box_h // 2, box_w, box_h, 6, 6)
            qp.setPen(QPen(_TEXT))
            label = name
            if name == "直流母线":
                label = f"{name}\n{self._vdc:.1f} V"
            qp.drawText(int(x), y_mid - box_h // 2, box_w, box_h,
                        Qt.AlignCenter, label)

        # 主链功率：电源→母线、母线→逆变器（=逆变器→电机）、电机→轴
        chain = [p.get("supply", 0.0), p.get("inv", 0.0),
                 p.get("inv", 0.0), p.get("em", 0.0)]
        for i, val in enumerate(chain):
            x1 = centers[i] + box_w / 2
            x2 = centers[i + 1] - box_w / 2
            self._arrow(qp, x1, x2, y_mid, val)

        # 损耗支路（向下）：位置 = 支路所挂的节点
        losses = [
            (0.5, "内阻损耗", p.get("loss_src", 0.0)),   # 电源→母线之间
            (1.0, "制动电阻", p.get("brake", 0.0)),
            (3.0, "铜损", p.get("cu", 0.0)),
            (4.0, "摩擦/负载", p.get("fric", 0.0)),
        ]
        y_tail = y_mid + box_h // 2 + 8
        y_head = int(h * 0.72)
        for pos, name, val in losses:
            if pos == int(pos):     # 挂在节点正下方
                x = centers[int(pos)]
                y0 = y_tail
            else:                   # 挂在两节点之间的箭头下方
                x = (centers[int(pos)] + centers[int(pos) + 1]) / 2.0
                y0 = y_mid + 10
            color = QColor(_LOSS)
            if name == "制动电阻" and val > 1.0:   # 泄放中：红色呼吸脉动
                color = QColor("#ff5252")
                color.setAlphaF(0.55 + 0.45 * math.sin(self._t * 6.0))
            self._flow_line(qp, QPointF(x, y0), QPointF(x, y_head),
                            color, val, 90.0)
            qp.setPen(QPen(_LOSS))
            qp.drawText(int(x - 60), y_head + 4, 120, 34,
                        Qt.AlignHCenter | Qt.AlignTop,
                        f"{name}\n{val:.1f} W")

        # 动能变化率：标在转轴节点上方
        pk = p.get("kinetic", 0.0)
        tag = "动能储存" if pk >= 0 else "动能释放"
        qp.setPen(QPen(_FWD if pk >= 0 else _REV))
        qp.drawText(int(centers[-1] - 70), y_mid - box_h // 2 - 36, 140, 32,
                    Qt.AlignHCenter | Qt.AlignBottom, f"{tag}\n{abs(pk):.1f} W")

    def _pen_width(self, power: float) -> float:
        return 1.5 + 3.0 * min(1.0, abs(power) / 200.0)

    def _arrow(self, qp: QPainter, x1: float, x2: float, y: float,
               power: float) -> None:
        """主链水平箭头：正功率向右（橙），回馈向左（蓝）。"""
        color = _FWD if power >= 0 else _REV
        if power >= 0:
            a, b, ang = QPointF(x1, y), QPointF(x2, y), 0.0
        else:   # 回馈：流向反转，脉冲向左流
            a, b, ang = QPointF(x2, y), QPointF(x1, y), 180.0
        self._flow_line(qp, a, b, color, power, ang)
        qp.setPen(QPen(color))
        qp.drawText(int((x1 + x2) / 2 - 50), int(y) - 26, 100, 20,
                    Qt.AlignCenter, f"{abs(power):.1f} W")

    def _flow_line(self, qp: QPainter, a: QPointF, b: QPointF,
                   color: QColor, power: float, head_angle: float) -> None:
        """能量流线：暗色底线 + 沿流向移动的虚线脉冲，流速 ∝ 功率。"""
        pen_w = self._pen_width(power)
        dim = QColor(color)
        dim.setAlpha(60)
        qp.setPen(QPen(dim, pen_w))
        qp.drawLine(a, b)
        mag = abs(power)
        if mag > 0.5:
            pen = QPen(color, pen_w)
            pen.setCapStyle(Qt.RoundCap)
            dash, gap = 1.8, 2.6                     # 单位：线宽倍数
            pen.setDashPattern([dash, gap])
            px_per_s = 30.0 + 0.4 * min(mag, 400.0)  # 功率越大流得越快
            offset = (self._t * px_per_s / max(pen_w, 0.5)) % (dash + gap)
            pen.setDashOffset(-offset)               # 负向偏移 = 沿画线方向流动
            qp.setPen(pen)
            qp.drawLine(a, b)
        self._arrow_head(qp, b, head_angle, color, pen_w)

    @staticmethod
    def _arrow_head(qp: QPainter, tip: QPointF, angle_deg: float,
                    color: QColor, pen_w: float) -> None:
        """在 tip 处画箭头头部；angle 0=向右，90=向下，180=向左。"""
        size = 5.0 + pen_w
        qp.save()
        qp.translate(tip)
        qp.rotate(angle_deg)
        qp.setPen(Qt.NoPen)
        qp.setBrush(color)
        qp.drawPolygon(QPolygonF([QPointF(0, 0),
                                  QPointF(-size, -size * 0.55),
                                  QPointF(-size, size * 0.55)]))
        qp.restore()


class PowerFlowPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._latest = TelemetryFrame()

        root = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("功率流")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._eff_label = QLabel("效率 η = --")
        self._eff_label.setStyleSheet("color: #69f0ae; font-weight: bold;")
        title_row.addWidget(self._eff_label)
        root.addLayout(title_row)

        hint = QLabel(
            "电源 → 直流母线 → 逆变器 → 电机 → 转轴 的实时能量链路。"
            "箭头粗细 ∝ 功率大小；回馈制动时主链箭头反向变蓝，"
            "能量经母线泵升由制动电阻泄放。逆变器开关损耗暂忽略。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #90a4ae;")
        root.addWidget(hint)

        diag_box = QGroupBox("能量链路与功率守恒")
        dv = QHBoxLayout(diag_box)
        self._diagram = _FlowDiagram()
        self._calculation = _CalculationPanel()
        dv.addWidget(self._diagram, 3)
        dv.addWidget(self._calculation, 2)
        root.addWidget(diag_box, 3)

        curve_box = QGroupBox("功率趋势")
        cv = QVBoxLayout(curve_box)
        self._curve = TrendCurve(
            "功率 W",
            {"电源输入": "#ffb74d", "电磁功率": "#4fc3f7",
             "制动泄放": "#ef5350", "总损耗": "#81c784"},
            y_label="W")
        cv.addWidget(self._curve)
        root.addWidget(curve_box, 2)

        comm.telemetryReceived.connect(self._on_telemetry)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(200)

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame

    def _refresh(self) -> None:
        f = self._latest
        p = f.powers or {}
        self._diagram.set_data(p, f.vdc, f.bus_state)
        self._calculation.set_data(p)
        if not p:
            self._eff_label.setText("效率 η = --")
            return
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
        loss = (p.get("loss_src", 0.0) + p.get("brake", 0.0)
                + p.get("cu", 0.0) + p.get("fric", 0.0))
        self._curve.append({"电源输入": supply, "电磁功率": em,
                            "制动泄放": p.get("brake", 0.0), "总损耗": loss})
