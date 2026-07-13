"""侧边导航栏控件：基于 QListWidget，支持分组标题 + 图标项。"""
from typing import List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem


class SideNav(QListWidget):
    """分组导航。sections 结构：[(组标题, [(条目文本, 页面索引), ...]), ...]

    组标题不可选中；条目通过 UserRole 携带页面索引，
    因此导航顺序可以与 QStackedWidget 的页面顺序解耦。
    """

    currentIndexChanged = Signal(int)

    def __init__(self, sections: List[Tuple[str, List[Tuple[str, int]]]]) -> None:
        super().__init__()
        self.setObjectName("SideNav")
        self.setMinimumWidth(130)
        self.setMaximumWidth(170)
        self.setFocusPolicy(Qt.NoFocus)
        for title, entries in sections:
            header = QListWidgetItem(title)
            header.setFlags(Qt.NoItemFlags)          # 不可选中/悬停
            header.setData(Qt.UserRole, -1)
            self.addItem(header)
            for text, page_idx in entries:
                item = QListWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                item.setData(Qt.UserRole, page_idx)
                self.addItem(item)
        self.currentRowChanged.connect(self._on_row_changed)

    def _on_row_changed(self, row: int) -> None:
        item = self.item(row)
        if item is None:
            return
        page_idx = item.data(Qt.UserRole)
        if page_idx is not None and page_idx >= 0:
            self.currentIndexChanged.emit(page_idx)

    def select_page(self, page_idx: int) -> None:
        """按页面索引选中对应条目（跳过组标题）。"""
        for row in range(self.count()):
            if self.item(row).data(Qt.UserRole) == page_idx:
                self.setCurrentRow(row)
                return
