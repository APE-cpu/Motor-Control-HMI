"""新手引导遮罩：半透明遮住主窗口，把目标控件"挖空"露出并配说明气泡。

安全原则：引导只高亮、只说明，从不替用户点击或发送任何命令。
挖空区域通过 setMask 排除在遮罩之外，用户可以直接操作露出的真实控件。
会让电机通电或动作的步骤以警告样式显示，并由用户确认"已亲手完成"后才继续。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import (
    QAbstractScrollArea, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

_ACCENT = QColor("#4fc3f7")
_WARN = QColor("#ffb74d")
_PAD = 6


@dataclass(frozen=True)
class GuideStep:
    title: str
    text: str
    control_id: str | None = None
    dangerous: bool = False


class _Bubble(QFrame):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("GuideBubble")
        self.setFixedWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.counter = QLabel()
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.warning = QLabel(
            "此步骤会让电机通电或改变运行状态。请确认现场安全后由你亲手操作；"
            "引导不会替你点击。")
        self.warning.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_skip = QPushButton("退出引导")
        self.btn_prev = QPushButton("上一步")
        self.btn_next = QPushButton("下一步")
        row.addWidget(self.btn_skip)
        row.addStretch(1)
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        for widget in (self.counter, self.title, self.text, self.warning):
            layout.addWidget(widget)
        layout.addLayout(row)

    def apply(self, step: GuideStep, index: int, total: int) -> None:
        color = _WARN if step.dangerous else _ACCENT
        self.setStyleSheet(
            "#GuideBubble { background:#1d222c; border:1px solid %s;"
            " border-radius:10px; } QLabel { color:#dfe6ee; background:transparent; }"
            % color.name())
        self.counter.setText(f"第 {index + 1} / {total} 步")
        self.counter.setStyleSheet("color:#8fa3b8; font-size:12px;")
        self.title.setText(step.title)
        self.title.setStyleSheet(f"color:{color.name()}; font-size:15px; font-weight:bold;")
        self.text.setText(step.text)
        self.warning.setVisible(step.dangerous)
        self.warning.setStyleSheet(f"color:{_WARN.name()};")
        self.btn_prev.setEnabled(index > 0)
        last = index == total - 1
        self.btn_next.setText(
            "我已完成，" + ("结束" if last else "下一步") if step.dangerous
            else ("完成" if last else "下一步"))
        self.adjustSize()


class GuideOverlay(QWidget):
    finished = Signal()

    def __init__(self, window: QWidget,
                 resolve: Callable[[str], tuple[QWidget | None, str | None]],
                 show_page: Callable[[str], bool]) -> None:
        super().__init__(window)
        self._window = window
        self._resolve = resolve
        self._show_page = show_page
        self._steps: list[GuideStep] = []
        self._index = 0
        self._hole = QRect()
        self._bubble = _Bubble(self)
        self._bubble.btn_next.clicked.connect(self.next_step)
        self._bubble.btn_prev.clicked.connect(self.prev_step)
        self._bubble.btn_skip.clicked.connect(self.stop)
        self._tracker = QTimer(self)
        self._tracker.setInterval(250)
        self._tracker.timeout.connect(self._update_geometry)
        self.setFocusPolicy(Qt.StrongFocus)
        window.installEventFilter(self)
        self.hide()

    # ------------------------------------------------------------ 控制
    @property
    def active(self) -> bool:
        return self.isVisible()

    @property
    def current_step(self) -> GuideStep | None:
        return self._steps[self._index] if self._steps else None

    def start(self, steps: list[GuideStep]) -> None:
        if not steps:
            return
        self._steps = list(steps)
        self._index = 0
        self.setGeometry(self._window.rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._tracker.start()
        self._enter_step()

    def stop(self) -> None:
        self._tracker.stop()
        self.hide()
        self._steps = []
        self.finished.emit()

    def next_step(self) -> None:
        if self._index >= len(self._steps) - 1:
            self.stop()
            return
        self._index += 1
        self._enter_step()

    def prev_step(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._enter_step()

    def _enter_step(self) -> None:
        step = self._steps[self._index]
        if step.control_id:
            _widget, page_key = self._resolve(step.control_id)
            if page_key:
                self._show_page(page_key)
        self._bubble.apply(step, self._index, len(self._steps))
        self._update_geometry()
        # 页面切换有约 190 ms 过渡动画：结束后把控件滚入视野，再对齐一次
        QTimer.singleShot(220, self._ensure_target_visible)
        QTimer.singleShot(280, self._update_geometry)

    def _ensure_target_visible(self) -> None:
        """只在进入步骤时滚动一次，之后不和用户的手动滚动抢。"""
        step = self.current_step
        if step is None or not step.control_id or not self.isVisible():
            return
        widget, _page = self._resolve(step.control_id)
        parent = widget.parentWidget() if widget is not None else None
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(widget, 40, 40)
                return
            if isinstance(parent, QAbstractScrollArea):
                return
            parent = parent.parentWidget()

    # ------------------------------------------------------------ 几何
    def _target_rect(self) -> QRect:
        step = self.current_step
        if step is None or not step.control_id:
            return QRect()
        widget, _page = self._resolve(step.control_id)
        if widget is None or not widget.isVisible():
            return QRect()
        top_left = widget.mapTo(self._window, QPoint(0, 0))
        rect = QRect(top_left, widget.size()).adjusted(-_PAD, -_PAD, _PAD, _PAD)
        return rect.intersected(self.rect())

    def _update_geometry(self) -> None:
        if not self.isVisible():
            return
        if self.geometry() != self._window.rect():
            self.setGeometry(self._window.rect())
        self._hole = self._target_rect()
        region = QRegion(self.rect())
        if not self._hole.isNull():
            region = region.subtracted(QRegion(self._hole, QRegion.Rectangle))
        self.setMask(region)
        self._place_bubble()
        self.update()

    def _place_bubble(self) -> None:
        bubble = self._bubble
        size = bubble.sizeHint()
        bubble.resize(size)
        area = self.rect().adjusted(12, 12, -12, -12)
        if self._hole.isNull():
            bubble.move(area.center().x() - size.width() // 2,
                        area.center().y() - size.height() // 2)
            return
        hole = self._hole
        candidates = [
            QPoint(hole.left(), hole.bottom() + 12),                    # 下方
            QPoint(hole.left(), hole.top() - size.height() - 12),       # 上方
            QPoint(hole.right() + 12, hole.top()),                      # 右侧
            QPoint(hole.left() - size.width() - 12, hole.top()),        # 左侧
        ]
        for point in candidates:
            rect = QRect(point, size)
            if area.contains(rect):
                bubble.move(point)
                return
        point = candidates[0]
        x = min(max(point.x(), area.left()), area.right() - size.width())
        y = min(max(point.y(), area.top()), area.bottom() - size.height())
        bubble.move(x, y)

    # ------------------------------------------------------------ 绘制与事件
    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        painter.fillPath(path, QColor(8, 10, 14, 165))
        if not self._hole.isNull():
            step = self.current_step
            color = _WARN if step is not None and step.dangerous else _ACCENT
            # 挖空区不属于遮罩、画不上，边框画在挖空区外侧一圈
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(QRectF(self._hole).adjusted(-2, -2, 2, 2), 7, 7)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            self.stop()
        elif event.key() in (Qt.Key_Right, Qt.Key_Return, Qt.Key_Enter):
            self.next_step()
        elif event.key() == Qt.Key_Left:
            self.prev_step()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if watched is self._window and event.type() == QEvent.Resize and self.isVisible():
            QTimer.singleShot(0, self._update_geometry)
        return super().eventFilter(watched, event)
