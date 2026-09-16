"""波形记录目录规划：按类型、日期和单次实验隔离保存。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from runtime_paths import writable_path


_MODE_CATEGORIES = {
    "闭环PI控制": "速度闭环PI",
    "位置三环控制": "位置三环",
    "开环控制": "电流环测试",
}


def category_for_control_mode(mode: str | None) -> str:
    """把界面控制模式转换为稳定、易读的保存分类。"""
    return _MODE_CATEGORIES.get(mode or "", "其他实验")


def create_waveform_record_dir(category: str, when: datetime | None = None,
                               root: Path | None = None) -> Path:
    """创建并返回本次实验的独立目录，重名时安全追加序号。"""
    moment = when or datetime.now()
    safe_category = re.sub(r'[<>:"/\\|?*]+', "_", category).strip(" .")
    if not safe_category:
        safe_category = "其他实验"

    if root is None:
        day_dir = writable_path(
            "波形记录", safe_category, moment.strftime("%Y-%m-%d"), ".keep"
        ).parent
    else:
        day_dir = Path(root) / safe_category / moment.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

    stem = f"实验_{moment:%Y%m%d_%H%M%S}"
    record_dir = day_dir / stem
    suffix = 2
    while record_dir.exists():
        record_dir = day_dir / f"{stem}_{suffix}"
        suffix += 1
    record_dir.mkdir(parents=True)
    return record_dir
