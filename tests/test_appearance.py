from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel
import pyqtgraph as pg

from ui_theme import (DEFAULT_THEME, THEMES, ThemeManager, appearance_manager,
                      brand_font, themed_stylesheet)
from widgets.appearance_bar import AppearanceBar


def test_theme_roundtrip_preserves_status_colors_and_curve_data():
    app = QApplication.instance()
    manager = appearance_manager()
    original_theme = manager.theme_id
    label = QLabel("Status")
    original_style = "color:#4fc3f7; background:#10131a; border:1px solid #ef5350;"
    label.setStyleSheet(original_style)
    plot = pg.PlotWidget()
    curve = plot.plot([1, 2, 3], [3, 2, 1], pen="#4fc3f7")
    label.show()
    plot.show()
    app.processEvents()
    before_pen = pg.mkPen(curve.opts["pen"]).color().name()
    try:
        for key, theme in THEMES.items():
            manager.apply_theme(key, persist=False)
            assert "#ef5350" in label.styleSheet()
            assert theme.field in label.styleSheet()
            assert pg.mkPen(curve.opts["pen"]).color().name() == before_pen
            assert list(curve.getData()[1]) == [3, 2, 1]
            assert plot.backgroundBrush().color().name() == theme.field
        manager.apply_theme("blue", persist=False)
        assert label.styleSheet() == original_style
        manager.apply_theme("graphite", persist=False)
        label.show()
        label.setStyleSheet("color:#4fc3f7; font-weight:bold;")
        app.processEvents()
        assert THEMES["graphite"].accent in label.styleSheet()
        label.setStyleSheet("color:#ef5350;")
        assert label.styleSheet() == "color:#ef5350;"
    finally:
        manager.apply_theme(original_theme, persist=False)
        plot.close()
        label.close()


def test_appearance_controls_persist_and_restore(tmp_path):
    app = QApplication.instance()
    manager = appearance_manager()
    old_settings, old_theme = manager.settings, manager.theme_id
    path = str(tmp_path / "appearance.ini")
    manager.settings = QSettings(path, QSettings.IniFormat)
    bar = AppearanceBar()
    restored = None
    try:
        bar.theme_combo.setCurrentIndex(bar.theme_combo.findData("ocean"))
        restored = ThemeManager(app, QSettings(path, QSettings.IniFormat))
        assert restored.theme_id == "ocean"
        assert not hasattr(bar, "font_combo")
        assert brand_font(27).italic()
    finally:
        if restored is not None:
            app.removeEventFilter(restored)
            restored.deleteLater()
        manager.settings = old_settings
        manager.apply_theme(old_theme, persist=False)
        bar.close()


def test_invalid_preferences_fall_back_and_alarm_palette_is_unchanged(tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "invalid.ini"), QSettings.IniFormat)
    settings.setValue("appearance/theme", "missing")
    manager = ThemeManager(app, settings)
    try:
        assert manager.theme_id == DEFAULT_THEME
        alarm = "color:#ef5350; background:#b71c1c; border-color:#66bb6a;"
        for theme in THEMES.values():
            assert themed_stylesheet(alarm, theme) == alarm
    finally:
        app.removeEventFilter(manager)
        manager.deleteLater()


def test_old_theme_preferences_migrate_and_old_font_choice_is_ignored(tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "old.ini"), QSettings.IniFormat)
    settings.setValue("appearance/theme", "violet")
    settings.setValue("appearance/brand_font", "calligraphy")
    manager = ThemeManager(app, settings)
    try:
        assert manager.theme_id == "titanium"
        assert brand_font(58).italic()
    finally:
        app.removeEventFilter(manager)
        manager.deleteLater()


def test_main_window_theme_switch_styles_visible_page_then_new_page():
    from main_window import MainWindow

    app = QApplication.instance()
    window = MainWindow(enable_training=False)
    window.show()
    app.processEvents()
    manager = appearance_manager()
    original_theme = manager.theme_id
    global_stylesheet = app.styleSheet()
    next_page = window.stack.widget(3)
    target = "ocean" if original_theme != "ocean" else "titanium"
    try:
        manager.apply_theme(target, persist=False)
        assert app.styleSheet() == global_stylesheet
        assert window.appearance_bar.styleSheet() == manager._stylesheet_for(target)
        assert window.stack.currentWidget().styleSheet() == manager._stylesheet_for(target)
        assert next_page.styleSheet() != manager._stylesheet_for(target)
        window.hide()  # 无界面测试不启动页面过渡动画
        window._switch_page(3)
        assert next_page.styleSheet() == manager._stylesheet_for(target)
    finally:
        manager.apply_theme(original_theme, persist=False)
        window.close()
