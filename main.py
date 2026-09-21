"""电机控制上位机主程序入口（开发与打包共用）。

运行方式：python main.py [--no-training]
  --no-training  隐藏模型训练页
打包 exe 中若未打入 torch（lite 包），训练页自动隐藏。

"""
import importlib.util
from pathlib import Path
import sys


def _print_runtime_check() -> None:
    """Headless diagnostic used by launchers and installation checks."""
    import motor_core_cpp

    print(f"python={Path(sys.executable).resolve()}")
    print(f"motor_core_cpp={Path(motor_core_cpp.__file__).resolve()}")
    print(
        "telemetry_schema_version="
        f"{int(getattr(motor_core_cpp, 'telemetry_schema_version', 0))}")


if __name__ == "__main__":
    if "--runtime-check" in sys.argv:
        _print_runtime_check()
        raise SystemExit(0)

# 强制 stdout/stderr 使用 UTF-8，避免第三方库输出 emoji 时 GBK 报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QComboBox

from main_window import MainWindow
from runtime_paths import resource_path


class _WheelValueGuard(QObject):
    """让滚轮只负责滚动页面，避免经过参数控件时意外改值。"""

    def eventFilter(self, watched, event):  # noqa: N802 - Qt signature
        if (event.type() == QEvent.Wheel and
                isinstance(watched, (QAbstractSpinBox, QComboBox))):
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def _training_enabled() -> bool:
    if "--no-training" in sys.argv:
        return False
    # lite 打包或精简开发环境可能没有 torch；此时训练页自动隐藏，
    # 其余监控、控制、辨识功能仍可正常启动。
    return importlib.util.find_spec("torch") is not None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("电机控制上位机")
    app.setStyle("Fusion")
    app._wheel_value_guard = _WheelValueGuard(app)
    app.installEventFilter(app._wheel_value_guard)

    # 加载全局深色商务风格
    try:
        with open(resource_path("config", "style.qss"), "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        pass

    window = MainWindow(enable_training=_training_enabled())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
