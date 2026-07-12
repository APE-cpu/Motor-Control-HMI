"""电机控制上位机主程序入口（开发与打包共用）。

运行方式：python main.py [--no-training]
  --no-training  隐藏模型训练页
打包 exe 中若未打入 torch（lite 包），训练页自动隐藏。
"""
import importlib.util
import sys

# 强制 stdout/stderr 使用 UTF-8，避免第三方库输出 emoji 时 GBK 报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication

from main_window import MainWindow
from runtime_paths import resource_path


def _training_enabled() -> bool:
    if "--no-training" in sys.argv:
        return False
    if getattr(sys, "frozen", False):
        # lite 打包排除了 torch，此时训练页无法工作，自动隐藏
        return importlib.util.find_spec("torch") is not None
    return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("电机控制上位机")
    app.setStyle("Fusion")

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
