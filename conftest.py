"""pytest 全局配置。"""
import os
import pytest

# Qt 规定一个进程只能有一个 QApplication。保持会话级强引用，避免多个 GUI
# 测试模块各自创建/析构应用导致 Windows 在 pytest 结束阶段原生崩溃。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_QT_APPLICATION = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _drain_qt_events_after_test():
    """送完后台线程排队的信号和deleteLater，避免跨测试遗留Qt事件。"""
    yield
    _QT_APPLICATION.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _QT_APPLICATION.processEvents()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """在Python扩展模块卸载前显式销毁遗留窗口，避免Windows原生退出崩溃。"""
    _QT_APPLICATION.closeAllWindows()
    for widget in list(_QT_APPLICATION.topLevelWidgets()):
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass
    _QT_APPLICATION.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _QT_APPLICATION.processEvents()
