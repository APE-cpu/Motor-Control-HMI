"""整页机械背景与独立零件重组动画；静止时不刷新、不占布局空间。"""
from PySide6.QtCore import QRectF, Qt, QSize, QVariantAnimation, QEasingCurve, QElapsedTimer
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
        background-color: rgba({panel.red()}, {panel.green()}, {panel.blue()}, 100);
    }}
    QWidget[artPage="true"] QTabWidget::pane {{ background: transparent; }}
    QWidget[artPage="true"] QTabBar {{ background: transparent; }}
    QWidget[artPage="true"] QGraphicsView {{ background: transparent; }}
    QWidget[artPage="true"] QTextEdit,
    QWidget[artPage="true"] QPlainTextEdit {{
        background-color: rgba({field.red()}, {field.green()}, {field.blue()}, 130);
    }}
    QWidget[artPage="true"] QCheckBox {{ background: transparent; }}
    '''


def ease(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


class PageArtwork(QWidget):
    """背景层：插图按尺寸缓存；切页短时绘制四个独立机械零件。"""
    DURATION_MS = 1000

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self._image = QPixmap()
        self._cache = QPixmap()
        self._cache_key = None
        self._parts = []
        self._progress = 1.0
        self._source_exploded = False
        self._paint_clock = QElapsedTimer()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Linear)
        self._animation.valueChanged.connect(self._advance)
        self._animation.finished.connect(self.stop_transition)
        appearance_manager().themeChanged.connect(self._invalidate)

    def sizeHint(self):
        return QSize(0, 0)

    def _invalidate(self, _theme=None):
        self._cache_key = None
        self.update()

    def _advance(self, value):
        self._progress = float(value)
        # 动画跟随墙钟推进，但背景至多约 30 Hz 重绘，不拖累实时曲线。
        if not self._paint_clock.isValid() or self._paint_clock.elapsed() >= 33:
            self._paint_clock.start()
            self.update()

    def showEvent(self, event):
        if self._image.isNull():
            self._image.load(str(resource_path("assets", "page_art", f"{self.key}.png")))
            self._cache_key = None
        super().showEvent(event)

    def hideEvent(self, event):
        self.stop_transition()
        super().hideEvent(event)

    def start_transition(self, source_key=None):
        self.stop_transition()
        if not self._parts:
            atlas = QPixmap(str(resource_path("assets", "page_art", "motor_parts.png")))
            if atlas.isNull():
                return
            tw, height = atlas.width() // 2, atlas.height()
            # 四个透明零件，运行时从图集读取；不是整页截图碎片。
            # 上排单元底部留出隔离带，避免下一排的零件边缘混入图层。
            self._parts = [atlas.copy(x * tw, 0, tw, round(height * .45))
                           for x in range(2)]
            self._parts += [atlas.copy(x * tw, round(height * .47),
                                      tw, height - round(height * .47))
                            for x in range(2)]
        self._source_exploded = source_key == "identify"
        self._progress = 0.0
        self._paint_clock.invalidate()
        self._animation.start()

    def stop_transition(self):
        self._animation.stop()
        self._progress = 1.0
        self.update()

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
            source = QRectF(iw * .18, 0, iw * .82, ih)
            scale = max(w / source.width(), min(h, 1000) / source.height())
            target = QRectF((w - source.width() * scale) / 2,
                            (min(h, 1000) - source.height() * scale) / 2,
                            source.width() * scale, source.height() * scale)
            painter.setOpacity(.32)
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

    def part_poses(self, progress):
        """归一化零件中心和旋角：前盖、转子、定子、后壳。"""
        assembled = [(-.12, .025), (-.035, .0), (.025, -.01), (.10, -.025)]
        exploded = [(-.32, .12), (-.11, .04), (.115, -.04), (.32, -.12)]
        opening = ease(progress / .38)
        closing = ease((progress - .42) / .40)
        amount = 1.0 if self._source_exploded else opening
        if self.key != "identify":
            amount *= 1.0 - closing
        return [(a[0] + (b[0] - a[0]) * amount,
                 a[1] + (b[1] - a[1]) * amount,
                 (i - 1.5) * 4.0 * amount)
                for i, (a, b) in enumerate(zip(assembled, exploded))]

    def _paint_parts(self, painter):
        t = self._progress
        alpha = ease(t / .14) * (1.0 - ease((t - .82) / .18))
        width = min(self.width() * .95, 1300.0)
        cx, cy = self.width() * .52, min(self.height(), 900) * .52
        poses = self.part_poses(t)
        for index in (3, 2, 1, 0):
            part = self._parts[index]
            x, y, angle = poses[index]
            pw = width * (.40 if index == 1 else .36)
            ph = pw * part.height() / part.width()
            painter.save()
            painter.setOpacity(alpha * .67)
            painter.translate(cx + x * width, cy + y * width)
            painter.rotate(angle)
            painter.drawPixmap(QRectF(-pw / 2, -ph / 2, pw, ph), part, QRectF(part.rect()))
            painter.restore()

    def paintEvent(self, event):
        key = (self.size(), self.devicePixelRatioF(), current_theme())
        if key != self._cache_key:
            self._render()
            self._cache_key = key
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(current_theme().background))
        t = self._progress
        if t < 1.0 and self._parts:
            painter.setOpacity(1.0 - .90 * ease(t / .15) * (1 - ease((t - .80) / .20)))
        painter.drawPixmap(0, 0, self._cache)
        painter.setOpacity(1.0)
        if t < 1.0 and self._parts:
            self._paint_parts(painter)
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
