"""优先加载随当前项目安装的 C++ 扩展。"""

from pathlib import Path
import sys


def prefer_project_native_core() -> None:
    runtime_dir = Path(__file__).resolve().parent / "native_core_runtime"
    if runtime_dir.is_dir():
        path = str(runtime_dir)
        if path not in sys.path:
            sys.path.insert(0, path)
