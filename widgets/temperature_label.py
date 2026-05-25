"""带颜色提示的温度显示控件。"""
from PySide6.QtWidgets import QLabel

from config.config import TEMP_HIGH_THRESHOLD, TEMP_NORMAL_THRESHOLD


class TemperatureLabel(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__("-- °C", parent)
        self.setObjectName("TempNormal")

    def set_temperature(self, t: float) -> None:
        self.setText(f"{t:.1f} °C")
        if t >= TEMP_HIGH_THRESHOLD:
            name = "TempHigh"
        elif t >= TEMP_NORMAL_THRESHOLD:
            # 中间过渡：仍按警告处理
            name = "TempHigh"
        else:
            name = "TempNormal"
        if self.objectName() != name:
            self.setObjectName(name)
            # 触发样式重绘
            self.style().unpolish(self)
            self.style().polish(self)
