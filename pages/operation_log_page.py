"""操作记录页面：显示所有按钮操作历史，支持搜索、级别筛选、着色、导出。"""
import os

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from logs.operation_logger import classify_level, logger

# 级别 → 显示色（与边缘AI页配色一致：正常绿/警告橙/错误红）
_LEVEL_COLOR = {"info": "#b8c6d8", "warn": "#ffa726", "error": "#ef5350"}
_LEVEL_NAME = {"info": "信息", "warn": "警告", "error": "错误"}


class OperationLogPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._all: list[str] = []          # 全部日志行（未筛选）
        self._auto_scroll = True

        root = QVBoxLayout(self)
        title = QLabel("操作记录")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        box = QGroupBox("操作历史")
        v = QVBoxLayout(box)

        # ---- 工具栏：搜索 + 级别筛选 + 自动滚动 ----
        bar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索关键词（操作名 / 参数 / 时间）…")
        self._search.textChanged.connect(self._refresh)
        self._level = QComboBox()
        self._level.addItems(["全部级别", "信息", "警告", "错误"])
        self._level.currentIndexChanged.connect(self._refresh)
        self._auto_cb = QCheckBox("自动滚动")
        self._auto_cb.setChecked(True)
        self._auto_cb.toggled.connect(self._on_auto_toggled)
        bar.addWidget(QLabel("筛选"))
        bar.addWidget(self._search, 1)
        bar.addWidget(self._level)
        bar.addWidget(self._auto_cb)
        v.addLayout(bar)

        # ---- 日志列表（逐行着色）----
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { font-family: Consolas, 'Courier New', monospace; }")
        v.addWidget(self._list, 1)

        # ---- 底部：统计 + 操作按钮 ----
        h = QHBoxLayout()
        self._stats = QLabel("共 0 条")
        self._stats.setStyleSheet("color: #8fa3b8;")
        btn_export = QPushButton("导出日志")
        btn_export.setToolTip("导出当前筛选后可见的日志行")
        btn_export.clicked.connect(self._on_export)
        btn_clear = QPushButton("清空显示")
        btn_clear.setToolTip("仅清空当前视图，不删除日志文件")
        btn_clear.clicked.connect(self._on_clear)
        h.addWidget(self._stats, 1)
        h.addWidget(btn_export)
        h.addWidget(btn_clear)
        v.addLayout(h)
        root.addWidget(box, 1)

        # 加载历史 + 订阅新条目
        self._all = logger.load_recent()
        self._refresh()
        logger.newEntry.connect(self._on_new_entry)

    # ---- 筛选与渲染 ----
    def _passes(self, line: str) -> bool:
        kw = self._search.text().strip()
        if kw and kw.lower() not in line.lower():
            return False
        lvl_idx = self._level.currentIndex()
        if lvl_idx != 0:
            want = {1: "info", 2: "warn", 3: "error"}[lvl_idx]
            if classify_level(line) != want:
                return False
        return True

    def _make_item(self, line: str) -> QListWidgetItem:
        item = QListWidgetItem(line)
        item.setForeground(QColor(_LEVEL_COLOR[classify_level(line)]))
        return item

    def _refresh(self) -> None:
        self._list.clear()
        shown = 0
        for line in self._all:
            if self._passes(line):
                self._list.addItem(self._make_item(line))
                shown += 1
        self._update_stats(shown)
        if self._auto_scroll:
            self._list.scrollToBottom()

    def _update_stats(self, shown: int) -> None:
        n_err = sum(1 for l in self._all if classify_level(l) == "error")
        n_warn = sum(1 for l in self._all if classify_level(l) == "warn")
        self._stats.setText(
            f"共 {len(self._all)} 条（显示 {shown}）　｜　"
            f"警告 {n_warn}　错误 {n_err}")

    def _on_new_entry(self, line: str) -> None:
        self._all.append(line)
        if self._passes(line):
            self._list.addItem(self._make_item(line))
            if self._auto_scroll:
                self._list.scrollToBottom()
        self._update_stats(self._list.count())

    def _on_auto_toggled(self, on: bool) -> None:
        self._auto_scroll = on
        if on:
            self._list.scrollToBottom()

    def _on_clear(self) -> None:
        """仅清空视图与内存缓冲，不动磁盘日志文件。"""
        self._all.clear()
        self._list.clear()
        self._update_stats(0)

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "operation_log.txt", "文本文件 (*.txt)"
        )
        if not path:
            return
        # 导出当前筛选后可见的行
        lines = [self._list.item(i).text() for i in range(self._list.count())]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.log("导出操作日志", f"{os.path.basename(path)}（{len(lines)}条）")
