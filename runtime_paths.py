"""资源/可写路径解析（兼容源码运行与 PyInstaller 单文件 exe）。"""
from __future__ import annotations

import sys
from pathlib import Path


def app_base_dir() -> Path:
    """exe 同级目录（开发期返回项目根）。可写文件应放这里。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """随包只读资源（QSS、默认模型等）。PyInstaller --onefile 解包到 _MEIPASS。"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).joinpath(*parts)
    return app_base_dir().joinpath(*parts)


def writable_path(*parts: str) -> Path:
    """运行时需要写入的文件路径，自动创建父目录。"""
    p = app_base_dir().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
