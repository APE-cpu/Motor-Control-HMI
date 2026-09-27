"""Persistent UI appearance; semantic alarm and trace colors stay independent."""
from __future__ import annotations

from dataclasses import dataclass
import re
import sys
import weakref

from PySide6.QtCore import QObject, QEvent, QSettings, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from shiboken6 import isValid

from runtime_paths import resource_path

APP_NAME = "驭衡智控"
APP_SUBTITLE = "电机控制与分析平台"
@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    panel: str
    field: str
    hover: str
    border: str
    text: str
    muted: str
    accent: str
    primary: str


THEMES = {
    "graphite": Theme("石墨橙", "#222323", "#2b2c2c", "#1b1c1c", "#363534", "#4a4744", "#f1efe9", "#b7b3ad", "#ff865f", "#a83c20"),
    "blue": Theme("经典蓝", "#14171e", "#1d222c", "#10131a", "#232b38", "#2c3442", "#dfe6ee", "#8fa3b8", "#4fc3f7", "#1976d2"),
    "ocean": Theme("深海青", "#10232b", "#19313a", "#0d1c22", "#25414b", "#3e626c", "#edf5f4", "#a8c4ca", "#5ed0d4", "#187e89"),
    "titanium": Theme("钛钢灰", "#24282c", "#30363a", "#1b2024", "#3b4449", "#59666c", "#f0f3f2", "#b7c2c5", "#cad9df", "#526f7a"),
}
DEFAULT_THEME = "graphite"
_LEGACY_THEMES = {"jade": "ocean", "violet": "titanium"}


def color_map(theme: Theme) -> dict[str, str]:
    if theme == THEMES["blue"]:
        return {}
    mapping = {}
    for colors, replacement in (
        (("#14171e",), theme.background),
        (("#1d222c", "#171c26", "#151b27", "#171d27", "#1b2332"), theme.panel),
        (("#10131a",), theme.field),
        (("#232b38", "#1c2230", "#1d2430", "#19212e", "#333c4d", "#262d3a"), theme.hover),
        (("#2c3442", "#3b4557", "#2f3b4d", "#4d5a6b"), theme.border),
        (("#dfe6ee", "#e8eef5", "#cfd8dc", "#c7d3e0", "#e8f1ff"), theme.text),
        (("#8fa3b8", "#90a4ae", "#b8c6d8", "#aebccb", "#748291"), theme.muted),
        (("#4fc3f7", "#42a5f5", "#26c6da"), theme.accent),
        (("#1976d2", "#2196f3", "#1565c0"), theme.primary),
    ):
        mapping.update(dict.fromkeys(colors, replacement))
    mapping["#1c2f4a"] = QColor(theme.primary).darker(180).name()
    return mapping


def themed_stylesheet(source: str, theme: Theme) -> str:
    mapping = color_map(theme)
    text = re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: mapping.get(m[0].lower(), m[0]), source)
    def rgba(match):
        red, green, blue = (int(match[i]) for i in (1, 2, 3))
        old = f"#{red:02x}{green:02x}{blue:02x}"
        if old not in mapping:
            return match[0]
        color = QColor(mapping[old])
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {match[4]})"
    return re.sub(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", rgba, text)


def current_theme() -> Theme:
    app = QApplication.instance()
    manager = getattr(app, "_yuheng_appearance", None)
    return THEMES[manager.theme_id if manager else DEFAULT_THEME]


def theme_color(source: str) -> QColor:
    return QColor(color_map(current_theme()).get(source.lower(), source))


def brand_font(pixels: int) -> QFont:
    font = QFont("Microsoft YaHei")
    font.setPixelSize(pixels)
    font.setBold(True)
    font.setItalic(True)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 3 if pixels > 35 else 1)
    return font


class ThemeManager(QObject):
    themeChanged = Signal(str)

    def __init__(self, app: QApplication, settings: QSettings | None = None):
        super().__init__(app)
        self.app = app
        self.settings = settings if settings is not None else QSettings("Yuheng", "MotorControlHMI")
        chosen = str(self.settings.value("appearance/theme", DEFAULT_THEME))
        chosen = _LEGACY_THEMES.get(chosen, chosen)
        self.theme_id = chosen if chosen in THEMES else DEFAULT_THEME
        self._applying = False
        self._styled_widgets = weakref.WeakSet()
        self._theme_windows = weakref.WeakSet()
        self._themed_qss = {}
        self._source_qss = resource_path("config", "style.qss").read_text(encoding="utf-8")
        app.installEventFilter(self)

    def register_window(self, window: QWidget) -> None:
        self._theme_windows.add(window)

    def unregister_window(self, window: QWidget) -> None:
        self._theme_windows.discard(window)

    def _stylesheet_for(self, theme_id: str) -> str:
        if theme_id not in self._themed_qss:
            self._themed_qss[theme_id] = themed_stylesheet(
                self._source_qss, THEMES[theme_id])
        return self._themed_qss[theme_id]

    def style_root(self, widget: QWidget) -> None:
        """只更新当前可见的界面区域，隐藏页面在打开时再更新。"""
        stylesheet = self._stylesheet_for(self.theme_id)
        if widget.property("artPage"):
            from widgets.page_artwork import artwork_stylesheet
            stylesheet += artwork_stylesheet(THEMES[self.theme_id])
        if widget.styleSheet() == stylesheet:
            return
        applying = self._applying
        self._applying = True
        try:
            widget.setStyleSheet(stylesheet)
        finally:
            self._applying = applying

    def apply_theme(self, theme_id: str, persist: bool = True) -> None:
        if theme_id not in THEMES:
            raise ValueError(f"Unknown theme: {theme_id}")
        self.theme_id = theme_id
        theme = THEMES[theme_id]
        self.app.setOverrideCursor(Qt.WaitCursor)
        self._applying = True
        try:
            stylesheet = self._stylesheet_for(theme_id)
            # 重设 QApplication 样式会同步重排所有页面，即使它们隐藏着。
            # 主窗口存在时仅由它刷新可见区域；独立窗口保留原有全局行为。
            has_window = any(isValid(window) for window in self._theme_windows)
            if (not has_window or not self.app.styleSheet()) and self.app.styleSheet() != stylesheet:
                self.app.setStyleSheet(stylesheet)
            palette = QPalette(self.app.palette())
            for role, color in ((QPalette.Window, theme.background), (QPalette.Base, theme.field),
                                (QPalette.WindowText, theme.text), (QPalette.Text, theme.text),
                                (QPalette.Button, theme.panel), (QPalette.ButtonText, theme.text),
                                (QPalette.Highlight, theme.primary), (QPalette.HighlightedText, "#ffffff")):
                palette.setColor(role, QColor(color))
            self.app.setPalette(palette)
            self.refresh_widgets()
        finally:
            self._applying = False
            self.app.restoreOverrideCursor()
        if persist:
            self.settings.setValue("appearance/theme", theme_id)
            self.settings.sync()
        self.themeChanged.emit(theme_id)

    def refresh_widgets(self) -> None:
        # Keep only wrappers we have seen alive. Enumerating allWidgets() also
        # wraps internal QtCharts objects owned by already-closing windows.
        for widget in tuple(self._styled_widgets):
            if isValid(widget):
                self._restyle_widget(widget)
                widget.update()

    def _restyle_widget(self, widget: QWidget) -> None:
        current = widget.styleSheet()
        pg = sys.modules.get("pyqtgraph")
        is_plot = pg and isinstance(widget, (pg.PlotWidget, pg.GraphicsLayoutWidget))
        if current or is_plot:
            self._styled_widgets.add(widget)
        previous = getattr(widget, "_yuheng_rendered_style", None)
        if current != previous:
            widget._yuheng_base_style = current
        original = getattr(widget, "_yuheng_base_style", current)
        styled = themed_stylesheet(original, THEMES[self.theme_id])
        widget._yuheng_rendered_style = styled
        if current != styled:
            widget.setStyleSheet(styled)

        if is_plot:
            theme = THEMES[self.theme_id]
            ancestor = widget
            while ancestor is not None and not ancestor.property("artPage"):
                ancestor = ancestor.parentWidget()
            background = QColor(theme.field)
            if ancestor is not None:
                background.setAlpha(70)
                widget.setAutoFillBackground(False)
                widget.viewport().setAutoFillBackground(False)
            widget.setBackground(background)
            # Axis chrome follows the theme; measured curve pens are untouched.
            for item in widget.scene().items():
                if isinstance(item, pg.AxisItem):
                    item.setPen(theme.border)
                    item.setTextPen(theme.muted)

    def eventFilter(self, watched, event):
        if self._applying:
            return False
        if event.type() == QEvent.Show and isinstance(watched, QWidget):
            if (isinstance(watched, QDialog) and watched.isWindow()
                    and self._theme_windows):
                self.style_root(watched)
            else:
                self._restyle_widget(watched)
        elif (event.type() == QEvent.StyleChange and not self._applying
              and isinstance(watched, QWidget)):
            style = watched.styleSheet()
            if style and style != getattr(watched, "_yuheng_rendered_style", None):
                self._restyle_widget(watched)
        return False


def appearance_manager() -> ThemeManager:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Create QApplication before the appearance manager")
    if not hasattr(app, "_yuheng_appearance"):
        app._yuheng_appearance = ThemeManager(app)
    return app._yuheng_appearance
