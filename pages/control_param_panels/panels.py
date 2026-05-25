"""参数面板基类 + 各控制方式的参数面板。"""
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QSpinBox, QWidget,
)

from config.config import SENSORLESS_METHODS


class _Panel(QWidget):
    def values(self) -> dict:
        """返回所有可调参数的当前值。"""
        raise NotImplementedError


class PIPanel(_Panel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.kp = QDoubleSpinBox(); self.kp.setRange(0, 1e4); self.kp.setDecimals(4); self.kp.setValue(1.0)
        self.ki = QDoubleSpinBox(); self.ki.setRange(0, 1e4); self.ki.setDecimals(4); self.ki.setValue(0.1)
        self.kd = QDoubleSpinBox(); self.kd.setRange(0, 1e4); self.kd.setDecimals(4); self.kd.setValue(0.0)
        self.dt = QDoubleSpinBox(); self.dt.setRange(1e-6, 1.0); self.dt.setDecimals(6); self.dt.setValue(0.001)
        f.addRow("比例系数 Kp", self.kp)
        f.addRow("积分系数 Ki", self.ki)
        f.addRow("微分系数 Kd", self.kd)
        f.addRow("采样时间 (s)", self.dt)

    def values(self) -> dict:
        return {"kp": self.kp.value(), "ki": self.ki.value(),
                "kd": self.kd.value(), "sample_time": self.dt.value()}


class OpenLoopPanel(_Panel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.amp = QDoubleSpinBox(); self.amp.setRange(0, 1000); self.amp.setValue(24.0)
        self.freq = QDoubleSpinBox(); self.freq.setRange(0, 1000); self.freq.setValue(50.0)
        self.duty = QDoubleSpinBox(); self.duty.setRange(0, 1); self.duty.setSingleStep(0.05); self.duty.setValue(0.5)
        f.addRow("电压/电流幅值", self.amp)
        f.addRow("频率 (Hz)", self.freq)
        f.addRow("占空比 (0-1)", self.duty)

    def values(self) -> dict:
        return {"amplitude": self.amp.value(),
                "frequency": self.freq.value(),
                "duty": self.duty.value()}


class MPCPanel(_Panel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.mpc_type = QComboBox(); self.mpc_type.addItems(["连续集(CCS)", "有限集(FCS)"])
        self.N = QSpinBox(); self.N.setRange(1, 200); self.N.setValue(10)
        self.M = QSpinBox(); self.M.setRange(1, 200); self.M.setValue(3)
        self.q = QDoubleSpinBox(); self.q.setRange(0, 1e6); self.q.setValue(1.0)
        self.r = QDoubleSpinBox(); self.r.setRange(0, 1e6); self.r.setValue(0.1)
        self.ecr = QDoubleSpinBox(); self.ecr.setRange(0, 1e6); self.ecr.setDecimals(4); self.ecr.setValue(1e4)
        self.umin = QDoubleSpinBox(); self.umin.setRange(-1e6, 1e6); self.umin.setValue(-24.0)
        self.umax = QDoubleSpinBox(); self.umax.setRange(-1e6, 1e6); self.umax.setValue(24.0)
        self.dumax = QDoubleSpinBox(); self.dumax.setRange(0, 1e6); self.dumax.setValue(5.0)
        self.xmin = QDoubleSpinBox(); self.xmin.setRange(-1e6, 1e6); self.xmin.setValue(-3000.0)
        self.xmax = QDoubleSpinBox(); self.xmax.setRange(-1e6, 1e6); self.xmax.setValue(3000.0)
        f.addRow("集合类型", self.mpc_type)
        f.addRow("预测时域 N", self.N)
        f.addRow("控制时域 M", self.M)
        f.addRow("权重 Q (状态)", self.q)
        f.addRow("权重 R (控制)", self.r)
        f.addRow("ECR (约束松弛)", self.ecr)
        f.addRow("约束 u_min", self.umin)
        f.addRow("约束 u_max", self.umax)
        f.addRow("约束 Δu_max", self.dumax)
        f.addRow("状态约束 x_min", self.xmin)
        f.addRow("状态约束 x_max", self.xmax)

    def values(self) -> dict:
        return {"mpc_type": self.mpc_type.currentText(),
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


class SensorlessPanel(_Panel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.gain = QDoubleSpinBox(); self.gain.setRange(0, 1e6); self.gain.setValue(100.0)
        self.method = QComboBox(); self.method.addItems(SENSORLESS_METHODS)
        self.start_freq = QDoubleSpinBox(); self.start_freq.setRange(0, 1000); self.start_freq.setValue(5.0)
        self.start_curr = QDoubleSpinBox(); self.start_curr.setRange(0, 1000); self.start_curr.setValue(2.0)
        f.addRow("观测器增益", self.gain)
        f.addRow("估算方法", self.method)
        f.addRow("启动频率 (Hz)", self.start_freq)
        f.addRow("启动电流 (A)", self.start_curr)

    def values(self) -> dict:
        return {"observer_gain": self.gain.value(),
                "method": self.method.currentText(),
                "start_freq": self.start_freq.value(),
                "start_current": self.start_curr.value()}


# ─── 双凸极电机专属面板 ──────────────────────────────────────
class CurrentChoppingPanel(_Panel):
    """电流斩波控制（CCC）：低速重载常用，电流滞环维持在 [i_lower, i_upper]。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.i_up = QDoubleSpinBox(); self.i_up.setRange(0, 1000); self.i_up.setValue(8.0)
        self.i_low = QDoubleSpinBox(); self.i_low.setRange(0, 1000); self.i_low.setValue(6.0)
        self.f_chop = QDoubleSpinBox(); self.f_chop.setRange(1, 200_000); self.f_chop.setValue(10_000.0)
        self.band = QDoubleSpinBox(); self.band.setRange(0, 100); self.band.setSingleStep(0.1); self.band.setValue(0.5)
        f.addRow("电流上限 i_upper (A)", self.i_up)
        f.addRow("电流下限 i_lower (A)", self.i_low)
        f.addRow("斩波频率 (Hz)", self.f_chop)
        f.addRow("滞环带宽 (A)", self.band)

    def values(self) -> dict:
        return {"current_upper": self.i_up.value(),
                "current_lower": self.i_low.value(),
                "chopping_frequency": self.f_chop.value(),
                "hysteresis_band": self.band.value()}


class AnglePositionPanel(_Panel):
    """角度位置控制（APC）：依据转子角度开通/关断，可设提前角。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.theta_on = QDoubleSpinBox(); self.theta_on.setRange(-90, 90); self.theta_on.setValue(5.0)
        self.theta_off = QDoubleSpinBox(); self.theta_off.setRange(-90, 90); self.theta_off.setValue(25.0)
        self.theta_adv = QDoubleSpinBox(); self.theta_adv.setRange(-30, 30); self.theta_adv.setValue(0.0)
        self.i_limit = QDoubleSpinBox(); self.i_limit.setRange(0, 1000); self.i_limit.setValue(8.0)
        f.addRow("开通角 θ_on (°)", self.theta_on)
        f.addRow("关断角 θ_off (°)", self.theta_off)
        f.addRow("提前角 θ_adv (°)", self.theta_adv)
        f.addRow("限流值 (A)", self.i_limit)

    def values(self) -> dict:
        return {"turn_on_angle": self.theta_on.value(),
                "turn_off_angle": self.theta_off.value(),
                "advance_angle": self.theta_adv.value(),
                "current_limit": self.i_limit.value()}


class VoltageControlPanel(_Panel):
    """电压 PWM 控制：占空比直接调制平均电压，结构简单适合宽调速。"""
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.vdc = QDoubleSpinBox(); self.vdc.setRange(0, 1000); self.vdc.setValue(48.0)
        self.duty = QDoubleSpinBox(); self.duty.setRange(0, 1); self.duty.setSingleStep(0.05); self.duty.setValue(0.5)
        self.f_pwm = QDoubleSpinBox(); self.f_pwm.setRange(1_000, 200_000); self.f_pwm.setValue(20_000.0)
        self.v_limit = QDoubleSpinBox(); self.v_limit.setRange(0, 1000); self.v_limit.setValue(48.0)
        f.addRow("直流母线电压 (V)", self.vdc)
        f.addRow("占空比 (0-1)", self.duty)
        f.addRow("PWM 频率 (Hz)", self.f_pwm)
        f.addRow("电压限幅 (V)", self.v_limit)

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
        self.lines = QSpinBox(); self.lines.setRange(100, 65535); self.lines.setValue(2500)
        self.dir = QComboBox(); self.dir.addItems(["+1 (正向)", "-1 (反向)"])
        self.idx = QCheckBox("使用 Z 相索引脉冲"); self.idx.setChecked(True)
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
        self.exc_f = QDoubleSpinBox(); self.exc_f.setRange(1_000, 50_000); self.exc_f.setValue(10_000.0)
        self.exc_a = QDoubleSpinBox(); self.exc_a.setRange(0.1, 20.0); self.exc_a.setSingleStep(0.1); self.exc_a.setValue(7.0)
        self.bw = QDoubleSpinBox(); self.bw.setRange(10, 5_000); self.bw.setValue(500.0)
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
        self.k = QDoubleSpinBox(); self.k.setRange(0, 1e4); self.k.setValue(100.0)
        self.fc = QDoubleSpinBox(); self.fc.setRange(1, 5_000); self.fc.setValue(200.0)
        self.thr = QDoubleSpinBox(); self.thr.setRange(0, 1_000); self.thr.setValue(50.0)
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
        self.q = QDoubleSpinBox(); self.q.setRange(1e-6, 10.0); self.q.setDecimals(6); self.q.setValue(0.01)
        self.r = QDoubleSpinBox(); self.r.setRange(1e-6, 10.0); self.r.setDecimals(6); self.r.setValue(0.1)
        self.p0 = QDoubleSpinBox(); self.p0.setRange(0, 100.0); self.p0.setValue(1.0)
        self.thr = QDoubleSpinBox(); self.thr.setRange(0, 1_000); self.thr.setValue(50.0)
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
        self.gain = QDoubleSpinBox(); self.gain.setRange(0, 1e4); self.gain.setValue(50.0)
        self.tc = QDoubleSpinBox(); self.tc.setRange(1e-5, 1.0); self.tc.setDecimals(5); self.tc.setValue(0.002)
        self.thr = QDoubleSpinBox(); self.thr.setRange(0, 1_000); self.thr.setValue(50.0)
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
        self.f_inj = QDoubleSpinBox(); self.f_inj.setRange(100, 10_000); self.f_inj.setValue(1_000.0)
        self.a_inj = QDoubleSpinBox(); self.a_inj.setRange(0.1, 50.0); self.a_inj.setSingleStep(0.1); self.a_inj.setValue(5.0)
        self.demod = QDoubleSpinBox(); self.demod.setRange(1, 1e4); self.demod.setValue(200.0)
        self.blend = QDoubleSpinBox(); self.blend.setRange(0, 5_000); self.blend.setValue(100.0)
        f.addRow("注入频率 (Hz)", self.f_inj)
        f.addRow("注入幅值 (V)", self.a_inj)
        f.addRow("解调增益", self.demod)
        f.addRow("切换转速 (rpm)", self.blend)

    def values(self) -> dict:
        return {"inject_freq": self.f_inj.value(),
                "inject_amp": self.a_inj.value(),
                "demod_gain": self.demod.value(),
                "blend_speed": self.blend.value()}
