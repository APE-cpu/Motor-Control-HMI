"""参数面板基类 + 各控制方式的参数面板。

控制方式面板统一为「左侧参数表单 + 右侧数学模型与参数说明」布局；
传感器参数面板保持简单表单。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from config.config import SENSORLESS_METHODS
from widgets.formula_view import Eq, FormulaSheet, Notes, Sec, Txt, Warn


class _Panel(QWidget):
    def values(self) -> dict:
        """返回所有可调参数的当前值。"""
        raise NotImplementedError


class _FormulaPanel(_Panel):
    """控制方式面板基类：左侧参数、右侧「数学模型与参数说明」。"""

    def __init__(self) -> None:
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        left = QWidget()
        self.left_v = QVBoxLayout(left)
        self.left_v.setContentsMargins(0, 0, 0, 0)
        self.form = QFormLayout()
        self.left_v.addLayout(self.form)
        self.left_v.addStretch(1)
        h.addWidget(left, 2)

        box = QGroupBox("数学模型与参数说明")
        bv = QVBoxLayout(box)
        self._formula = FormulaSheet()
        self._formula.setContentsMargins(4, 0, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self._formula)
        bv.addWidget(scroll)
        h.addWidget(box, 3)

    def set_formula(self, blocks) -> None:
        self._formula.set_blocks(blocks)


def _loop_group(title: str, rows: list) -> tuple:
    """带标题的参数子组：rows = [(标签, widget), ...]。"""
    box = QGroupBox(title)
    f = QFormLayout(box)
    for label, w in rows:
        f.addRow(label, _field_with_range(w))
    return box


def _format_bound(value: float) -> str:
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _field_with_range(widget: QWidget, hint: str = "") -> QWidget:
    """输入框在前、弱化的范围提示在后，避免参数名被范围文字淹没。"""
    if not hint and isinstance(widget, (QDoubleSpinBox, QSpinBox)) \
            and not widget.isReadOnly():
        hint = (f"{_format_bound(widget.minimum())} ～ "
                f"{_format_bound(widget.maximum())}")
    if not hint:
        return widget

    field = QWidget()
    field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    widget_policy = widget.sizePolicy()
    widget_policy.setHorizontalPolicy(QSizePolicy.Expanding)
    widget.setSizePolicy(widget_policy)
    row = QHBoxLayout(field)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addWidget(widget, 1)
    range_label = QLabel(hint)
    range_label.setObjectName("RangeHintLabel")
    range_label.setStyleSheet("color: #748291; font-size: 11px;")
    range_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(range_label)
    return field


def _dspin(mn, mx, val, decimals=4, step=None):
    sp = QDoubleSpinBox()
    sp.setRange(mn, mx)
    sp.setDecimals(decimals)
    sp.setValue(val)
    if step:
        sp.setSingleStep(step)
    sp.setToolTip(
        f"可输入范围：{_format_bound(mn)} ～ {_format_bound(mx)}")
    return sp


# ─── 闭环 PI（转速外环 + 电流内环级联） ─────────────────────────
_PI_FORMULA = [
    Txt("<b>级联双闭环</b>：转速外环输出电流给定，电流内环输出电压。"),
    Sec("转速环（外环）"),
    Eq(r"e_\omega = \omega^* - \omega"),
    Eq(r"i_q^* = K_{p\omega}\,e_\omega + K_{i\omega}\int e_\omega\,\mathrm{d}t"),
    Eq(r"|i_q^*| \leq i_{q\max}", "输出限幅"),
    Sec("电流环（内环，含 dq 解耦前馈）"),
    Eq(r"v_d = K_{pi}\,e_d + K_{ii}\int e_d\,\mathrm{d}t - \omega_e L_q i_q"),
    Eq(r"v_q = K_{pi}\,e_q + K_{ii}\int e_q\,\mathrm{d}t + \omega_e\,(L_d i_d + \psi_f)"),
    Txt("其中 e<sub>d</sub> = i<sub>d</sub>* − i<sub>d</sub>，"
        "e<sub>q</sub> = i<sub>q</sub>* − i<sub>q</sub>；表贴式取 i<sub>d</sub>* = 0。"),
    Sec("参数说明"),
    Notes(
        "K<sub>pω</sub>/K<sub>iω</sub>：转速环增益。Kp 大→响应快但易超调；"
        "Ki 消除稳态误差，过大引起振荡",
        "i<sub>qmax</sub>：转速环输出限幅 = 最大转矩电流，兼作过流保护；"
        "限幅期间冻结积分（抗饱和）",
        "K<sub>pi</sub>/K<sub>ii</sub>：电流环增益，按带宽整定 "
        "K<sub>pi</sub> = L·ω<sub>bw</sub>，K<sub>ii</sub> = R·ω<sub>bw</sub>",
        "采样时间：内环带宽须远高于外环（≥10 倍），典型电流环 10~20 kHz、"
        "转速环 1 kHz"),
]

class PIPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.kp_spd = _dspin(0, 1e4, 1752, 0)
        self.ki_spd = _dspin(0, 1e4, 121, 0)
        for gain in (self.kp_spd, self.ki_spd):
            gain.setToolTip("允许范围：0～10000；运行中单次热更新不得超过当前值±10%")
        # 真机速度控制器是 PI；保留该对象只兼容旧配置/数字孪生，不再作为真机参数展示。
        self.kd_spd = _dspin(0, 1e4, 0.0)
        # 与顶部“电流限幅”同一物理量；恢复实验基线 1.887 A（约 4.5 A 硬件上限）。
        self.iq_max = _dspin(0.1, 4.49, 1.887, 3, 0.1)
        self.iq_max.setToolTip(
            "转速环输出限幅 i_qmax（A）。发送参数时同步为 max_current_a。"
            "允许范围：0.1～4.49 A；实验基线1.887 A。")
        self.dt_spd = _dspin(1e-6, 1.0, 0.002, 6)
        self.dt_spd.setReadOnly(True)
        self.dt_spd.setToolTip("下位机固定500 Hz；界面不可修改")
        self.left_v.insertWidget(0, _loop_group("转速环（外环）", [
            ("比例 Kpω", self.kp_spd),
            ("积分 Kiω", self.ki_spd),
            ("输出限幅 iq_max (A)", self.iq_max),
            ("采样时间 (s)", self.dt_spd),
        ]))
        self.kp_cur = _dspin(0, 1e4, 2323, 0)
        self.ki_cur = _dspin(0, 1e4, 2077, 0)
        for gain in (self.kp_cur, self.ki_cur):
            gain.setToolTip("允许范围：0～10000；仅电流环测试模式允许运行中更新，单次±10%")
        self.dt_cur = _dspin(1e-7, 1.0, 0.0000625, 7)
        self.dt_cur.setReadOnly(True)
        self.dt_cur.setToolTip("下位机固定16 kHz；每个PWM周期执行一次")
        self.left_v.insertWidget(1, _loop_group("电流环（内环）", [
            ("比例 Kpi", self.kp_cur),
            ("积分 Kii", self.ki_cur),
            ("控制周期 (s，16 kHz固定)", self.dt_cur),
        ]))
        self.set_formula(_PI_FORMULA)

    def values(self) -> dict:
        return {
            # 转速环
            "kp_spd": self.kp_spd.value(), "ki_spd": self.ki_spd.value(),
            "kd_spd": self.kd_spd.value(), "iq_max": self.iq_max.value(),
            "spd_sample_time": self.dt_spd.value(),
            # 电流环
            "kp_cur": self.kp_cur.value(), "ki_cur": self.ki_cur.value(),
            "cur_sample_time": self.dt_cur.value(),
            # 兼容旧协议/控制器键（映射为外环参数）
            "kp": self.kp_spd.value(), "ki": self.ki_spd.value(),
            "kd": self.kd_spd.value(), "sample_time": self.dt_spd.value(),
        }


# ─── PMSM 位置—速度—电流三级级联 ───────────────────────────
_POSITION_FORMULA = [
    Txt("<b>位置三级级联</b>：最外层位置环输出速度给定，下面复用速度 PI 和电流 PI。"
        "位置环不直接驱动 PWM。"),
    Sec("位置环（最外环，200 Hz）"),
    Eq(r"e_\theta = \theta^* - \theta", "连续机械角，支持多圈"),
    Eq(r"(\theta_r,\ n_r) = \mathrm{Traj}(\theta^*,\ n_{\mathrm{lim}},\ a_{\mathrm{lim}})",
       "加减速受限轨迹"),
    Eq(r"n_{\mathrm{pos}} = K_{p\theta}\,(\theta_r - \theta) - K_{d\theta}\,n"
       r" + \mathrm{LPF}(6\,K_{pf\theta}\,n_r)"),
    Eq(r"n^* = \mathrm{sat}(n_{\mathrm{pos}},\ \pm n_{\mathrm{lim}})"),
    Sec("速度环与电流环"),
    Eq(r"i_q^* = \mathrm{PI}_\omega(n^* - n)"),
    Eq(r"v_{dq} = \mathrm{PI}_i(i_{dq}^* - i_{dq}) + v_{\mathrm{ff}}", "v_ff：dq 解耦前馈"),
    Sec("对拖台架用法"),
    Notes(
        "上位机仍输入最终角度；固件内部生成加减速受限轨迹，避免位置阶跃瞬间顶到速度限幅",
        "首轮用 ±10°，速度限幅 60 rpm、轨迹加速度 30 rpm/s；确认轨迹与反馈方向正确后再提高",
        "Kpfθ 作用于轨迹速度而非最终目标的数值跳变，因此运行中应能看到非零速度前馈",
        "F407 当前固定：电流/PWM 16 kHz、速度 500 Hz；位置环在 500 Hz 任务中按 200 Hz 执行",
        "位置最外环使用比例、实际速度阻尼与目标速度前馈；积分仍只由速度/电流内环承担",
        "速度指令确认跨过±10 rpm并反向时，固件卸载一次速度PI积分，避免旧方向转矩加重滑动过冲"),
]

class PositionPanel(PIPanel):
    """位置环参数 + 现有速度/电流 PI 参数。"""

    def __init__(self) -> None:
        super().__init__()
        self.kp_pos = _dspin(0.0, 1_000.0, 8.0, 3, 0.5)
        self.kd_pos = _dspin(0.0, 10.0, 0.20, 3, 0.05)
        self.kpf_pos = _dspin(-100.0, 100.0, 0.0, 3, 0.1)
        self.kp_pos.setToolTip("允许范围：0～1000；运行中单次热更新不得超过当前值±10%")
        self.kd_pos.setToolTip(
            "机械速度阻尼，0=关闭；允许范围0～10；运行中单次热更新±10%")
        self.kpf_pos.setToolTip("允许范围：-100～100；运行中单次热更新不得超过当前值±10%")
        self.ff_lpf_hz = _dspin(0.1, 200.0, 8.0, 2, 0.5)
        self.position_speed_limit_rpm = _dspin(1.0, 4_000.0, 300.0, 1, 10.0)
        self.position_accel_limit_rpm_s = _dspin(1.0, 10_000.0, 60.0, 1, 10.0)
        self.position_speed_limit_rpm.setToolTip(
            "允许范围1～4000 rpm；实际值还不得超过顶部配置的最高转速")
        self.position_accel_limit_rpm_s.setToolTip("允许范围：1～10000 rpm/s")
        self.dt_pos = _dspin(1e-6, 1.0, 0.005, 6)
        self.dt_pos.setReadOnly(True)
        self.dt_pos.setToolTip("F407 当前按 500 Hz 中频任务每 2~3 拍执行，位置环 200 Hz")
        self.left_v.insertWidget(0, _loop_group("位置环（最外环）", [
            ("比例 Kpθ (rpm/deg)", self.kp_pos),
            ("速度阻尼 Kdθ (rpm/rpm)", self.kd_pos),
            ("速度前馈 Kpfθ", self.kpf_pos),
            ("前馈低通频率 (Hz)", self.ff_lpf_hz),
            ("位置环速度限幅 (rpm)", self.position_speed_limit_rpm),
            ("轨迹加速度限幅 (rpm/s)", self.position_accel_limit_rpm_s),
            ("采样时间 (s，200 Hz)", self.dt_pos),
        ]))
        self.set_formula(_POSITION_FORMULA)

    def values(self) -> dict:
        values = super().values()
        values.update({
            "kp_pos": self.kp_pos.value(),
            "kd_pos": self.kd_pos.value(),
            "kpf_pos": self.kpf_pos.value(),
            "position_ff_lpf_hz": self.ff_lpf_hz.value(),
            "position_speed_limit_rpm": self.position_speed_limit_rpm.value(),
            "position_accel_limit_rpm_s": self.position_accel_limit_rpm_s.value(),
            "pos_sample_time": self.dt_pos.value(),
        })
        return values


# ─── 速度开环 / 电流闭环调试 ───────────────────────────────
_OPENLOOP_FORMULA = [
    Txt("<b>速度环旁路，d/q 电流环保持闭环</b>。直接给定 Iqref，"
        "用于电流环 PI 整定；这不是 V/f 开环运行。"),
    Sec("电流闭环"),
    Eq(r"e_q = I_{q,\mathrm{ref}} - I_q"),
    Eq(r"V_q = K_{pi}\,e_q + K_{ii}\int e_q\,\mathrm{d}t"),
    Eq(r"I_{d,\mathrm{ref}} = 0"),
    Sec("参数说明"),
    Notes(
        "Iqref：q轴转矩电流给定；空载电机会向任一方向加速，不能把它当速度给定",
        "Kpi/Kii：电流内环整数增益；运行中每次只允许修改 ±10%",
        "斜坡时间：改变 Iqref 时的过渡时间，避免电流阶跃过猛"),
    Warn("<b>⚠ 注意空载加速</b>：本模式无速度环，空载电机会持续加速；"
         "超过 1500 rpm 下位机受控停机，超过 2500 rpm 按跑飞锁存故障。"),
]

class OpenLoopPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.iq_ref = _dspin(-1.5, 1.5, 0.00, 3, 0.01)
        self.kp_cur = _dspin(0, 10000, 2323, 0)
        self.ki_cur = _dspin(0, 10000, 2077, 0)
        self.ramp_ms = _dspin(100, 5000, 500, 0, 100)
        self.iq_ref.setToolTip("电流环测试模式允许范围：-1.5～1.5 A")
        for gain in (self.kp_cur, self.ki_cur):
            gain.setToolTip("允许范围：0～10000；运行中单次热更新不得超过当前值±10%")
        self.ramp_ms.setToolTip("允许范围：100～5000 ms")
        self.form.addRow("Iqref (A)", _field_with_range(self.iq_ref))
        self.form.addRow("电流环 Kpi", _field_with_range(self.kp_cur))
        self.form.addRow("电流环 Kii", _field_with_range(self.ki_cur))
        period = QLabel("62.5 µs（16 kHz，每个PWM周期执行一次）")
        period.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        self.form.addRow("电流环控制周期", period)
        self.form.addRow("Iq 斜坡时间 (ms)", _field_with_range(self.ramp_ms))
        self.set_formula(_OPENLOOP_FORMULA)

    def values(self) -> dict:
        return {"control_mode": "current_loop_test",
                "iq_ref_a": self.iq_ref.value(),
                "kp_cur": self.kp_cur.value(),
                "ki_cur": self.ki_cur.value(),
                "iq_ramp_ms": self.ramp_ms.value()}


# ─── MPC ───────────────────────────────────────────────────
_MPC_LOOPS = ["电流环（转速环用 PI）", "转速环（电流环用 PI）", "转速+电流双环"]
_LOOP_NOTE = object()          # 公式模板中「环路结构」说明的插入位置

_FCS_FORMULA = [
    Txt("<b>FCS-MPC（有限集）</b>：每个控制周期遍历逆变器 8 个基本电压矢量"
        " u ∈ {V<sub>0</sub> … V<sub>7</sub>}，取代价最小者直接输出（无调制器）。"),
    Sec("预测模型（dq 电流方程，前向欧拉，T<sub>s</sub> 为控制周期）"),
    Eq(r"i_d(k+1) = i_d + \dfrac{T_s}{L_d}\left[v_d - R_s i_d + \omega_e L_q i_q\right]"),
    Eq(r"i_q(k+1) = i_q + \dfrac{T_s}{L_q}"
       r"\left[v_q - R_s i_q - \omega_e\,(L_d i_d + \psi_f)\right]"),
    Sec("价值函数（预测 N 步）"),
    Eq(r"J = \sum_{k=1}^{N} q\left[(i_d^* - i_d(k))^2 + (i_q^* - i_q(k))^2\right]"
       r" + r\,\Vert\Delta u(k)\Vert^2 + I_{\mathrm{lim}}"),
    Sec("约束处理"),
    Eq(r"I_{\mathrm{lim}} = 0,\quad |i| \leq i_{\max}"),
    Eq(r"I_{\mathrm{lim}} = \mathrm{ECR},\quad |i| > i_{\max}", "大罚值，等效剔除越限矢量"),
    _LOOP_NOTE,
    Sec("参数说明"),
    Notes(
        "N/M：预测/控制时域。N 大→前瞻多但计算量按 8<sup>N</sup> 增长，"
        "FCS 常用 N = 1~2",
        "q/r：跟踪误差与开关变化的权重比。r 越大开关频率越低（损耗小、纹波大）",
        "ECR：约束违反罚值，越大越接近硬约束",
        "u/Δu/x 约束：FCS 中 u 天然离散有界，x 约束以罚项进入价值函数"),
]

_CCS_FORMULA = [
    Txt("<b>CCS-MPC（连续集）</b>：解二次规划得连续电压矢量，经 SVPWM 调制"
        "输出，开关频率固定。"),
    Sec("优化问题（滚动时域，每周期只执行 u(0)）"),
    Eq(r"\min\ J = \sum_{k=1}^{N}\Vert x(k) - x^*\Vert_Q^2"
       r" + \sum_{k=0}^{M-1}\Vert\Delta u(k)\Vert_R^2 + \mathrm{ECR}\cdot\varepsilon^2"),
    Sec("约束条件 s.t."),
    Eq(r"x(k+1) = A\,x(k) + B\,u(k)", "线性化预测模型"),
    Eq(r"u_{\min} \leq u \leq u_{\max},\quad |\Delta u| \leq \Delta u_{\max}", "硬约束"),
    Eq(r"x_{\min} - \varepsilon \leq x \leq x_{\max} + \varepsilon,\quad \varepsilon \geq 0",
       "软约束，ε 为松弛变量"),
    _LOOP_NOTE,
    Sec("参数说明"),
    Notes(
        "N：预测时域，应覆盖被控对象主导时间常数；M ≤ N，M 之后控制量保持不变",
        "Q/R：状态误差与控制增量权重。Q/R 大→跟踪快、控制猛；小→平滑、省能量",
        "ECR：松弛惩罚，防止约束冲突导致 QP 无解",
        "u/Δu：执行器幅值与速率约束；x：状态（转速/电流）安全范围"),
]

_LOOP_NOTES = {
    _MPC_LOOPS[0]: [Sec("环路结构"), Txt(
        "MPC 替代<b>电流环</b>；转速环仍用 PI，其输出 i<sub>q</sub>* "
        "作为 MPC 的电流参考。")],
    _MPC_LOOPS[1]: [Sec("环路结构"), Txt(
        "MPC 替代<b>转速环</b>，输出 i<sub>q</sub>* 给下级 PI 电流环执行。")],
    _MPC_LOOPS[2]: [Sec("环路结构"), Txt(
        "单一 MPC 同时优化转速与电流（状态向量含 ω 和 i<sub>dq</sub>），"
        "无内外环级联。")],
}

class MPCPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.mpc_type = QComboBox(); self.mpc_type.addItems(["连续集(CCS)", "有限集(FCS)"])
        self.loop = QComboBox(); self.loop.addItems(_MPC_LOOPS)
        self.N = QSpinBox(); self.N.setRange(1, 200); self.N.setValue(10)
        self.M = QSpinBox(); self.M.setRange(1, 200); self.M.setValue(3)
        self.q = _dspin(0, 1e6, 1.0, 2)
        self.r = _dspin(0, 1e6, 0.1, 2)
        self.ecr = _dspin(0, 1e6, 1e4, 4)
        self.umin = _dspin(-1e6, 1e6, -24.0, 2)
        self.umax = _dspin(-1e6, 1e6, 24.0, 2)
        self.dumax = _dspin(0, 1e6, 5.0, 2)
        self.xmin = _dspin(-1e6, 1e6, -3000.0, 2)
        self.xmax = _dspin(-1e6, 1e6, 3000.0, 2)
        self.form.addRow("集合类型", self.mpc_type)
        self.form.addRow("替代环节", self.loop)
        self.form.addRow("预测时域 N", self.N)
        self.form.addRow("控制时域 M", self.M)
        self.form.addRow("权重 Q (状态)", self.q)
        self.form.addRow("权重 R (控制)", self.r)
        self.form.addRow("ECR (约束松弛)", self.ecr)
        self.form.addRow("约束 u_min", self.umin)
        self.form.addRow("约束 u_max", self.umax)
        self.form.addRow("约束 Δu_max", self.dumax)
        self.form.addRow("状态约束 x_min", self.xmin)
        self.form.addRow("状态约束 x_max", self.xmax)
        self.mpc_type.currentIndexChanged.connect(self._update_formula)
        self.loop.currentIndexChanged.connect(self._update_formula)
        self._update_formula()

    def _update_formula(self) -> None:
        note = _LOOP_NOTES.get(self.loop.currentText(), [])
        tmpl = _FCS_FORMULA if "FCS" in self.mpc_type.currentText() else _CCS_FORMULA
        blocks = []
        for block in tmpl:
            blocks.extend(note if block is _LOOP_NOTE else [block])
        self.set_formula(blocks)

    def values(self) -> dict:
        return {"mpc_type": self.mpc_type.currentText(),
                "replaced_loop": self.loop.currentText(),
                "prediction_horizon": self.N.value(),
                "control_horizon": self.M.value(),
                "weight_q": self.q.value(),
                "weight_r": self.r.value(),
                "ecr": self.ecr.value(),
                "u_min": self.umin.value(),
                "u_max": self.umax.value(),
                "delta_u_max": self.dumax.value(),
                "x_min": self.xmin.value(),
                "x_max": self.xmax.value()}


# ─── 无位置传感器控制 ────────────────────────────────────────
_SENSORLESS_FORMULA = [
    Txt("<b>环路结构：控制律仍为 PI 双闭环</b>（同「闭环PI控制」），仅位置/"
        "转速反馈由观测器估计值 θ̂、ω̂ 替代物理传感器。观测器方程随所选方法"
        "而异（SMO/EKF/MRAS/HFI，原理详见「传感器详情 / 自检」）。"),
    Sec("I/f 强拖启动（反电动势法低速不可观）"),
    Eq(r"\theta_e = 2\pi\int f_{\mathrm{start}}\,\mathrm{d}t", "按启动频率积分"),
    Eq(r"|i| = i_{\mathrm{start}}", "注入恒定电流"),
    Txt("转速爬升 → 观测器收敛 → 切入闭环"),
    Sec("参数说明"),
    Notes(
        "观测器增益：收敛速度与噪声/抖振的折中，过大易振荡",
        "估算方法：SMO 鲁棒/中高速，EKF 平滑/计算量大，MRAS 参数敏感，"
        "HFI 零低速可用",
        "启动频率：强拖阶段的电角频率斜坡终值",
        "启动电流：强拖注入电流，需克服负载转矩，过大发热"),
]

class SensorlessPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.gain = _dspin(0, 1e6, 100.0, 2)
        self.method = QComboBox(); self.method.addItems(SENSORLESS_METHODS)
        self.start_freq = _dspin(0, 1000, 5.0, 2)
        self.start_curr = _dspin(0, 1000, 2.0, 2)
        self.form.addRow("观测器增益", self.gain)
        self.form.addRow("估算方法", self.method)
        self.form.addRow("启动频率 (Hz)", self.start_freq)
        self.form.addRow("启动电流 (A)", self.start_curr)
        self.set_formula(_SENSORLESS_FORMULA)

    def values(self) -> dict:
        return {"observer_gain": self.gain.value(),
                "method": self.method.currentText(),
                "start_freq": self.start_freq.value(),
                "start_current": self.start_curr.value()}


# ─── 双凸极电机专属面板 ──────────────────────────────────────
_CCC_FORMULA = [
    Txt("<b>电流斩波控制（CCC）</b>：低速段转矩控制，滞环把相电流限制在带内。"),
    Sec("滞环开关律（导通区间内）"),
    Eq(r"i < i_{\mathrm{lower}}\ \Rightarrow\ u = +U_{dc}", "开通"),
    Eq(r"i > i_{\mathrm{upper}}\ \Rightarrow\ u = 0\ \ \mathrm{or}\ -U_{dc}", "关断续流"),
    Sec("磁阻转矩"),
    Eq(r"T = \dfrac{1}{2}\,i^2\,\dfrac{\mathrm{d}L(\theta)}{\mathrm{d}\theta}",
       "电感上升区通电得正转矩"),
    Sec("参数说明"),
    Notes(
        "i<sub>upper</sub>/i<sub>lower</sub>：滞环上下限，差值决定实际斩波频率与纹波",
        "斩波频率：开关频率上限（保护功率管），滞环自然频率高于此值时强制限频",
        "滞环带宽：带宽小→电流平滑但开关损耗大"),
]

class CurrentChoppingPanel(_FormulaPanel):
    """电流斩波控制（CCC）：低速重载常用，电流滞环维持在 [i_lower, i_upper]。"""
    def __init__(self) -> None:
        super().__init__()
        self.i_up = _dspin(0, 1000, 8.0, 2)
        self.i_low = _dspin(0, 1000, 6.0, 2)
        self.f_chop = _dspin(1, 200_000, 10_000.0, 0)
        self.band = _dspin(0, 100, 0.5, 2, 0.1)
        self.form.addRow("电流上限 i_upper (A)", self.i_up)
        self.form.addRow("电流下限 i_lower (A)", self.i_low)
        self.form.addRow("斩波频率 (Hz)", self.f_chop)
        self.form.addRow("滞环带宽 (A)", self.band)
        self.set_formula(_CCC_FORMULA)

    def values(self) -> dict:
        return {"current_upper": self.i_up.value(),
                "current_lower": self.i_low.value(),
                "chopping_frequency": self.f_chop.value(),
                "hysteresis_band": self.band.value()}


_APC_FORMULA = [
    Txt("<b>角度位置控制（APC）</b>：中高速段主流方式，按转子位置角决定各相"
        "开通/关断，导通期内电压全开（单脉冲）。"),
    Sec("导通逻辑（对每相，考虑提前角）"),
    Eq(r"\theta_{\mathrm{on}} - \theta_{\mathrm{adv}} \leq \theta"
       r" < \theta_{\mathrm{off}} - \theta_{\mathrm{adv}}", "该相通电"),
    Sec("磁阻转矩"),
    Eq(r"T = \dfrac{1}{2}\,i^2\,\dfrac{\mathrm{d}L(\theta)}{\mathrm{d}\theta}"),
    Txt("平均转矩由 θ<sub>on</sub>/θ<sub>off</sub> 与转速共同决定。"),
    Sec("参数说明"),
    Notes(
        "θ<sub>on</sub>：开通角。提前开通让电流在电感上升区前建立",
        "θ<sub>off</sub>：关断角。过迟→电流拖入电感下降区产生负转矩",
        "θ<sub>adv</sub>：提前角，随转速增大而增大（补偿电流建立时间 ≈ L·i/U）",
        "限流值：防止低速单脉冲模式下电流失控"),
]

class AnglePositionPanel(_FormulaPanel):
    """角度位置控制（APC）：依据转子角度开通/关断，可设提前角。"""
    def __init__(self) -> None:
        super().__init__()
        self.theta_on = _dspin(-90, 90, 5.0, 2)
        self.theta_off = _dspin(-90, 90, 25.0, 2)
        self.theta_adv = _dspin(-30, 30, 0.0, 2)
        self.i_limit = _dspin(0, 1000, 8.0, 2)
        self.form.addRow("开通角 θ_on (°)", self.theta_on)
        self.form.addRow("关断角 θ_off (°)", self.theta_off)
        self.form.addRow("提前角 θ_adv (°)", self.theta_adv)
        self.form.addRow("限流值 (A)", self.i_limit)
        self.set_formula(_APC_FORMULA)

    def values(self) -> dict:
        return {"turn_on_angle": self.theta_on.value(),
                "turn_off_angle": self.theta_off.value(),
                "advance_angle": self.theta_adv.value(),
                "current_limit": self.i_limit.value()}


_VOLTAGE_FORMULA = [
    Txt("<b>电压 PWM 控制</b>：占空比直接调制绕组平均电压，无电流/转速闭环"
        "（或仅留外部限流保护）。"),
    Sec("控制律"),
    Eq(r"U_{\mathrm{avg}} = D\cdot U_{dc}"),
    Eq(r"n \approx \dfrac{U_{\mathrm{avg}} - I R}{k_e}", "稳态近似，随负载下垂"),
    Sec("参数说明"),
    Notes(
        "直流母线电压 U<sub>dc</sub>：调制的电压基准",
        "占空比 D：0~1，直接决定平均电压",
        "PWM 频率：高→电流纹波小、开关损耗大；典型 10~20 kHz（避开可听频段）",
        "电压限幅：输出电压上限保护"),
    Warn("<b>⚠ 无电流闭环</b>：堵转/低速时电流仅受绕组电阻限制，注意硬件限流。"),
]

class VoltageControlPanel(_FormulaPanel):
    """电压 PWM 控制：占空比直接调制平均电压，结构简单适合宽调速。"""
    def __init__(self) -> None:
        super().__init__()
        self.vdc = _dspin(0, 1000, 24.0, 2)
        self.duty = _dspin(0, 1, 0.5, 2, 0.05)
        self.f_pwm = _dspin(1_000, 200_000, 20_000.0, 0)
        self.v_limit = _dspin(0, 1000, 24.0, 2)
        self.form.addRow("直流母线电压 (V)", self.vdc)
        self.form.addRow("占空比 (0-1)", self.duty)
        self.form.addRow("PWM 频率 (Hz)", self.f_pwm)
        self.form.addRow("电压限幅 (V)", self.v_limit)
        self.set_formula(_VOLTAGE_FORMULA)

    def values(self) -> dict:
        return {"dc_bus_voltage": self.vdc.value(),
                "duty": self.duty.value(),
                "pwm_frequency": self.f_pwm.value(),
                "voltage_limit": self.v_limit.value()}


# ─── 位置传感器参数面板 ──────────────────────────────────────
class HallPanel(_Panel):
    """霍尔传感器：3 路开关量，常用于低速 / 换相检测，分辨率 60° 电角度。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.phase = QComboBox(); self.phase.addItems(["ABC", "ACB"])
        self.poles = QSpinBox(); self.poles.setRange(1, 64); self.poles.setValue(4)
        self.deb = QSpinBox(); self.deb.setRange(0, 1000); self.deb.setValue(10)
        f.addRow("相序", self.phase)
        f.addRow("极对数", self.poles)
        f.addRow("消抖时间 (μs)", self.deb)

    def values(self) -> dict:
        return {"phase_sequence": self.phase.currentText(),
                "pole_pairs": self.poles.value(),
                "debounce_us": self.deb.value()}


class QEPPanel(_Panel):
    """增量式编码器：高分辨率角度脉冲，适合精确位置/速度反馈。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.lines = QSpinBox(); self.lines.setRange(100, 65535); self.lines.setValue(1000)
        self.dir = QComboBox(); self.dir.addItems(["+1 (正向)", "-1 (反向)"])
        self.dir.setCurrentIndex(1)
        self.idx = QCheckBox("使用 Z 相索引脉冲"); self.idx.setChecked(False)
        f.addRow("线数 / 圈", self.lines)
        f.addRow("计数方向", self.dir)
        f.addRow("索引脉冲", self.idx)

    def values(self) -> dict:
        return {"lines_per_rev": self.lines.value(),
                "direction": 1 if self.dir.currentIndex() == 0 else -1,
                "index_pulse": self.idx.isChecked()}


class ResolverPanel(_Panel):
    """旋转变压器：模拟绝对位置传感器，需要激励信号 + 解调。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.poles = QSpinBox(); self.poles.setRange(1, 32); self.poles.setValue(1)
        self.exc_f = _dspin(1_000, 50_000, 10_000.0, 0)
        self.exc_a = _dspin(0.1, 20.0, 7.0, 2, 0.1)
        self.bw = _dspin(10, 5_000, 500.0, 0)
        f.addRow("极对数", self.poles)
        f.addRow("激励频率 (Hz)", self.exc_f)
        f.addRow("激励幅值 (V)", self.exc_a)
        f.addRow("跟踪环带宽 (Hz)", self.bw)

    def values(self) -> dict:
        return {"pole_pairs": self.poles.value(),
                "excitation_freq": self.exc_f.value(),
                "excitation_amp": self.exc_a.value(),
                "tracking_bw": self.bw.value()}


class SMOPanel(_Panel):
    """滑模观测器：高速段反电动势观测，低速估算不可用。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.k = _dspin(0, 1e4, 100.0, 2)
        self.fc = _dspin(1, 5_000, 200.0, 0)
        self.thr = _dspin(0, 1_000, 50.0, 0)
        f.addRow("滑模增益 K", self.k)
        f.addRow("低通截止频率 (Hz)", self.fc)
        f.addRow("低速不可用阈值 (rpm)", self.thr)

    def values(self) -> dict:
        return {"gain_k": self.k.value(),
                "cutoff_freq": self.fc.value(),
                "low_speed_threshold": self.thr.value()}


class EKFPanel(_Panel):
    """扩展卡尔曼滤波器：递推估计角度/转速，对噪声鲁棒。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.q = _dspin(1e-6, 10.0, 0.01, 6)
        self.r = _dspin(1e-6, 10.0, 0.1, 6)
        self.p0 = _dspin(0, 100.0, 1.0, 2)
        self.thr = _dspin(0, 1_000, 50.0, 0)
        f.addRow("过程噪声 Q", self.q)
        f.addRow("观测噪声 R", self.r)
        f.addRow("初始协方差", self.p0)
        f.addRow("低速不可用阈值 (rpm)", self.thr)

    def values(self) -> dict:
        return {"q_noise": self.q.value(),
                "r_noise": self.r.value(),
                "init_covariance": self.p0.value(),
                "low_speed_threshold": self.thr.value()}


class MRASPanel(_Panel):
    """模型参考自适应：以电压方程为参考，自适应模型逼近转速。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.gain = _dspin(0, 1e4, 50.0, 2)
        self.tc = _dspin(1e-5, 1.0, 0.002, 5)
        self.thr = _dspin(0, 1_000, 50.0, 0)
        f.addRow("自适应增益", self.gain)
        f.addRow("滤波时间常数 (s)", self.tc)
        f.addRow("低速不可用阈值 (rpm)", self.thr)

    def values(self) -> dict:
        return {"adapt_gain": self.gain.value(),
                "filter_tc": self.tc.value(),
                "low_speed_threshold": self.thr.value()}


class HFIPanel(_Panel):
    """高频注入：依靠转子凸极性低速辨识位置，高速需切回反电动势。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.f_inj = _dspin(100, 10_000, 1_000.0, 0)
        self.a_inj = _dspin(0.1, 50.0, 5.0, 2, 0.1)
        self.demod = _dspin(1, 1e4, 200.0, 0)
        self.blend = _dspin(0, 5_000, 100.0, 0)
        f.addRow("注入频率 (Hz)", self.f_inj)
        f.addRow("注入幅值 (V)", self.a_inj)
        f.addRow("解调增益", self.demod)
        f.addRow("切换转速 (rpm)", self.blend)

    def values(self) -> dict:
        return {"inject_freq": self.f_inj.value(),
                "inject_amp": self.a_inj.value(),
                "demod_gain": self.demod.value(),
                "blend_speed": self.blend.value()}
