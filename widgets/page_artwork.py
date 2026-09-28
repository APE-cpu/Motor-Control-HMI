"""整页单色机械结构背景；静态缓存，不占布局空间。"""
from PySide6.QtCore import QRectF, Qt, QSize
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QGridLayout, QWidget
from runtime_paths import resource_path
from ui_theme import appearance_manager, current_theme


def artwork_stylesheet(theme):
    """只让结构面板透出背景；输入框和按钮保持不透明。"""
    panel, field = QColor(theme.panel), QColor(theme.field)
    return f'''
    QWidget[artSurface="true"] {{ background: transparent; }}
    QWidget[artPage="true"] QGroupBox {{
        background-color: rgba({panel.red()}, {panel.green()}, {panel.blue()}, 175);
    }}
    QWidget[artPage="true"] QTabWidget::pane {{ background: transparent; }}
    QWidget[artPage="true"] QTabBar {{ background: transparent; }}
    QWidget[artPage="true"] QGraphicsView {{ background-color: {theme.field}; }}
    QWidget[artPage="true"] QTextEdit,
    QWidget[artPage="true"] QPlainTextEdit {{
        background-color: rgba({field.red()}, {field.green()}, {field.blue()}, 230);
    }}
    QWidget[artPage="true"] QCheckBox {{ background: transparent; }}
    '''


class PageArtwork(QWidget):
    """只在尺寸或主题变化时重建缓存，没有动画或刷新定时器。"""

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self._image = QPixmap()
        self._cache = QPixmap()
        self._cache_key = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        appearance_manager().themeChanged.connect(self._invalidate)

    def sizeHint(self):
        return QSize(0, 0)

    def _invalidate(self, _theme=None):
        self._cache_key = None
        self.update()

    def showEvent(self, event):
        if self._image.isNull():
            self._image.load(str(resource_path("assets", "page_art", f"{self.key}_structure.png")))
            if not self._image.isNull():
                # 运行时统一线稿笔色，保留素材透明度；不随强调色染成彩色。
                tint = QPainter(self._image)
                tint.setCompositionMode(QPainter.CompositionMode_SourceIn)
                tint.fillRect(self._image.rect(), QColor("#c0c3c7"))
                tint.end()
            self._cache_key = None
        super().showEvent(event)

    def _render(self):
        theme = current_theme()
        dpr = self.devicePixelRatioF()
        self._cache = QPixmap(max(1, round(self.width() * dpr)),
                              max(1, round(self.height() * dpr)))
        self._cache.setDevicePixelRatio(dpr)
        self._cache.fill(QColor(theme.background))
        painter = QPainter(self._cache)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        if not self._image.isNull():
            iw, ih = self._image.width(), self._image.height()
            source = QRectF(0, 0, iw, ih)
            scale = max(w / source.width(), min(h, 1000) / source.height())
            target = QRectF((w - source.width() * scale) / 2,
                            (min(h, 1000) - source.height() * scale) / 2,
                            source.width() * scale, source.height() * scale)
            painter.setOpacity(.20)
            painter.drawPixmap(target, self._image, source)
            painter.setOpacity(1.0)
            clear = QColor(theme.background)
            clear.setAlpha(0)
            opaque = QColor(theme.background)
            fade = QLinearGradient(0, 0, w, 0)
            fade.setColorAt(0, opaque)
            fade.setColorAt(.20, clear)
            fade.setColorAt(.85, clear)
            fade.setColorAt(1, QColor(opaque.red(), opaque.green(), opaque.blue(), 130))
            painter.fillRect(self.rect(), fade)
            vertical = QLinearGradient(0, target.top(), 0, target.bottom())
            vertical.setColorAt(0, opaque)
            vertical.setColorAt(.18, clear)
            vertical.setColorAt(.80, clear)
            vertical.setColorAt(1, opaque)
            painter.fillRect(self.rect(), vertical)
        painter.end()

    def paintEvent(self, event):
        key = (self.size(), self.devicePixelRatioF(), current_theme())
        if key != self._cache_key:
            self._render()
            self._cache_key = key
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(current_theme().background))
        painter.drawPixmap(0, 0, self._cache)
        painter.end()


class IllustratedPageFrame(QWidget):
    """插图与原页重叠，页面始终占满原有工作区。"""
    def __init__(self, page, artwork_key):
        super().__init__()
        self.page = page
        page.setProperty("artPage", True)
        page.setProperty("artSurface", True)
        for child in page.findChildren(QWidget):
            if child.metaObject().className() in ("QWidget", "QScrollArea", "QStackedWidget"):
                child.setProperty("artSurface", True)
                child.setAutoFillBackground(False)
        self.artwork = PageArtwork(artwork_key, self)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.artwork, 0, 0)
        layout.addWidget(page, 0, 0)
        page.raise_()
        appearance_manager().style_root(page)
