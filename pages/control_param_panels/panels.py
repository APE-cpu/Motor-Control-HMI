"""参数面板基类 + 各控制方式的参数面板。

控制方式面板统一为「左侧参数表单 + 右侧数学模型与参数说明」布局；
传感器参数面板保持简单表单。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from config.config import SENSORLESS_METHODS


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
        self._formula = QLabel()
        self._formula.setTextFormat(Qt.RichText)
        self._formula.setWordWrap(True)
        self._formula.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._formula.setStyleSheet(
            "QLabel { font-size: 13px; color: #c7d3e0; padding: 4px; }")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self._formula)
        bv.addWidget(scroll)
        h.addWidget(box, 3)

    def set_formula(self, html: str) -> None:
        self._formula.setText(html)


def _loop_group(title: str, rows: list) -> tuple:
    """带标题的参数子组：rows = [(标签, widget), ...]。"""
    box = QGroupBox(title)
    f = QFormLayout(box)
    for label, w in rows:
        f.addRow(label, w)
    return box


def _dspin(mn, mx, val, decimals=4, step=None):
    sp = QDoubleSpinBox()
    sp.setRange(mn, mx)
    sp.setDecimals(decimals)
    sp.setValue(val)
    if step:
        sp.setSingleStep(step)
    return sp


# ─── 公式排版辅助（Qt 富文本） ───────────────────────────────
def _sec(title: str) -> str:
    """小节标题：蓝色加粗。"""
    return (f"<p style='color:#4fc3f7; font-weight:bold; font-size:14px;"
            f" margin:12px 0 4px 0;'>{title}</p>")


def _fx(*lines: str) -> str:
    """公式块：深色底、衬线数学字体、加大字号、行距。"""
    body = "".join(
        f"<div style='margin:5px 2px;'>{ln}</div>" for ln in lines)
    return ("<table width='100%' cellspacing='0' cellpadding='10'"
            " bgcolor='#10131a'><tr><td style=\"font-family:'Cambria Math',"
            "'STIX Two Math','Times New Roman',serif; font-size:16px;"
            " color:#e8f1ff;\">" + body + "</td></tr></table>")


def _txt(text: str) -> str:
    """正文说明行。"""
    return f"<p style='margin:6px 0; color:#c7d3e0;'>{text}</p>"


def _note(*items: str) -> str:
    """参数说明列表。"""
    lis = "".join(
        f"<li style='margin:5px 0; color:#aebccb;'>{i}</li>" for i in items)
    return f"<ul style='margin:4px 0;'>{lis}</ul>"


# ─── 闭环 PI（转速外环 + 电流内环级联） ─────────────────────────
_PI_FORMULA = (
    _txt("<b>级联双闭环</b>：转速外环输出电流给定，电流内环输出电压。")
    + _sec("转速环（外环）")
    + _fx("e<sub>ω</sub> = ω* − ω",
          "i<sub>q</sub>* = K<sub>pω</sub>·e<sub>ω</sub> + "
          "K<sub>iω</sub>·∫e<sub>ω</sub> dt",
          "|i<sub>q</sub>*| ≤ i<sub>qmax</sub>&nbsp;&nbsp;（输出限幅）")
    + _sec("电流环（内环，含 dq 解耦前馈）")
    + _fx("v<sub>d</sub> = K<sub>pi</sub>·e<sub>d</sub> + "
          "K<sub>ii</sub>·∫e<sub>d</sub> dt − ω<sub>e</sub>L<sub>q</sub>i<sub>q</sub>",
          "v<sub>q</sub> = K<sub>pi</sub>·e<sub>q</sub> + "
          "K<sub>ii</sub>·∫e<sub>q</sub> dt + ω<sub>e</sub>(L<sub>d</sub>i<sub>d</sub>"
          " + ψ<sub>f</sub>)")
    + _txt("其中 e<sub>d</sub> = i<sub>d</sub>* − i<sub>d</sub>，"
           "e<sub>q</sub> = i<sub>q</sub>* − i<sub>q</sub>；表贴式取 i<sub>d</sub>* = 0。")
    + _sec("参数说明")
    + _note(
        "K<sub>pω</sub>/K<sub>iω</sub>：转速环增益。Kp 大→响应快但易超调；"
        "Ki 消除稳态误差，过大引起振荡",
        "i<sub>qmax</sub>：转速环输出限幅 = 最大转矩电流，兼作过流保护；"
        "限幅期间冻结积分（抗饱和）",
        "K<sub>pi</sub>/K<sub>ii</sub>：电流环增益，按带宽整定 "
        "K<sub>pi</sub> = L·ω<sub>bw</sub>，K<sub>ii</sub> = R·ω<sub>bw</sub>",
        "采样时间：内环带宽须远高于外环（≥10 倍），典型电流环 10~20 kHz、"
        "转速环 1 kHz"))


class PIPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.kp_spd = _dspin(0, 1e4, 1752, 0)
        self.ki_spd = _dspin(0, 1e4, 121, 0)
        self.kd_spd = _dspin(0, 1e4, 0.0)
        self.iq_max = _dspin(0, 4.49, 1.887, 3)
        self.dt_spd = _dspin(1e-6, 1.0, 0.002, 6)
        self.dt_spd.setReadOnly(True)
        self.dt_spd.setToolTip("下位机固定500 Hz；界面不可修改")
        self.left_v.insertWidget(0, _loop_group("转速环（外环）", [
            ("比例 Kpω（下位机整数）", self.kp_spd),
            ("积分 Kiω（下位机整数）", self.ki_spd),
            ("微分 Kdω", self.kd_spd),
            ("输出限幅 iq_max (A)", self.iq_max),
            ("采样时间 (s)", self.dt_spd),
        ]))
        self.kp_cur = _dspin(0, 1e4, 2323, 0)
        self.ki_cur = _dspin(0, 1e6, 2077, 0)
        self.dt_cur = _dspin(1e-7, 1.0, 0.0000625, 7)
        self.dt_cur.setReadOnly(True)
        self.dt_cur.setToolTip("下位机固定16 kHz；每个PWM周期执行一次")
        self.left_v.insertWidget(1, _loop_group("电流环（内环）", [
            ("比例 Kpi（下位机整数）", self.kp_cur),
            ("积分 Kii（下位机整数）", self.ki_cur),
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


# ─── 速度开环 / 电流闭环调试 ───────────────────────────────
_OPENLOOP_FORMULA = (
    _txt("<b>速度环旁路，d/q 电流环保持闭环</b>。直接给定 Iqref，"
         "用于电流环 PI 整定；这不是 V/f 开环运行。")
    + _sec("电流闭环")
    + _fx("e<sub>q</sub> = I<sub>qref</sub> − I<sub>q</sub>",
          "V<sub>q</sub> = K<sub>pi</sub>e<sub>q</sub> + "
          "K<sub>ii</sub>∫e<sub>q</sub>dt",
          "I<sub>dref</sub> = 0")
    + _sec("参数说明")
    + _note(
        "Iqref：q轴转矩电流给定；空载电机会向任一方向加速，不能把它当速度给定",
        "Kpi/Kii：电流内环整数增益；运行中每次只允许修改 ±10%",
        "斜坡时间：改变 Iqref 时的过渡时间，避免电流阶跃过猛")
    + _txt("<b style='color:#ff8a65'>⚠ 禁止空载长时间运行</b>：本模式仅允许固定转子测试或"
           "极短低电流脉冲；超过 100 rpm 下位机将独立切断功率级。"))


class OpenLoopPanel(_FormulaPanel):
    def __init__(self) -> None:
        super().__init__()
        self.iq_ref = _dspin(-0.5, 0.5, 0.00, 3, 0.01)
        self.kp_cur = _dspin(0, 10000, 2323, 0)
        self.ki_cur = _dspin(0, 10000, 2077, 0)
        self.ramp_ms = _dspin(100, 5000, 500, 0, 100)
        self.form.addRow("Iqref (A)", self.iq_ref)
        self.form.addRow("电流环 Kpi（下位机整数）", self.kp_cur)
        self.form.addRow("电流环 Kii（下位机整数）", self.ki_cur)
        period = QLabel("62.5 µs（16 kHz，每个PWM周期执行一次）")
        period.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        self.form.addRow("电流环控制周期", period)
        self.form.addRow("Iq 斜坡时间 (ms)", self.ramp_ms)
        self.set_formula(_OPENLOOP_FORMULA)

    def values(self) -> dict:
        return {"control_mode": "current_loop_test",
                "iq_ref_a": self.iq_ref.value(),
                "kp_cur": self.kp_cur.value(),
                "ki_cur": self.ki_cur.value(),
                "iq_ramp_ms": self.ramp_ms.value()}


# ─── MPC ───────────────────────────────────────────────────
_MPC_LOOPS = ["电流环（转速环用 PI）", "转速环（电流环用 PI）", "转速+电流双环"]

_FCS_FORMULA = (
    _txt("<b>FCS-MPC（有限集）</b>：每个控制周期遍历逆变器 8 个基本电压矢量"
         " u ∈ {{V<sub>0</sub> … V<sub>7</sub>}}，取代价最小者直接输出（无调制器）。")
    + _sec("预测模型（dq 电流方程，前向欧拉，T<sub>s</sub> 为控制周期）")
    + _fx("i<sub>d</sub>(k+1) = i<sub>d</sub> + T<sub>s</sub>/L<sub>d</sub> · "
          "[ v<sub>d</sub> − R<sub>s</sub>i<sub>d</sub> + "
          "ω<sub>e</sub>L<sub>q</sub>i<sub>q</sub> ]",
          "i<sub>q</sub>(k+1) = i<sub>q</sub> + T<sub>s</sub>/L<sub>q</sub> · "
          "[ v<sub>q</sub> − R<sub>s</sub>i<sub>q</sub> − "
          "ω<sub>e</sub>(L<sub>d</sub>i<sub>d</sub> + ψ<sub>f</sub>) ]")
    + _sec("价值函数（预测 N 步）")
    + _fx("J = Σ<sub>k=1..N</sub> q·[ (i<sub>d</sub>* − i<sub>d</sub>(k))² + "
          "(i<sub>q</sub>* − i<sub>q</sub>(k))² ] + r·‖Δu(k)‖² + I<sub>lim</sub>")
    + _sec("约束处理")
    + _fx("I<sub>lim</sub> = 0&nbsp;（|i| ≤ i<sub>max</sub>）；"
          "否则 I<sub>lim</sub> = ECR&nbsp;（大罚值，等效剔除越限矢量）")
    + "{loop_note}"
    + _sec("参数说明")
    + _note(
        "N/M：预测/控制时域。N 大→前瞻多但计算量按 8<sup>N</sup> 增长，"
        "FCS 常用 N = 1~2",
        "q/r：跟踪误差与开关变化的权重比。r 越大开关频率越低（损耗小、纹波大）",
        "ECR：约束违反罚值，越大越接近硬约束",
        "u/Δu/x 约束：FCS 中 u 天然离散有界，x 约束以罚项进入价值函数"))

_CCS_FORMULA = (
    _txt("<b>CCS-MPC（连续集）</b>：解二次规划得连续电压矢量，经 SVPWM 调制"
         "输出，开关频率固定。")
    + _sec("优化问题（滚动时域，每周期只执行 u(0)）")
    + _fx("min&nbsp; J = Σ<sub>k=1..N</sub> ‖x(k) − x*‖²<sub>Q</sub> + "
          "Σ<sub>k=0..M−1</sub> ‖Δu(k)‖²<sub>R</sub> + ECR·ε²")
    + _sec("约束条件 s.t.")
    + _fx("x(k+1) = A·x(k) + B·u(k)&nbsp;&nbsp;（线性化预测模型）",
          "u<sub>min</sub> ≤ u ≤ u<sub>max</sub>，|Δu| ≤ Δu<sub>max</sub>"
          "&nbsp;&nbsp;（硬约束）",
          "x<sub>min</sub> − ε ≤ x ≤ x<sub>max</sub> + ε，ε ≥ 0"
          "&nbsp;&nbsp;（软约束，ε 为松弛变量）")
    + "{loop_note}"
    + _sec("参数说明")
    + _note(
        "N：预测时域，应覆盖被控对象主导时间常数；M ≤ N，M 之后控制量保持不变",
        "Q/R：状态误差与控制增量权重。Q/R 大→跟踪快、控制猛；小→平滑、省能量",
        "ECR：松弛惩罚，防止约束冲突导致 QP 无解",
        "u/Δu：执行器幅值与速率约束；x：状态（转速/电流）安全范围"))

_LOOP_NOTES = {
    _MPC_LOOPS[0]: _sec("环路结构") + _txt(
        "MPC 替代<b>电流环</b>；转速环仍用 PI，其输出 i<sub>q</sub>* "
        "作为 MPC 的电流参考。"),
    _MPC_LOOPS[1]: _sec("环路结构") + _txt(
        "MPC 替代<b>转速环</b>，输出 i<sub>q</sub>* 给下级 PI 电流环执行。"),
    _MPC_LOOPS[2]: _sec("环路结构") + _txt(
        "单一 MPC 同时优化转速与电流（状态向量含 ω 和 i<sub>dq</sub>），"
        "无内外环级联。"),
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
        note = _LOOP_NOTES.get(self.loop.currentText(), "")
        tmpl = _FCS_FORMULA if "FCS" in self.mpc_type.currentText() else _CCS_FORMULA
        self.set_formula(tmpl.format(loop_note=note))

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
_SENSORLESS_FORMULA = (
    _txt("<b>环路结构：控制律仍为 PI 双闭环</b>（同「闭环PI控制」），仅位置/"
         "转速反馈由观测器估计值 θ̂、ω̂ 替代物理传感器。观测器方程随所选方法"
         "而异（SMO/EKF/MRAS/HFI，原理详见「传感器详情 / 自检」）。")
    + _sec("I/f 强拖启动（反电动势法低速不可观）")
    + _fx("θ<sub>e</sub> = 2π ∫f<sub>start</sub> dt，注入恒流 i<sub>start</sub>",
          "转速爬升 → 观测器收敛 → 切入闭环")
    + _sec("参数说明")
    + _note(
        "观测器增益：收敛速度与噪声/抖振的折中，过大易振荡",
        "估算方法：SMO 鲁棒/中高速，EKF 平滑/计算量大，MRAS 参数敏感，"
        "HFI 零低速可用",
        "启动频率：强拖阶段的电角频率斜坡终值",
        "启动电流：强拖注入电流，需克服负载转矩，过大发热"))


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
_CCC_FORMULA = (
    _txt("<b>电流斩波控制（CCC）</b>：低速段转矩控制，滞环把相电流限制在带内。")
    + _sec("滞环开关律（导通区间内）")
    + _fx("i &lt; i<sub>lower</sub> → 开通（+U<sub>dc</sub>）",
          "i &gt; i<sub>upper</sub> → 关断（0 或 −U<sub>dc</sub> 续流）")
    + _sec("磁阻转矩")
    + _fx("T = ½ · i² · dL(θ)/dθ&nbsp;&nbsp;（电感上升区通电得正转矩）")
    + _sec("参数说明")
    + _note(
        "i<sub>upper</sub>/i<sub>lower</sub>：滞环上下限，差值决定实际斩波频率与纹波",
        "斩波频率：开关频率上限（保护功率管），滞环自然频率高于此值时强制限频",
        "滞环带宽：带宽小→电流平滑但开关损耗大"))


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


_APC_FORMULA = (
    _txt("<b>角度位置控制（APC）</b>：中高速段主流方式，按转子位置角决定各相"
         "开通/关断，导通期内电压全开（单脉冲）。")
    + _sec("导通逻辑（对每相，考虑提前角）")
    + _fx("θ<sub>on</sub> − θ<sub>adv</sub> ≤ θ &lt; θ<sub>off</sub> − "
          "θ<sub>adv</sub>&nbsp;&nbsp;→ 该相通电")
    + _sec("磁阻转矩")
    + _fx("T = ½ · i² · dL(θ)/dθ",
          "平均转矩由 θ<sub>on</sub>/θ<sub>off</sub> 与转速共同决定")
    + _sec("参数说明")
    + _note(
        "θ<sub>on</sub>：开通角。提前开通让电流在电感上升区前建立",
        "θ<sub>off</sub>：关断角。过迟→电流拖入电感下降区产生负转矩",
        "θ<sub>adv</sub>：提前角，随转速增大而增大（补偿电流建立时间 ≈ L·i/U）",
        "限流值：防止低速单脉冲模式下电流失控"))


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


_VOLTAGE_FORMULA = (
    _txt("<b>电压 PWM 控制</b>：占空比直接调制绕组平均电压，无电流/转速闭环"
         "（或仅留外部限流保护）。")
    + _sec("控制律")
    + _fx("U<sub>avg</sub> = D · U<sub>dc</sub>",
          "n ≈ (U<sub>avg</sub> − I·R) / k<sub>e</sub>"
          "&nbsp;&nbsp;（稳态近似，随负载下垂）")
    + _sec("参数说明")
    + _note(
        "直流母线电压 U<sub>dc</sub>：调制的电压基准",
        "占空比 D：0~1，直接决定平均电压",
        "PWM 频率：高→电流纹波小、开关损耗大；典型 10~20 kHz（避开可听频段）",
        "电压限幅：输出电压上限保护")
    + _txt("<b style='color:#ff8a65'>⚠ 无电流闭环</b>：堵转/低速时电流仅受"
           "绕组电阻限制，注意硬件限流。"))


class VoltageControlPanel(_FormulaPanel):
    """电压 PWM 控制：占空比直接调制平均电压，结构简单适合宽调速。"""
    def __init__(self) -> None:
        super().__init__()
        self.vdc = _dspin(0, 1000, 48.0, 2)
        self.duty = _dspin(0, 1, 0.5, 2, 0.05)
        self.f_pwm = _dspin(1_000, 200_000, 20_000.0, 0)
        self.v_limit = _dspin(0, 1000, 48.0, 2)
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
