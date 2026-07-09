"""操作日志单例：记录按钮操作历史，落盘并通过信号推送给日志页面。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Signal

from runtime_paths import writable_path

_LOG_FILE = writable_path("logs", "operation_log.txt")


class OperationLogger(QObject):
    newEntry = Signal(str)

    def log(self, action: str, detail: str = "") -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {action}" + (f"  {detail}" if detail else "")
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.newEntry.emit(line)

    def load_recent(self, limit: int = 500) -> list[str]:
        try:
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            return lines[-limit:]
        except OSError:
            return []


logger = OperationLogger()
