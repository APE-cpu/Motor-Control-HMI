"""PyInstaller 打包入口：禁用模型训练页以减小 exe 体积。"""
import sys

# 强制 stdout/stderr 使用 UTF-8，避免第三方库输出时 GBK 报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication

from main_window import MainWindow
from runtime_paths import resource_path


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("电机控制上位机")
    app.setStyle("Fusion")

    try:
        with open(resource_path("config", "style.qss"), "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        pass

    window = MainWindow(enable_training=False)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
