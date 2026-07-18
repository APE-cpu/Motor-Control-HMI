"""速度环旁路、电流环闭环调试控制器。"""

from .base_controller import BaseController


class OpenLoopController(BaseController):
    name = "OpenLoop"

    def __init__(self, iq_ref_a: float = 0.0, kp_cur: float = 2323,
                 ki_cur: float = 2077, iq_ramp_ms: float = 500, **_) -> None:
        self.iq_ref_a = iq_ref_a
        self.kp_cur = kp_cur
        self.ki_cur = ki_cur
        self.iq_ramp_ms = iq_ramp_ms

    def update(self, target: float, feedback: float) -> float:  # noqa: ARG002
        return self.iq_ref_a

    def set_params(self, **kwargs) -> None:
        for k in ("iq_ref_a", "kp_cur", "ki_cur", "iq_ramp_ms"):
            if k in kwargs:
                setattr(self, k, float(kwargs[k]))

    def reset(self) -> None:
        pass
