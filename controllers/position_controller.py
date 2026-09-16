"""PMSM 位置—速度—电流级联的最外层位置环。

位置环只负责把位置误差变成速度给定；速度 PI 和电流 PI 仍由下位机执行。
这与官方参数整定 App 的结构一致，避免在上位机重复实现内环。
"""
from .base_controller import BaseController


def _wrap_deg(error: float) -> float:
    """把角度误差折返到 [-180, 180) 度，避免跨零点走长路。"""
    return (float(error) + 180.0) % 360.0 - 180.0


class PositionController(BaseController):
    name = "PositionCascade"

    def __init__(self, kp_pos: float = 8.0, kd_pos: float = 0.20,
                 kpf_pos: float = 0.0,
                 sample_time: float = 0.005,
                 speed_limit_rpm: float = 300.0) -> None:
        self.kp_pos = float(kp_pos)
        self.kd_pos = float(kd_pos)
        self.kpf_pos = float(kpf_pos)
        self.dt = float(sample_time)
        self.speed_limit_rpm = abs(float(speed_limit_rpm))
        self._prev_target_deg = 0.0

    def update(self, target: float, feedback: float,
        target_speed_deg_s: float = 0.0,
        actual_speed_rpm: float = 0.0) -> float:
        error = _wrap_deg(float(target) - float(feedback))
        speed = (self.kp_pos * error - self.kd_pos * float(actual_speed_rpm) +
                 self.kpf_pos * float(target_speed_deg_s))
        limit = self.speed_limit_rpm
        if limit > 0.0 and abs(speed) > limit:
            speed = max(-limit, min(limit, speed))
        return float(speed)

    def set_params(self, **kwargs) -> None:
        for key in ("kp_pos", "kd_pos", "kpf_pos", "speed_limit_rpm"):
            if key in kwargs:
                setattr(self, key, float(kwargs[key]))
        if "pos_sample_time" in kwargs:
            self.dt = float(kwargs["pos_sample_time"])
        elif "sample_time" in kwargs:
            self.dt = float(kwargs["sample_time"])

    def reset(self) -> None:
        self._prev_target_deg = 0.0
