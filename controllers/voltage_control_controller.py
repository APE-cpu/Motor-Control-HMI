"""双凸极（SRM）电压 PWM 控制。

以固定 PWM 频率输出电压脉冲，占空比决定平均电压，
适合宽速域、对动态性能要求适中的场景。
"""
from .base_controller import BaseController


class VoltageControlController(BaseController):
    name = "VoltagePWM"

    def __init__(self, dc_bus_voltage: float = 48.0, duty: float = 0.5,
                 pwm_frequency: float = 20_000.0,
                 voltage_limit: float = 48.0) -> None:
        self.vdc = dc_bus_voltage
        self.duty = duty
        self.f_pwm = pwm_frequency
        self.v_limit = voltage_limit

    def update(self, target: float, feedback: float) -> float:  # noqa: ARG002
        v_out = self.vdc * self.duty
        return max(-self.v_limit, min(self.v_limit, v_out))

    def set_params(self, **kwargs) -> None:
        mapping = {"dc_bus_voltage": "vdc",
                   "duty": "duty",
                   "pwm_frequency": "f_pwm",
                   "voltage_limit": "v_limit"}
        for k, attr in mapping.items():
            if k in kwargs:
                setattr(self, attr, float(kwargs[k]))

    def reset(self) -> None:
        pass
