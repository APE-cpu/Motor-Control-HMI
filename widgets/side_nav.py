"""侧边导航栏控件：基于 QListWidget 的简化封装。"""
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem


class SideNav(QListWidget):
    """点击导航项时通过 currentRowChanged 切换页面，已重映射为 currentIndexChanged。"""

    currentIndexChanged = Signal(int)

    def __init__(self, items: List[str]) -> None:
        super().__init__()
        self.setObjectName("SideNav")
        self.setFixedWidth(160)
        self.setFocusPolicy(Qt.NoFocus)
        for text in items:
            item = QListWidgetItem(text)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.addItem(item)
        self.currentRowChanged.connect(self.currentIndexChanged.emit)
