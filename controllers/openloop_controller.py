"""开环控制器：按设定的幅值/频率/占空比直接输出。"""
import math

from .base_controller import BaseController


class OpenLoopController(BaseController):
    name = "OpenLoop"

    def __init__(self, amplitude: float = 24.0, frequency: float = 50.0,
                 duty: float = 0.5) -> None:
        self.amplitude = amplitude
        self.frequency = frequency
        self.duty = duty
        self._t = 0.0
        self._dt = 0.001

    def update(self, target: float, feedback: float) -> float:  # noqa: ARG002
        # 开环：不使用 feedback；按正弦/方波叠加给出参考电压
        self._t += self._dt
        # 简单实现：占空比加权的正弦
        wave = math.sin(2 * math.pi * self.frequency * self._t)
        return self.amplitude * (self.duty * 1.0 + (1 - self.duty) * wave)

    def set_params(self, **kwargs) -> None:
        for k in ("amplitude", "frequency", "duty"):
            if k in kwargs:
                setattr(self, k, float(kwargs[k]))
        if "sample_time" in kwargs:
            self._dt = float(kwargs["sample_time"])

    def reset(self) -> None:
        self._t = 0.0
