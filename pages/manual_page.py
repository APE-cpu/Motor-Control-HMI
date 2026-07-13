"""系统内置使用说明书页面。"""
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)


class ManualPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._path = Path(__file__).resolve().parent.parent / "使用说明书.md"
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("使用说明书")
        title.setObjectName("TitleLabel")
        self._source = QLabel(str(self._path))
        self._source.setStyleSheet("color: #8fa3b8;")
        refresh = QPushButton("重新载入")
        refresh.clicked.connect(self.reload)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(refresh)
        root.addLayout(header)
        root.addWidget(self._source)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)
        root.addWidget(self._browser, 1)
        self.reload()

    def reload(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._browser.setPlainText(f"使用说明书读取失败：{exc}")
            return
        self._browser.setMarkdown(text)
