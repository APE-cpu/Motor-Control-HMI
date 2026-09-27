"""启动画面：品牌底图 + 版本号 + 加载状态 + 进度条。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from runtime_paths import resource_path
from ui_theme import APP_NAME, APP_SUBTITLE, brand_font

SPLASH_WIDTH = 760
ART_HEIGHT = 476
SPLASH_HEIGHT = 542

_ACCENT = QColor("#f04b27")
_TEXT = QColor("#f1efe9")
_TEXT_DIM = QColor("#babbb7")


class StartupSplash(QWidget):
    def __init__(self, version: str = "") -> None:
        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(SPLASH_WIDTH, SPLASH_HEIGHT)
        self._version = version
        self._progress = 0
        self._message = "正在启动…"

        pixmap = QPixmap(str(resource_path("assets", "splash_yuheng.png")))
        if not pixmap.isNull():
            pixmap = pixmap.scaled(SPLASH_WIDTH * 2, ART_HEIGHT * 2,
                                   Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
            x = (pixmap.width() - SPLASH_WIDTH * 2) // 2
            y = (pixmap.height() - ART_HEIGHT * 2) // 2
            pixmap = pixmap.copy(x, y, SPLASH_WIDTH * 2, ART_HEIGHT * 2)
            pixmap.setDevicePixelRatio(2)
        self._background = pixmap
        self._icon = QPixmap(str(resource_path("assets", "app_icon_charcoal.png")))

        screen = QApplication.primaryScreen()
        if screen is not None:
            center = screen.availableGeometry().center()
            self.move(center.x() - SPLASH_WIDTH // 2, center.y() - SPLASH_HEIGHT // 2)

    @property
    def progress(self) -> int:
        return self._progress

    @property
    def message(self) -> str:
        return self._message

    def set_version(self, version: str) -> None:
        self._version = version
        self._refresh()

    def set_progress(self, value: float, message: str | None = None) -> None:
        """更新进度（0–100，只增不减）和状态文字，并立即重绘。"""
        self._progress = max(self._progress, min(100, int(round(value))))
        if message:
            self._message = message
        self._refresh()

    def finish(self, window: QWidget) -> None:
        """主窗口已显示后关闭启动画面。"""
        self.set_progress(100, "启动完成")
        window.raise_()
        window.activateWindow()
        self.close()

    def _refresh(self) -> None:
        if self.isVisible():
            self.repaint()
        QApplication.processEvents()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        rect = QRectF(self.rect())
        p.fillRect(rect, QColor("#242525"))

        if self._background.isNull():
            self._paint_fallback(p, rect)
        else:
            p.drawPixmap(0, 0, self._background)
        self._paint_brand(p)

        if self._version:
            self._paint_version(p, rect)
        self._paint_progress(p, rect)
        p.end()

    def _paint_fallback(self, p: QPainter, rect: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        bg = QLinearGradient(rect.topLeft(), rect.bottomRight())
        bg.setColorAt(0.0, QColor("#292a29"))
        bg.setColorAt(1.0, QColor("#222323"))
        p.fillPath(path, bg)
        p.setPen(QPen(QColor(240, 75, 39, 70), 1))
        p.drawPath(path)

    def _paint_brand(self, p: QPainter) -> None:
        p.setFont(brand_font(58))
        p.setPen(_TEXT)
        p.drawText(QRectF(34, 177, 322, 79),
                   Qt.AlignLeft | Qt.AlignVCenter, APP_NAME)
        font = QFont("Microsoft YaHei")
        font.setPixelSize(15)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        p.setFont(font)
        p.setPen(_TEXT_DIM)
        p.drawText(QRectF(38, 256, 305, 28), Qt.AlignLeft, APP_SUBTITLE)
        font = QFont("Bahnschrift")
        font.setPixelSize(11)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1)
        p.setFont(font)
        p.setPen(_ACCENT)
        p.drawText(QRectF(38, 288, 306, 22), Qt.AlignLeft, "YUHENG INTELLIGENT CONTROL")

    def _paint_version(self, p: QPainter, rect: QRectF) -> None:
        font = QFont("Bahnschrift")
        font.setPixelSize(13)
        p.setFont(font)
        text = f"v{self._version}"
        width = p.fontMetrics().horizontalAdvance(text) + 22
        pill = QRectF(rect.right() - width - 20, 20, width, 24)
        p.setPen(QPen(QColor(240, 75, 39, 100), 1))
        p.setBrush(QColor(34, 35, 35, 220))
        p.drawRoundedRect(pill, 12, 12)
        p.setPen(_TEXT)
        p.drawText(pill, Qt.AlignCenter, text)

    def _paint_progress(self, p: QPainter, rect: QRectF) -> None:
        left, right = 34.0, rect.width() - 34.0
        bar_y = rect.height() - 20.0

        font = QFont("Microsoft YaHei")
        font.setPixelSize(13)
        p.setFont(font)
        p.setPen(_TEXT_DIM)
        p.drawText(QRectF(left, bar_y - 30, right - left - 60, 22),
                   Qt.AlignLeft | Qt.AlignVCenter, self._message)

        num_font = QFont("Bahnschrift")
        num_font.setPixelSize(15)
        p.setFont(num_font)
        p.setPen(_TEXT)
        p.drawText(QRectF(right - 60, bar_y - 30, 60, 22),
                   Qt.AlignRight | Qt.AlignVCenter, f"{self._progress}%")

        track = QRectF(left, bar_y, right - left, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(track, 3, 3)

        if self._progress <= 0:
            return
        fill = QRectF(track.left(), track.top(),
                      max(track.height(), track.width() * self._progress / 100),
                      track.height())
        p.setBrush(_ACCENT)
        p.drawRoundedRect(fill, 3, 3)
