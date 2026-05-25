"""增量式 PID 控制器（含 P/I/D 与采样时间）。"""
from .base_controller import BaseController


class PIController(BaseController):
    name = "PI/PID"

    def __init__(self, kp: float = 1.0, ki: float = 0.1, kd: float = 0.0,
                 sample_time: float = 0.001,
                 out_min: float = -1e6, out_max: float = 1e6) -> None:
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = sample_time
        self.out_min = out_min
        self.out_max = out_max
        self._integral = 0.0
        self._prev_err = 0.0

    def update(self, target: float, feedback: float) -> float:
        err = target - feedback
        self._integral += err * self.dt
        derivative = (err - self._prev_err) / self.dt if self.dt > 0 else 0.0
        out = self.kp * err + self.ki * self._integral + self.kd * derivative
        # 输出限幅 + 抗积分饱和
        if out > self.out_max:
            out = self.out_max
            self._integral -= err * self.dt
        elif out < self.out_min:
            out = self.out_min
            self._integral -= err * self.dt
        self._prev_err = err
        return out

    def set_params(self, **kwargs) -> None:
        for k in ("kp", "ki", "kd"):
            if k in kwargs:
                setattr(self, k, float(kwargs[k]))
        if "sample_time" in kwargs:
            self.dt = float(kwargs["sample_time"])
        if "out_min" in kwargs:
            self.out_min = float(kwargs["out_min"])
        if "out_max" in kwargs:
            self.out_max = float(kwargs["out_max"])

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_err = 0.0
