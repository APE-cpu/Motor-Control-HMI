"""操作记录页面：显示所有按钮操作历史，支持清空和导出。"""
import os

from PySide6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from logs.operation_logger import logger


class OperationLogPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        title = QLabel("操作记录")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        box = QGroupBox("操作历史")
        v = QVBoxLayout(box)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        v.addWidget(self._log_view, 1)

        h = QHBoxLayout()
        btn_export = QPushButton("导出日志")
        btn_export.clicked.connect(self._on_export)
        btn_clear = QPushButton("清空显示")
        btn_clear.clicked.connect(self._log_view.clear)
        h.addStretch(1)
        h.addWidget(btn_export)
        h.addWidget(btn_clear)
        v.addLayout(h)
        root.addWidget(box, 1)

        # 加载历史 + 订阅新条目
        for line in logger.load_recent():
            self._log_view.appendPlainText(line)
        logger.newEntry.connect(self._log_view.appendPlainText)

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "operation_log.txt", "文本文件 (*.txt)"
        )
        if not path:
            return
        content = self._log_view.toPlainText()
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.log("导出操作日志", os.path.basename(path))
