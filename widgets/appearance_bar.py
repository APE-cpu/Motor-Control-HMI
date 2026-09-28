"""Brand title and visible appearance controls."""
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QPainter, QPixmap, QColor
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QComboBox, QCheckBox

from runtime_paths import resource_path
from ui_theme import (APP_NAME, APP_SUBTITLE, THEMES,
                      appearance_manager, brand_font, current_theme)


class BrandMark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(154, 38)
        self.setAccessibleName(APP_NAME)
        manager = appearance_manager()
        manager.themeChanged.connect(self._refresh)

    def _refresh(self, _value):
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setFont(brand_font(27))
        painter.setPen(QColor(current_theme().text))
        painter.drawText(self.rect().adjusted(3, 0, -3, 0), Qt.AlignCenter, APP_NAME)


class AppearanceBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AppearanceBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 5, 15, 5)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(QPixmap(str(resource_path("assets", "app_icon_charcoal.png")))
                       .scaled(38, 38, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(icon)
        layout.addWidget(BrandMark())
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setStyleSheet("color:#8fa3b8; font-size:11px;")
        layout.addWidget(subtitle)
        layout.addStretch()
        manager = appearance_manager()
        self.diagnostic_toggle = QCheckBox("右侧诊断助手")
        self.diagnostic_toggle.setToolTip("启用或关闭右侧聊天栏")
        layout.addWidget(self.diagnostic_toggle)
        self.hints_toggle = QCheckBox("说明卡片")
        self.hints_toggle.setToolTip("显示数字孪生与通信页的说明卡片")
        self.hints_toggle.setChecked(manager.hints_visible)
        self.hints_toggle.toggled.connect(manager.set_hints_visible)
        manager.hintsChanged.connect(self.hints_toggle.setChecked)
        layout.addWidget(self.hints_toggle)
        layout.addWidget(QLabel("配色"))
        self.theme_combo = QComboBox()
        self.theme_combo.setAccessibleName("界面配色")
        for key, theme in THEMES.items():
            self.theme_combo.addItem(theme.name, key)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(manager.theme_id))
        self.theme_combo.currentIndexChanged.connect(
            lambda: manager.apply_theme(self.theme_combo.currentData()))
        layout.addWidget(self.theme_combo)
        manager.themeChanged.connect(self._sync_theme)

    def _sync_theme(self, key):
        with QSignalBlocker(self.theme_combo):
            self.theme_combo.setCurrentIndex(self.theme_combo.findData(key))
