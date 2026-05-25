"""双凸极（SRM）电流斩波控制（CCC）。

工作原理（简化）：在转子位于导通区间内时，对相绕组施加直流母线电压，
当相电流达到上限 i_upper 时关断；下降到下限 i_lower 时再次开通，
从而把电流维持在 [i_lower, i_upper] 的滞环带内，常用于低速重载。
"""
from .base_controller import BaseController


class CurrentChoppingController(BaseController):
    name = "CCC"

    def __init__(self, current_upper: float = 8.0, current_lower: float = 6.0,
                 chopping_frequency: float = 10_000.0,
                 hysteresis_band: float = 0.5) -> None:
        self.i_upper = current_upper
        self.i_lower = current_lower
        self.f_chop = chopping_frequency
        self.band = hysteresis_band
        self._gate_on = False

    def update(self, target: float, feedback: float) -> float:  # noqa: ARG002
        # target 视为相电流给定，feedback 视为相电流采样
        if feedback >= self.i_upper:
            self._gate_on = False
        elif feedback <= self.i_lower:
            self._gate_on = True
        return 1.0 if self._gate_on else 0.0

    def set_params(self, **kwargs) -> None:
        for k in ("current_upper", "current_lower",
                  "chopping_frequency", "hysteresis_band"):
            if k in kwargs:
                setattr(self, {"current_upper": "i_upper",
                               "current_lower": "i_lower",
                               "chopping_frequency": "f_chop",
                               "hysteresis_band": "band"}[k], float(kwargs[k]))

    def reset(self) -> None:
        self._gate_on = False
