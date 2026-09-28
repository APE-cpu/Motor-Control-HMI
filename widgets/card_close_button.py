"""A small, independently persisted close button inside an existing hint card."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QToolButton

from ui_theme import appearance_manager


class CardCloseButton(QToolButton):
    def __init__(self, card, card_id, settings=None):
        super().__init__(card)
        self._card = card
        self._settings = settings if settings is not None else appearance_manager().settings
        self._key = f"appearance/cards/{card_id}/dismissed"
        self.setObjectName("CardCloseButton")
        self.setText("×")
        self.setAccessibleName("关闭此说明")
        self.setToolTip("关闭此说明，重启后仍保持关闭")
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFixedSize(24, 24)
        self.setStyleSheet(
            "QToolButton { background: transparent; border: none; padding: 0; "
            "color: #8fa3b8; font-size: 18px; }"
            "QToolButton:hover { background: rgba(128,128,128,0.2); color: #dfe6ee; }")
        self.clicked.connect(self._dismiss)
        card.installEventFilter(self)
        if self._settings.value(self._key, False, type=bool):
            card.hide()
        self.show()
        self._place()

    def _place(self):
        self.move(max(0, self._card.width() - self.width() - 6), 6)
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Show):
            self._place()
        return False

    def _dismiss(self):
        self._settings.setValue(self._key, True)
        self._settings.sync()
        self._card.hide()
