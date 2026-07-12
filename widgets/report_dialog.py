"""AI 实验报告对话框：把实验数据交给大模型，流式生成格式化 Markdown 报告。

复用 AI 分析页的 API 配置（config/ai_config.json）；报告可保存到 reports/。
"""
import base64
import json
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from ai.ai_client import AIClient
from config.config import AI_REQUEST_TIMEOUT
from logs.operation_logger import logger
from runtime_paths import writable_path

_CONFIG_FILE = writable_path("config", "ai_config.json")
_REPORT_DIR = writable_path("reports", ".keep").parent

_REPORT_PROMPT = """你是一名电机控制实验工程师。请根据下面提供的实验数据，\
生成一份规范的实验报告，使用 Markdown 格式，结构如下：

# 实验报告标题（含日期）
## 一、实验目的
## 二、实验条件（用表格列出配置参数）
## 三、实验过程
## 四、数据与结果（用表格呈现关键数据）
## 五、结果分析
## 六、结论与建议

要求：数据必须完全忠实于提供的内容，禁止编造未提供的数值；\
分析部分要给出工程判断（是否正常、误差是否可接受、可能的改进方向）；\
语言专业简洁。若附有波形图，请结合图形形态进行分析。"""


def load_ai_client():
    """从 AI 分析页保存的配置构造客户端；未配置返回 None。"""
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    url = cfg.get("base_url", "").rstrip("/")
    if url and not url.endswith("/v1"):
        url += "/v1"
    if not url or not cfg.get("model"):
        return None
    return AIClient(url, cfg.get("api_key", ""), cfg["model"])


class _ReportWorker(QObject):
    chunk = Signal(str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, client: AIClient, context_text: str,
                 images: list) -> None:
        super().__init__()
        self._client = client
        self._context = context_text
        self._images = images or []

    def run(self) -> None:
        if self._images:
            content = [{"type": "text", "text": self._context}]
            for mime, data in self._images:
                b64 = base64.b64encode(data).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
        else:
            content = self._context
        messages = [{"role": "system", "content": _REPORT_PROMPT},
                    {"role": "user", "content": content}]
        try:
            full = self._client.chat_stream(
                messages, timeout=AI_REQUEST_TIMEOUT, on_delta=self.chunk.emit)
            self.finished.emit(full)
        except Exception as exc:
            self.error.emit(str(exc))


class ExperimentReportDialog(QDialog):
    """打开即开始流式生成；支持重新生成与保存 Markdown。"""

    def __init__(self, experiment_name: str, context_text: str,
                 images: list = None, parent=None) -> None:
        super().__init__(parent)
        self._name = experiment_name
        self._context = context_text
        self._images = images or []
        self._worker = None
        self._report_md = ""

        self.setWindowTitle(f"AI 实验报告 — {experiment_name}")
        self.resize(760, 640)
        root = QVBoxLayout(self)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #8fa3b8;")
        root.addWidget(self._status)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        root.addWidget(self._text, 1)

        h = QHBoxLayout()
        self._btn_regen = QPushButton("重新生成")
        self._btn_regen.clicked.connect(self._start)
        self._btn_save = QPushButton("保存 Markdown")
        self._btn_save.setObjectName("PrimaryButton")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._on_save)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        h.addWidget(self._btn_regen)
        h.addWidget(self._btn_save)
        h.addStretch(1)
        h.addWidget(btn_close)
        root.addLayout(h)

        self._start()

    # ─── 生成 ───────────────────────────────────────────────
    def _start(self) -> None:
        client = load_ai_client()
        if client is None:
            self._status.setText("未找到 AI 配置——请先到「AI 分析」页填写并保存 API 配置。")
            return
        self._text.clear()
        self._report_md = ""
        self._btn_regen.setEnabled(False)
        self._btn_save.setEnabled(False)
        extra = f"（附 {len(self._images)} 张波形图）" if self._images else ""
        self._status.setText(f"正在生成实验报告…{extra}")
        logger.log("AI实验报告", f"开始生成：{self._name}")

        self._worker = _ReportWorker(client, self._context, self._images)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        threading.Thread(target=self._worker.run, daemon=True).start()

    def _on_chunk(self, delta: str) -> None:
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(delta)
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def _on_done(self, full: str) -> None:
        self._report_md = full
        self._status.setText("生成完成。可保存为 Markdown 文件。")
        self._btn_regen.setEnabled(True)
        self._btn_save.setEnabled(bool(full.strip()))
        logger.log("AI实验报告", f"生成完成：{self._name}（{len(full)} 字）")
        self._worker = None

    def _on_error(self, err: str) -> None:
        self._status.setText(f"生成失败：{err}")
        self._btn_regen.setEnabled(True)
        logger.log("AI实验报告失败", err[:120])
        self._worker = None

    # ─── 保存 ───────────────────────────────────────────────
    def _on_save(self) -> None:
        default = _REPORT_DIR / (
            f"{self._name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存实验报告", str(default), "Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._report_md)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        logger.log("AI实验报告", f"已保存：{path}")
        in_kb = Path(path).parent.resolve() == _REPORT_DIR.resolve()
        hint = ("\n\n该报告已自动纳入 AI 知识库（RAG）——"
                "以后在 AI 分析页提问时可检索到本次实验。") if in_kb else ""
        QMessageBox.information(self, "已保存", f"{path}{hint}")
