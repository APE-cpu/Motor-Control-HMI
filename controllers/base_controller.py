"""所有控制器的基类与统一接口。"""
from abc import ABC, abstractmethod


class BaseController(ABC):
    name: str = "BaseController"

    @abstractmethod
    def update(self, target: float, feedback: float) -> float:
        """根据给定值与反馈值计算输出量。"""

    @abstractmethod
    def set_params(self, **kwargs) -> None:
        """运行时动态修改参数。"""

    @abstractmethod
    def reset(self) -> None:
        """复位内部状态（积分、历史值等）。"""
