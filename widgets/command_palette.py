"""Ctrl+K 命令面板：一个输入框同时承担精确命令、模糊搜索和"问诊断助手"。

    Enter 执行选中项 · Tab 补全命令 · ↑↓ 选择 · 输入为空时 ↑ 调出历史 · Esc 关闭
耗时的诊断命令在工作线程执行，结果经信号回到界面线程显示。
"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QCursor, QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from core.commands import CommandRegistry, CommandResult, Suggestion

_TAG_COLOR = {"只读": "#66bb6a", "界面": "#90a4ae", "停机": "#ffb74d",
              "帮助": "#4fc3f7", "AI": "#ba68c8"}
_HISTORY_MAX = 30


class CommandPalette(QFrame):
    resultReady = Signal(object, str)          # (CommandResult, 输入文本)

    def __init__(self, window: QWidget, registry: CommandRegistry,
                 cards_provider: Callable[[], list],
                 open_page: Callable[[str], bool],
                 open_card: Callable[[str], None],
                 ask_ai: Callable[[str], None]) -> None:
        super().__init__(window)
        self.setObjectName("CommandPalette")
        self._window = window
        self._registry = registry
        self._cards_provider = cards_provider
        self._open_page = open_page
        self._open_card = open_card
        self._ask_ai = ask_ai
        self._suggestions: list[Suggestion] = []
        self._history: list[str] = []
        self._history_pos = -1
        self._busy = False
        # 面板固定深色，子控件颜色一并写死，浅色主题下也保持可读
        self.setStyleSheet(
            "#CommandPalette { background:#1d222c; border:1px solid #4fc3f7;"
            " border-radius:10px; }"
            "#CommandPalette QLineEdit, #CommandPalette QListWidget,"
            " #CommandPalette QPlainTextEdit { background:#14171e; color:#dfe6ee;"
            " border:1px solid #2c3442; border-radius:6px; }"
            "#CommandPalette QListWidget::item:selected { background:#232b38;"
            " color:#ffffff; }"
            "#CommandPalette QLabel { background:transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        top = QHBoxLayout()
        # 拖动柄：按住拖动面板；面板空白边框处同样可以拖
        self._grip = QLabel("⋮⋮")
        self._grip.setToolTip("按住拖动面板")
        self._grip.setCursor(Qt.SizeAllCursor)
        self._grip.setStyleSheet("color:#748291; font-size:16px; padding:0 4px;")
        self._grip.installEventFilter(self)
        self._drag_offset: QPoint | None = None
        top.addWidget(self._grip)
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "输入命令或关键词：state · fft iq 2s · goto 监控 · help 辨识 · stop")
        self._input.installEventFilter(self)
        self._input.textEdited.connect(self._refresh)
        close = QPushButton("×")
        close.setToolTip("关闭命令面板（Esc）")
        close.setFixedSize(32, 30)
        close.setStyleSheet("padding:0; font-size:16px;")   # 主题默认内边距会把 × 挤没
        close.clicked.connect(self.hide_palette)
        top.addWidget(self._input, 1)
        top.addWidget(close)
        layout.addLayout(top)

        self._list = QListWidget()
        self._list.setFixedHeight(200)
        self._list.itemActivated.connect(lambda _item: self._activate())
        layout.addWidget(self._list)

        output_header = QHBoxLayout()
        self._output_title = QLabel()
        self._output_title.setStyleSheet("color:#8fa3b8;")
        self._btn_close_output = QToolButton()
        self._btn_close_output.setText("收起结果 ×")
        self._btn_close_output.setToolTip("收起执行结果，面板恢复紧凑大小")
        self._btn_close_output.clicked.connect(self.close_output)
        output_header.addWidget(self._output_title, 1)
        output_header.addWidget(self._btn_close_output)
        layout.addLayout(output_header)
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self._output.setFont(font)
        self._output.setFixedHeight(170)
        layout.addWidget(self._output)
        self._show_output(False)

        hint = QLabel("Enter 执行 · Tab 补全 · ↑↓ 选择 · 空输入时 ↑ 调出历史 · "
                      "按住左上角把手拖动 · Esc 关闭")
        hint.setStyleSheet("color:#748291; font-size:11px;")
        layout.addWidget(hint)
        self.resultReady.connect(self._on_result)
        self.hide()

    # ------------------------------------------------------------ 显示与位置
    def show_palette(self, text: str = "", at: QPoint | None = None) -> None:
        """在鼠标位置打开面板；at 为主窗口坐标，默认取当前鼠标位置。

        鼠标不在主窗口内时回到顶部居中；面板超出窗口边缘时自动收回窗口内。
        """
        width = min(680, max(420, self._window.width() - 120))
        self.setFixedWidth(width)
        if not self._busy:
            self.close_output()           # 每次打开都从紧凑状态开始
        self._input.setText(text)
        self._history_pos = -1
        self._refresh(text)
        self.adjustSize()
        if at is None:
            cursor = self._window.mapFromGlobal(QCursor.pos())
            at = cursor if self._window.rect().contains(cursor) else None
        if at is None:
            self.move((self._window.width() - width) // 2, 56)
        else:
            # 让输入框落在鼠标附近：面板左上角放在鼠标左上方一点
            self.move(self._clamped(QPoint(at.x() - 40, at.y() - 20)))
        self.show()
        self.raise_()
        self._input.setFocus()
        self._input.selectAll()

    def hide_palette(self) -> None:
        self._drag_offset = None
        self.hide()

    def _clamped(self, top_left: QPoint) -> QPoint:
        margin = 8
        max_x = max(margin, self._window.width() - self.width() - margin)
        max_y = max(margin, self._window.height() - self.height() - margin)
        return QPoint(min(max(top_left.x(), margin), max_x),
                      min(max(top_left.y(), margin), max_y))

    def _show_output(self, visible: bool) -> None:
        self._output_title.setVisible(visible)
        self._btn_close_output.setVisible(visible)
        self._output.setVisible(visible)
        self.layout().activate()          # 先按新的可见性重算布局，否则尺寸偏大
        self.adjustSize()
        if self.isVisible():
            self.move(self._clamped(self.pos()))    # 变高后仍留在窗口内

    def close_output(self) -> None:
        """收起结果区；正在执行的命令仍会完成，完成后结果重新显示。"""
        self._output.clear()
        self._show_output(False)

    @property
    def output_visible(self) -> bool:
        return self._output.isVisibleTo(self)

    # ------------------------------------------------------------ 拖动
    def _drag(self, event) -> bool:
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            return True
        if kind == QEvent.MouseMove and self._drag_offset is not None:
            self.move(self._clamped(event.globalPosition().toPoint() - self._drag_offset))
            return True
        if kind == QEvent.MouseButtonRelease and self._drag_offset is not None:
            self._drag_offset = None
            return True
        return False

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._drag(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._drag(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._drag(event):
            super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ 候选
    def _refresh(self, text: str | None = None) -> None:
        text = self._input.text() if text is None else text
        self._suggestions = self._registry.suggest(text, self._cards_provider())
        self._list.clear()
        for suggestion in self._suggestions:
            color = _TAG_COLOR.get(suggestion.tag, "#90a4ae")
            label = f"［{suggestion.tag}］ {suggestion.title}"
            if suggestion.detail:
                label += f"\n        {suggestion.detail}"
            item = QListWidgetItem(label)
            item.setForeground(Qt.white if suggestion.runnable else Qt.lightGray)
            item.setToolTip(suggestion.detail)
            item.setData(Qt.UserRole, color)
            self._list.addItem(item)
        if self._suggestions:
            self._list.setCurrentRow(0)

    def _selected(self) -> Suggestion | None:
        row = self._list.currentRow()
        return self._suggestions[row] if 0 <= row < len(self._suggestions) else None

    def _complete(self) -> None:
        suggestion = self._selected()
        if suggestion is None or suggestion.kind != "command":
            return
        payload = suggestion.payload
        if isinstance(payload, tuple) and payload and payload[0] == "complete":
            text = payload[1] + " "
        else:
            command, _args = self._registry.parse(self._input.text())
            if command is None:
                return
            text = self._input.text() if self._input.text().endswith(" ") \
                else self._input.text() + " "
        self._input.setText(text)
        self._refresh(text)

    # ------------------------------------------------------------ 执行
    def _activate(self) -> None:
        if self._busy:
            return
        suggestion = self._selected()
        if suggestion is None:
            return
        text = self._input.text().strip()
        if suggestion.kind == "command":
            payload = suggestion.payload
            if isinstance(payload, tuple) and payload and payload[0] == "complete":
                self._complete()
                return
            if not suggestion.runnable or payload is None:
                return
            command, parsed = payload
            self._remember(text)
            if command.background:
                self._busy = True
                self._output_title.setText(f"{command.title}：执行中…")
                self._output.setPlainText("")
                self._show_output(True)
                threading.Thread(target=self._run_background,
                                 args=(command, parsed, text),
                                 name="palette-command", daemon=True).start()
            else:
                self._on_result(self._safe_run(command, parsed), text)
        elif suggestion.kind == "page":
            self._open_page(suggestion.payload)
            self.hide_palette()
        elif suggestion.kind == "card":
            self._open_card(suggestion.payload)
            self.hide_palette()
        elif suggestion.kind == "ai":
            self._ask_ai(suggestion.payload)
            self.hide_palette()

    @staticmethod
    def _safe_run(command, parsed) -> CommandResult:
        try:
            return command.run(parsed)
        except Exception as exc:  # 命令异常只显示，不允许冲出界面事件循环
            return CommandResult(False, command.title,
                                 error=f"{type(exc).__name__}: {exc}")

    def _run_background(self, command, parsed, text: str) -> None:
        self.resultReady.emit(self._safe_run(command, parsed), text)

    def _on_result(self, result: CommandResult, text: str) -> None:
        self._busy = False
        state = "完成" if result.ok else "失败"
        self._output_title.setText(f"{result.title}：{state}　（{text}）")
        lines = list(result.lines)
        if result.error:
            lines.append(f"错误：{result.error}")
        lines += [f"提示：{warning}" for warning in result.warnings]
        self._output.setPlainText("\n".join(lines))
        self._show_output(True)

    @property
    def last_output(self) -> str:
        return self._output.toPlainText()

    def run_text(self, text: str) -> None:
        """按输入文本执行第一个候选（测试与脚本用）。"""
        self._input.setText(text)
        self._refresh(text)
        self._list.setCurrentRow(0)
        self._activate()

    # ------------------------------------------------------------ 历史与按键
    def _remember(self, text: str) -> None:
        if text in self._history:
            self._history.remove(text)
        self._history.insert(0, text)
        del self._history[_HISTORY_MAX:]

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if watched is self._grip and event.type() in (
                QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
            if self._drag(event):
                return True
        if watched is self._input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape:
                self.hide_palette()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._activate()
                return True
            if key == Qt.Key_Tab:
                self._complete()
                return True
            if key == Qt.Key_Up and (not self._input.text() or self._history_pos >= 0):
                if self._history and self._history_pos < len(self._history) - 1:
                    self._history_pos += 1
                    self._input.setText(self._history[self._history_pos])
                    self._refresh()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down):
                row = self._list.currentRow() + (1 if key == Qt.Key_Down else -1)
                if 0 <= row < self._list.count():
                    self._list.setCurrentRow(row)
                return True
            self._history_pos = -1
        return super().eventFilter(watched, event)
