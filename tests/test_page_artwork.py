"""装饰层不能挤占内容、阻挡输入或遗留后台动画。"""
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from widgets.page_artwork import IllustratedPageFrame


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


def test_structure_pages_keep_plots_opaque_across_themes():
    import pyqtgraph as pg
    from ui_theme import appearance_manager, THEMES
    app = QApplication.instance()
    manager = appearance_manager()
    original = manager.theme_id
    page = QWidget()
    plot = pg.PlotWidget()
    QVBoxLayout(page).addWidget(plot)
    frame = IllustratedPageFrame(page, "vector")
    frame.resize(1100, 740)
    frame.show()
    app.processEvents()
    try:
        assert not frame.artwork._image.isNull()
        assert not frame.artwork.findChildren(QAbstractAnimation)
        for key, theme in THEMES.items():
            manager.apply_theme(key, persist=False)
            manager.style_root(page)
            app.processEvents()
            color = plot.backgroundBrush().color()
            assert color.alpha() == 255
            assert color.name() == theme.field
    finally:
        frame.close()
        manager.apply_theme(original, persist=False)
