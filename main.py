"""电机控制上位机主程序入口（开发与打包共用）。

运行方式：python main.py [--no-training]
  --no-training  隐藏模型训练页
打包 exe 中若未打入 torch（lite 包），训练页自动隐藏。

"""
import importlib
import importlib.util
import math
from pathlib import Path
import sys
import threading
import time


def _print_runtime_check() -> None:
    """Headless diagnostic used by launchers and installation checks."""
    from native_core_loader import prefer_project_native_core

    prefer_project_native_core()
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
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QComboBox

from runtime_paths import resource_path
from widgets.splash_screen import StartupSplash
from ui_theme import APP_NAME, appearance_manager

# 启动时按顺序预导入的重型模块：(模块名, 进度提示, 相对耗时权重)。
# 权重按实测导入耗时（约 0.1 s 为 1）分配进度条长度；导入结果被
# sys.modules 缓存，随后 import main_window 基本不再耗时。
_STARTUP_IMPORTS = [
    ("numpy", "数值计算库", 3),
    ("pyqtgraph", "绘图引擎", 7),
    ("communications.comm_manager", "通信模块", 5),
    ("pages.monitor_page", "监控页面", 1),
    ("pages.control_page", "电机控制", 1),
    ("pages.digital_twin_page", "数字孪生", 1),
    ("pages.vector_page", "矢量可视化", 1),
    ("pages.power_flow_page", "功率流", 1),
    ("pages.identify_page", "参数辨识", 1),
    ("pages.fourier_page", "离线傅里叶", 1),
    ("pages.frequency_response_page", "波特图与传函", 1),
    ("pages.current_sampling_page", "电流采样诊断", 1),
    ("pages.communication_page", "通信设置", 1),
    ("pages.ai_page", "诊断助手", 1),
    ("pages.edge_ai_page", "边缘AI", 2),
    ("pages.experiment_page", "实验管理", 1),
    ("pages.operation_log_page", "操作记录", 1),
    ("pages.manual_page", "使用说明书", 1),
]
# 训练页依赖 PyTorch（约 7~9 s）与 scikit-learn（约 5 s）：放到后台线程导入，
# 前台让进度条继续推进而不是停住。
_TRAINING_IMPORTS = [
    ("torch", "PyTorch 深度学习库（首次加载较慢）", 80),
    ("training.trainer", "scikit-learn 机器学习库", 45),
    ("training.drl_trainer", "强化学习训练器", 12),
    ("pages.training_page", "模型训练", 2),
]
_BACKGROUND_IMPORTS = {"torch", "training.trainer", "training.drl_trainer"}
# 进度区间：样式 0–5，模块导入 5–55，页面构建 55–98，显示窗口 100
_IMPORT_RANGE = (5.0, 55.0)
_BUILD_RANGE = (55.0, 98.0)


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


def _set_windows_app_id() -> None:
    """让任务栏按本程序分组并显示本程序图标（源码运行时否则显示 Python 图标）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MotorControlHMI.Desktop")
    except Exception:
        pass


def _span(bounds: tuple[float, float], done: float, total: float) -> float:
    low, high = bounds
    return low + (high - low) * done / max(total, 1)


def _import_in_background(module: str, on_tick) -> None:
    """后台线程导入模块；等待期间每 50 ms 回调 on_tick(已耗时秒数)。"""
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            importlib.import_module(module)
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            errors.append(exc)

    thread = threading.Thread(target=worker, name=f"import-{module}", daemon=True)
    begin = time.monotonic()
    thread.start()
    while thread.is_alive():
        on_tick(time.monotonic() - begin)
        thread.join(0.05)
    if errors:
        raise errors[0]


def _run_startup_imports(splash: StartupSplash, imports) -> None:
    total = sum(weight for _, _, weight in imports)
    done = 0.0
    for module, label, weight in imports:
        start = _span(_IMPORT_RANGE, done, total)
        end = _span(_IMPORT_RANGE, done + weight, total)
        message = f"正在加载 {label}…"
        splash.set_progress(start, message)
        if module in _BACKGROUND_IMPORTS:
            # 耗时未知：以 4 s 时间常数渐近逼近本步终点，最多走到 95%
            _import_in_background(module, lambda elapsed: splash.set_progress(
                start + (end - start) * 0.95 * (1.0 - math.exp(-elapsed / 4.0)),
                message))
        else:
            importlib.import_module(module)
        done += weight


def main() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(resource_path("assets", "app_icon_charcoal.ico"))))
    app.setStyle("Fusion")
    app._wheel_value_guard = _WheelValueGuard(app)
    app.installEventFilter(app._wheel_value_guard)

    splash = StartupSplash()
    splash.show()
    splash.set_progress(0, "正在启动…")

    try:
        appearance = appearance_manager()
        appearance.apply_theme(appearance.theme_id, persist=False)

        enable_training = _training_enabled()
        imports = list(_STARTUP_IMPORTS)
        if enable_training:
            imports += _TRAINING_IMPORTS
        _run_startup_imports(splash, imports)

        from main_window import APP_VERSION, MainWindow

        splash.set_version(APP_VERSION)
        window = MainWindow(
            enable_training=enable_training,
            progress=lambda done, total, label: splash.set_progress(
                _span(_BUILD_RANGE, done, total), f"正在初始化 {label}…"),
        )
    except BaseException:
        splash.close()
        raise

    window.show()
    splash.finish(window)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
