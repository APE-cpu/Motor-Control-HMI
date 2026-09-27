import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

import main
from widgets.splash_screen import StartupSplash


def _app():
    return QApplication.instance() or QApplication([])


def test_splash_progress_is_clamped_and_never_goes_backwards():
    _app()
    splash = StartupSplash()
    splash.set_progress(40, "正在加载 A…")
    splash.set_progress(20, "正在加载 B…")
    assert splash.progress == 40
    assert splash.message == "正在加载 B…"
    splash.set_progress(250)
    assert splash.progress == 100
    splash.close()


def test_splash_paints_and_finish_closes_it():
    _app()
    splash = StartupSplash()
    splash.show()
    splash.set_version("9.9.9")
    splash.set_progress(60, "正在初始化 监控页面…")
    assert not splash.grab().isNull()
    window = QWidget()
    window.show()
    splash.finish(window)
    assert splash.progress == 100
    assert not splash.isVisible()
    window.close()


def test_startup_imports_advance_through_weighted_range(monkeypatch):
    _app()
    monkeypatch.setattr(main, "_BACKGROUND_IMPORTS", {"csv"})
    seen = []
    splash = StartupSplash()
    original = splash.set_progress

    def record(value, message=None):
        original(value, message)
        seen.append((splash.progress, splash.message))

    monkeypatch.setattr(splash, "set_progress", record)
    main._run_startup_imports(splash, [("json", "A", 1), ("csv", "B", 3)])

    low, high = main._IMPORT_RANGE
    assert seen[0] == (int(low), "正在加载 A…")
    # 权重 1:3 → 第二步从导入区间的 1/4 处开始
    assert (int(round(low + (high - low) / 4)), "正在加载 B…") in seen
    assert all(p <= high for p, _ in seen)
    splash.close()


def test_background_import_error_is_raised_in_main_thread(monkeypatch):
    _app()
    monkeypatch.setattr(main, "_BACKGROUND_IMPORTS", {"no_such_module_for_splash"})
    splash = StartupSplash()
    with pytest.raises(ModuleNotFoundError):
        main._run_startup_imports(splash, [("no_such_module_for_splash", "X", 1)])
    splash.close()


def test_main_window_reports_every_startup_step():
    _app()
    from main_window import MainWindow

    calls = []
    window = MainWindow(enable_training=False,
                        progress=lambda done, total, label: calls.append((done, total, label)))
    assert [done for done, _, _ in calls] == list(range(len(calls)))
    assert {total for _, total, _ in calls} == {len(calls)}
    assert calls[-1][2] == "组装主界面"
    window.close()
