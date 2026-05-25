"""模型预测控制器：一阶系统 + 二次代价的简化 MPC 演示实现。

这里给出一个轻量级的占位实现：在每个控制周期内基于一阶离散模型
    y[k+1] = a*y[k] + b*u[k]
求解 N 步窗口的最优控制序列（解析最小化），输出窗口内第一个控制量。
真实工程中可替换为 cvxpy / qpsolvers 等数值求解器。
"""
from typing import List

from .base_controller import BaseController


class MPCController(BaseController):
    name = "MPC"

    def __init__(self, prediction_horizon: int = 10, control_horizon: int = 3,
                 weight_q: float = 1.0, weight_r: float = 0.1,
                 u_max: float = 24.0, u_min: float = -24.0,
                 a: float = 0.9, b: float = 0.1) -> None:
        self.N = prediction_horizon
        self.M = control_horizon
        self.Q = weight_q
        self.R = weight_r
        self.u_max = u_max
        self.u_min = u_min
        self.a = a
        self.b = b
        self._u_prev = 0.0

    def _solve(self, y0: float, ref: float) -> float:
        """简化求解：在 [u_min, u_max] 网格上搜索使代价最小的常数控制 u。"""
        best_u = 0.0
        best_cost = float("inf")
        steps = 41  # 网格点数
        for i in range(steps):
            u = self.u_min + (self.u_max - self.u_min) * i / (steps - 1)
            y = y0
            cost = 0.0
            for _ in range(self.N):
                y = self.a * y + self.b * u
                cost += self.Q * (ref - y) ** 2 + self.R * u * u
            if cost < best_cost:
                best_cost = cost
                best_u = u
        return best_u

    def update(self, target: float, feedback: float) -> float:
        u = self._solve(feedback, target)
        self._u_prev = u
        return u

    def set_params(self, **kwargs) -> None:
        mapping = {
            "prediction_horizon": "N",
            "control_horizon": "M",
            "weight_q": "Q",
            "weight_r": "R",
            "u_max": "u_max",
            "u_min": "u_min",
        }
        for k, attr in mapping.items():
            if k in kwargs:
                val = kwargs[k]
                setattr(self, attr,
                        int(val) if attr in ("N", "M") else float(val))

    def reset(self) -> None:
        self._u_prev = 0.0
