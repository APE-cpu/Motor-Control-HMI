"""诊断助手的紧凑视图，共享完整页的对话、配置与请求。"""
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QPlainTextEdit, QCheckBox,
)


class DiagnosticSidebar(QWidget):
    closeRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, ai_page, parent=None):
        super().__init__(parent)
        self.ai_page = ai_page
        self.setObjectName("DiagnosticSidebar")
        self.setMinimumWidth(300)
        self.setMaximumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("诊断助手")
        title.setObjectName("TitleLabel")
        header.addWidget(title, 1)
        close = QPushButton("收起")
        close.setToolTip("关闭右侧栏，可从顶部重新启用")
        close.clicked.connect(self.closeRequested.emit)
        header.addWidget(close)
        root.addLayout(header)

        row = QHBoxLayout()
        self.profile = QComboBox()
        self.profile.setAccessibleName("诊断助手模型")
        self.profile.setModel(ai_page._profile.model())
        self.profile.setCurrentIndex(ai_page._profile.currentIndex())
        self.profile.currentIndexChanged.connect(ai_page._profile.setCurrentIndex)
        ai_page._profile.currentIndexChanged.connect(self._sync_profile)
        row.addWidget(self.profile, 1)
        settings = QPushButton("配置")
        settings.clicked.connect(self.settingsRequested.emit)
        row.addWidget(settings)
        root.addLayout(row)
        self.model_hint = QLabel()
        self.model_hint.setWordWrap(True)
        ai_page._model.textChanged.connect(self._sync_model)
        self._sync_model()
        root.addWidget(self.model_hint)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setAccessibleName("诊断对话记录")
        self.transcript.setPlaceholderText("一边查看波形，一边提问。\n对话与完整诊断助手页面同步。")
        self.transcript.setDocument(ai_page._chat_display.document())
        self.transcript.textChanged.connect(self._scroll_to_end)
        root.addWidget(self.transcript, 1)
        self.input = QPlainTextEdit()
        self.input.setAccessibleName("诊断问题")
        self.input.setPlaceholderText("输入诊断问题，Ctrl+Enter 发送")
        self.input.setFixedHeight(88)
        root.addWidget(self.input)

        options = QHBoxLayout()
        self.rag = QCheckBox("知识库")
        self.rag.setChecked(ai_page._chk_rag.isChecked())
        self.rag.toggled.connect(ai_page._chk_rag.setChecked)
        ai_page._chk_rag.toggled.connect(self.rag.setChecked)
        options.addWidget(self.rag)
        options.addStretch()
        self.send = QPushButton("发送")
        self.send.setObjectName("PrimaryButton")
        self.send.clicked.connect(self._send)
        options.addWidget(self.send)
        root.addLayout(options)
        self.state = QLabel("可读取状态和波形，不会控制电机")
        self.state.setWordWrap(True)
        root.addWidget(self.state)
        self.shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.input)
        self.shortcut.activated.connect(self._send)
        ai_page.requestStateChanged.connect(self._sync_busy)
        self._sync_busy(ai_page.request_busy)

    def _sync_profile(self, index):
        with QSignalBlocker(self.profile):
            self.profile.setCurrentIndex(index)
        self._sync_model()

    def _sync_model(self, *_args):
        self.model_hint.setText(self.ai_page._model.text())

    def _sync_busy(self, busy):
        self.send.setEnabled(not busy)
        self.profile.setEnabled(not busy)
        self.send.setText("回复中…" if busy else "发送")
        self.state.setText("正在分析，可继续查看其他页面" if busy
                           else "可读取状态和波形，不会控制电机")

    def _send(self):
        if self.ai_page.send_question(self.input.toPlainText()):
            self.input.clear()

    def _scroll_to_end(self):
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()
