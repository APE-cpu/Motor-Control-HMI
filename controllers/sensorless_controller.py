"""无位置传感器控制：四种估算方法的差异化实现。

实际工程中需结合电机模型与采样数据进行严格推导，本实现保持统一接口
并对 SMO / EKF / MRAS / HFI 给出能够区分行为的简化版本，便于上位机演示。
"""
import math

from .base_controller import BaseController


class SensorlessController(BaseController):
    name = "Sensorless"

    def __init__(self, observer_gain: float = 100.0, method: str = "滑模观测器",
                 start_freq: float = 5.0, start_current: float = 2.0,
                 **kwargs) -> None:
        self.k_obs = observer_gain
        self.method = method
        self.start_freq = start_freq
        self.start_current = start_current
        self._theta = 0.0
        self._omega = 0.0
        self._dt = 0.001
        # 各 method 独有参数（由上层透传，无则用默认）
        self.cutoff_freq = float(kwargs.get("cutoff_freq", 200.0))
        self.q_noise = float(kwargs.get("q_noise", 0.01))
        self.r_noise = float(kwargs.get("r_noise", 0.1))
        self.adapt_gain = float(kwargs.get("adapt_gain", 50.0))
        self.filter_tc = float(kwargs.get("filter_tc", 0.002))
        self.inject_freq = float(kwargs.get("inject_freq", 1_000.0))
        self.inject_amp = float(kwargs.get("inject_amp", 5.0))
        self.demod_gain = float(kwargs.get("demod_gain", 200.0))
        # EKF / MRAS 内部状态
        self._ekf_x = 0.0
        self._ekf_p = float(kwargs.get("init_covariance", 1.0))
        self._mras_ref = 0.0
        self._mras_adp = 0.0
        self._t = 0.0

    def estimate_position(self, i_alpha: float, i_beta: float) -> float:
        """从两相电流估算转子角度，按 method 分支。"""
        base = math.atan2(i_beta, i_alpha)
        self._t += self._dt

        if self.method == "滑模观测器":
            # 在反正切结果上叠加滑模切换扰动（演示用）
            sgn = 1.0 if base >= self._theta else -1.0
            theta = base + 0.02 * sgn / max(1.0, self.cutoff_freq / 100.0)

        elif self.method == "扩展卡尔曼":
            # 1D EKF 标量滤波：状态=角度，观测=base
            self._ekf_p = self._ekf_p + self.q_noise
            k_gain = self._ekf_p / (self._ekf_p + self.r_noise)
            self._ekf_x = self._ekf_x + k_gain * (base - self._ekf_x)
            self._ekf_p = (1.0 - k_gain) * self._ekf_p
            theta = self._ekf_x

        elif self.method == "模型参考自适应":
            # 参考模型直接用 base，自适应模型由 adapt_gain 跟踪
            self._mras_ref = base
            err = self._mras_ref - self._mras_adp
            self._mras_adp += self.adapt_gain * err * self._dt
            theta = self._mras_adp

        elif self.method == "高频注入":
            # base 上叠加注入残余项（与注入幅值成比例）
            inj = self.inject_amp * 0.001 * math.sin(2 * math.pi * self.inject_freq * self._t)
            theta = base + inj

        else:
            theta = base

        self._theta = theta
        return theta

    def update(self, target: float, feedback: float) -> float:
        # 简化：把 target 视为目标转速，feedback 视为估算转速
        err = target - feedback
        self._omega += self.k_obs * err * self._dt
        self._theta = (self._theta + self._omega * self._dt) % (2 * math.pi)
        return self._omega

    def set_params(self, **kwargs) -> None:
        if "observer_gain" in kwargs:
            self.k_obs = float(kwargs["observer_gain"])
        if "method" in kwargs:
            self.method = str(kwargs["method"])
        if "start_freq" in kwargs:
            self.start_freq = float(kwargs["start_freq"])
        if "start_current" in kwargs:
            self.start_current = float(kwargs["start_current"])
        if "sample_time" in kwargs:
            self._dt = float(kwargs["sample_time"])
        # 估算方法特有参数
        for k in ("cutoff_freq", "q_noise", "r_noise", "adapt_gain",
                  "filter_tc", "inject_freq", "inject_amp", "demod_gain"):
            if k in kwargs:
                setattr(self, k, float(kwargs[k]))
        if "init_covariance" in kwargs:
            self._ekf_p = float(kwargs["init_covariance"])

    def reset(self) -> None:
        self._theta = 0.0
        self._omega = 0.0
        self._ekf_x = 0.0
        self._mras_ref = 0.0
        self._mras_adp = 0.0
        self._t = 0.0
