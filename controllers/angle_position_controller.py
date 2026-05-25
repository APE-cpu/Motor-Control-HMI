"""双凸极（SRM）角度位置控制（APC）。

依据转子角度决定开通/关断：当角度落在 [theta_on, theta_off] 区间时
开通相绕组施加直流母线电压，常用于中高速场景，并可通过提前角实现弱磁。
"""
from .base_controller import BaseController


class AnglePositionController(BaseController):
    name = "APC"

    def __init__(self, turn_on_angle: float = 5.0, turn_off_angle: float = 25.0,
                 advance_angle: float = 0.0, current_limit: float = 8.0) -> None:
        self.theta_on = turn_on_angle
        self.theta_off = turn_off_angle
        self.theta_adv = advance_angle
        self.i_limit = current_limit

    def update(self, target: float, feedback: float) -> float:  # noqa: ARG002
        # feedback 视为当前机械角度（deg），返回 1 表示开通
        theta = feedback - self.theta_adv
        on = self.theta_on <= theta <= self.theta_off
        return 1.0 if on else 0.0

    def set_params(self, **kwargs) -> None:
        mapping = {"turn_on_angle": "theta_on",
                   "turn_off_angle": "theta_off",
                   "advance_angle": "theta_adv",
                   "current_limit": "i_limit"}
        for k, attr in mapping.items():
            if k in kwargs:
                setattr(self, attr, float(kwargs[k]))

    def reset(self) -> None:
        pass
