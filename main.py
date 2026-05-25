"""电机控制上位机主程序入口。

启动后会加载主窗口（含侧边导航 + 三个功能页面）。
运行方式：python main.py
"""
import sys
import io

# 强制 stdout/stderr 使用 UTF-8，避免第三方库输出 emoji 时 GBK 报错
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

    # 加载全局深色商务风格
    try:
        with open(resource_path("config", "style.qss"), "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        pass

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
