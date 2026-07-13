"""操作日志单例：记录按钮操作历史，落盘并通过信号推送给日志页面。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Signal

from runtime_paths import writable_path

_LOG_FILE = writable_path("logs", "operation_log.txt")


def classify_level(line: str) -> str:
    """按行文本启发式判定日志级别：error / warn / info。

    不依赖 logger 调用点传级别（那要改几十处），直接看关键词。
    """
    low = line
    if any(k in low for k in ("错误", "失败", "[错误]", "[DRL错误]", "ERROR", "Error")):
        return "error"
    if any(k in low for k in ("警告", "告警", "[警告]", "紧急停止", "WARN")):
        return "warn"
    return "info"


class OperationLogger(QObject):
    newEntry = Signal(str)
    # 结构化记录供实验归档等后台消费者使用；保留 newEntry 兼容现有日志页。
    newRecord = Signal(str, str, str)  # timestamp_iso, action, detail

    def log(self, action: str, detail: str = "") -> None:
        now = datetime.now().astimezone()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {action}" + (f"  {detail}" if detail else "")
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.newEntry.emit(line)
        self.newRecord.emit(now.isoformat(timespec="milliseconds"), action, detail)

    def load_recent(self, limit: int = 500) -> list[str]:
        try:
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            return lines[-limit:]
        except OSError:
            return []


logger = OperationLogger()
