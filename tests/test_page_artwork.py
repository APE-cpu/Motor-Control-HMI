"""装饰层不能挤占内容、阻挡输入或遗留后台动画。"""
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from widgets.page_artwork import IllustratedPageFrame
from runtime_paths import resource_path


def test_background_overlaps_full_page_and_preserves_title():
    app = QApplication.instance()
    page = QWidget()
    title = QLabel("矢量可视化", page)
    title.setObjectName("TitleLabel")
    QVBoxLayout(page).addWidget(title)
    frame = IllustratedPageFrame(page, "vector")
    try:
        for width in (980, 1740):
            frame.resize(width, 740)
            frame.show()
            app.processEvents()
            assert frame.rect() == page.geometry() == frame.artwork.geometry()
            assert title.isVisible()
            assert frame.artwork.testAttribute(Qt.WA_TransparentForMouseEvents)
    finally:
        frame.close()


def test_real_sprite_alpha_and_animation_lifecycle():
    app = QApplication.instance()
    atlas = QImage(str(resource_path("assets", "page_art", "motor_parts.png")))
    assert not atlas.isNull() and atlas.hasAlphaChannel()
    assert atlas.pixelColor(0, 0).alpha() == 0
    assert atlas.pixelColor(atlas.width() // 2 - 1, atlas.height() // 2 - 1).alpha() == 0
    frame = IllustratedPageFrame(QWidget(), "twin")
    frame.resize(1100, 740)
    frame.show()
    app.processEvents()
    art = frame.artwork
    try:
        art.start_transition("vector")
        assert art._animation.state() == QAbstractAnimation.Running
        assert len(art._parts) == 4
        initial, opened, assembled = (art.part_poses(t) for t in (0, .4, .82))
        assert opened[3][0] - opened[0][0] > initial[3][0] - initial[0][0]
        assert assembled == initial
        art._animation.setCurrentTime(art.DURATION_MS)
        assert art._animation.state() == QAbstractAnimation.Stopped
        assert art._progress == 1.0
        art.start_transition("identify")
        frame.hide()
        app.processEvents()
        assert art._animation.state() == QAbstractAnimation.Stopped
        assert art._progress == 1.0
    finally:
        frame.close()
