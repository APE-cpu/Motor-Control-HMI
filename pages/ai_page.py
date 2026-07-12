"""AI 分析页面：接入外部大模型，分析电机运行状态与故障诊断。

支持 RAG（检索增强生成）：本地 BM25 检索项目文档与 knowledge/ 目录资料，
把相关片段注入提示词，让回答有据可依。
"""
import base64
import json
import os
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ai.ai_client import AIClient
from ai.rag import RAGIndex, format_context
from communications.comm_manager import CommManager, TelemetryFrame
from config.config import AI_DEFAULT_BASE_URL, AI_DEFAULT_MODEL, AI_REQUEST_TIMEOUT
from logs.operation_logger import logger
from runtime_paths import app_base_dir, writable_path

_CONFIG_FILE = writable_path("config", "ai_config.json")
_KNOWLEDGE_DIR = writable_path("knowledge", ".keep").parent
_REPORT_DIR = writable_path("reports", ".keep").parent

# 随包自带的知识文档（项目根目录）
_BUILTIN_DOCS = ["README.md", "使用说明书.md", "软件介绍.md"]

_SYS_PROMPT = """你是一名电机控制专家。当前电机遥测数据如下：
{snapshot}
请根据以上数据回答用户的问题，重点关注异常状态和故障诊断。回答简洁专业。"""

_SYS_PROMPT_RAG = """你是一名电机控制专家。当前电机遥测数据如下：
{snapshot}

以下是从本地知识库检索到的参考资料。回答时优先依据资料内容，引用处标注来源
（如“据资料1”）；资料未覆盖的部分再用你的通用知识并注明：
{context}

请根据以上信息回答用户的问题，重点关注异常状态和故障诊断。回答简洁专业。"""


def _normalize_url(url: str) -> str:
    """确保 base_url 以 /v1 结尾。"""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


class _Worker(QObject):
    chunk = Signal(str)       # 流式增量文本
    finished = Signal(str)    # 完整回答
    error = Signal(str)

    def __init__(self, client: AIClient, messages: list) -> None:
        super().__init__()
        self._client = client
        self._messages = messages

    def run(self) -> None:
        try:
            reply = self._client.chat_stream(
                self._messages, timeout=AI_REQUEST_TIMEOUT,
                on_delta=self.chunk.emit)
            self.finished.emit(reply)
        except Exception as exc:
            if "timed out" in str(exc).lower():
                self.error.emit(
                    f"网络读等待超过 {AI_REQUEST_TIMEOUT}s（流式下为相邻数据块"
                    "间隔），请检查网络后重试。")
            else:
                self.error.emit(str(exc))


_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".bmp": "image/bmp", ".webp": "image/webp"}

_WAVEFORM_QUESTION = "请分析这张波形截图，判断电机运行状态是否正常，有无异常或故障迹象。"


class _IndexBuilder(QObject):
    """后台构建 RAG 索引（扫描版 PDF OCR 可能耗时数分钟）。"""
    progress = Signal(str)
    done = Signal(object)     # 构建完成的 RAGIndex

    def __init__(self, paths: list) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        index = RAGIndex(self._paths)
        index.build(progress=self.progress.emit)
        self.done.emit(index)


class AIPage(QWidget):
    def __init__(self, comm: CommManager, monitor_page=None) -> None:
        super().__init__()
        self._comm = comm
        self._monitor = monitor_page
        self._latest: TelemetryFrame = TelemetryFrame()
        self._worker = None  # 持有引用，防止 GC
        self._attachments: list = []  # [(文件名, mime, 原始字节)]
        self._rag_index: RAGIndex | None = None
        self._rag_builder = None      # 后台索引构建 worker（防 GC）
        self._rag_building = False

        root = QVBoxLayout(self)

        title = QLabel("AI 电机分析")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        root.addWidget(self._build_config_box())
        root.addWidget(self._build_snapshot_box())
        root.addWidget(self._build_chat_box(), 1)

        comm.telemetryReceived.connect(self._on_telemetry)
        self._load_config()
        # 启动即后台构建索引：有缓存时 <1s 就绪，首个问题不再错过检索
        self._ensure_rag_index()

    # -------- 子构件 --------
    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("API 配置")
        f = QFormLayout(box)
        self._base_url = QLineEdit(AI_DEFAULT_BASE_URL)
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.Password)
        self._api_key.setPlaceholderText("sk-...")
        self._model = QLineEdit(AI_DEFAULT_MODEL)
        btn_save = QPushButton("保存配置")
        btn_save.clicked.connect(self._on_save_config)
        f.addRow("Base URL", self._base_url)
        f.addRow("API Key", self._api_key)
        f.addRow("模型名称", self._model)
        f.addRow("", btn_save)
        self._config_status = QLabel("未保存")
        f.addRow("状态", self._config_status)
        return box

    def _build_snapshot_box(self) -> QGroupBox:
        box = QGroupBox("当前电机状态快照")
        v = QVBoxLayout(box)
        self._snapshot_label = QPlainTextEdit()
        self._snapshot_label.setReadOnly(True)
        self._snapshot_label.setMaximumHeight(90)
        self._snapshot_label.setPlainText("（暂无数据，请先启动仿真或连接通信）")
        v.addWidget(self._snapshot_label)
        return box

    def _build_chat_box(self) -> QGroupBox:
        box = QGroupBox("对话")
        v = QVBoxLayout(box)
        self._chat_display = QPlainTextEdit()
        self._chat_display.setReadOnly(True)
        v.addWidget(self._chat_display, 1)

        h = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入问题，例如：当前转速是否正常？有无故障迹象？")
        self._input.returnPressed.connect(self._on_send)
        self._btn_send = QPushButton("发送")
        self._btn_send.setObjectName("PrimaryButton")
        self._btn_send.clicked.connect(self._on_send)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._chat_display.clear)
        h.addWidget(self._input, 1)
        h.addWidget(self._btn_send)
        h.addWidget(btn_clear)
        v.addLayout(h)

        # 图片附件行（需要支持视觉输入的模型，如 qwen3.7-plus / qwen-vl 系列）
        h2 = QHBoxLayout()
        btn_img = QPushButton("添加图片")
        btn_img.clicked.connect(self._on_add_images)
        self._btn_wave = QPushButton("分析当前波形")
        self._btn_wave.clicked.connect(self._on_analyze_waveform)
        self._attach_label = QLabel("")
        self._attach_label.setStyleSheet("color: #90a4ae;")
        btn_attach_clear = QPushButton("移除图片")
        btn_attach_clear.clicked.connect(self._clear_attachments)
        h2.addWidget(btn_img)
        h2.addWidget(self._btn_wave)
        h2.addWidget(btn_attach_clear)
        h2.addWidget(self._attach_label, 1)
        v.addLayout(h2)

        # RAG 行：知识库增强开关与管理
        h3 = QHBoxLayout()
        self._chk_rag = QCheckBox("📚 知识库增强 (RAG)")
        self._chk_rag.setChecked(True)
        self._chk_rag.setToolTip(
            "发送前用 BM25 从本地知识库检索相关片段注入提示词，让回答有据可依。\n"
            "知识库 = 项目自带文档（README/使用说明书/软件介绍）+ knowledge/ 目录\n"
            "（支持 md/txt/pdf——PDF 教材/论文自动提取文本并按页码标注出处）\n"
            "+ reports/ 目录的历史实验报告（AI 因此记得之前的实验结果与异常）。")
        btn_add_doc = QPushButton("添加文档…")
        btn_add_doc.setToolTip(f"把 md/txt 资料复制到知识库目录：\n{_KNOWLEDGE_DIR}")
        btn_add_doc.clicked.connect(self._on_add_docs)
        btn_rebuild = QPushButton("重建索引")
        btn_rebuild.clicked.connect(self._on_rebuild_index)
        btn_help = QPushButton("工作原理")
        btn_help.setToolTip("RAG 检索增强生成的完整工作流程说明")
        btn_help.clicked.connect(self._on_rag_help)
        self._rag_status = QLabel("知识库未索引（首次提问时自动构建）")
        self._rag_status.setStyleSheet("color: #90a4ae;")
        h3.addWidget(self._chk_rag)
        h3.addWidget(btn_add_doc)
        h3.addWidget(btn_rebuild)
        h3.addWidget(btn_help)
        h3.addWidget(self._rag_status, 1)
        v.addLayout(h3)
        return box

    # -------- RAG --------
    @staticmethod
    def _gather_rag_paths() -> list:
        base = app_base_dir()
        paths = [base / name for name in _BUILTIN_DOCS]
        paths += sorted(_KNOWLEDGE_DIR.glob("*.md"))
        paths += sorted(_KNOWLEDGE_DIR.glob("*.txt"))
        paths += sorted(_KNOWLEDGE_DIR.glob("*.pdf"))   # 教材/论文，需 pypdf
        # 历史实验报告自动入库：模型因此记得之前的实验结果与异常
        paths += sorted(_REPORT_DIR.glob("*.md"))
        return [p for p in paths if p.exists() and p.name != ".keep"]

    def _ensure_rag_index(self):
        """索引最新则直接返回；需要重建则后台构建并返回 None（不阻塞 UI）。

        重建可能很慢（扫描版 PDF 逐页 OCR），故放到线程里，
        进度实时显示在状态标签上，完成后自动可用。
        """
        paths = self._gather_rag_paths()
        if (self._rag_index is not None
                and self._rag_index.sources == [str(p) for p in paths]
                and not self._rag_index.needs_rebuild()):
            return self._rag_index
        if self._rag_building:
            return None
        self._rag_building = True
        self._rag_builder = _IndexBuilder(paths)
        self._rag_builder.progress.connect(self._rag_status.setText)
        self._rag_builder.done.connect(self._on_index_ready)
        threading.Thread(target=self._rag_builder.run, daemon=True).start()
        return None

    def _on_index_ready(self, index: object) -> None:
        self._rag_index = index
        self._rag_building = False
        self._rag_builder = None
        self._rag_status.setText(
            f"知识库：{len(index.sources)} 个文档，{index.num_chunks} 个检索块")

    def _on_add_docs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "添加知识文档", "", "文档 (*.md *.txt *.pdf)")
        for src in files:
            try:
                shutil.copy2(src, _KNOWLEDGE_DIR / Path(src).name)
            except OSError as e:
                self._append_chat("系统", f"复制 {Path(src).name} 失败：{e}")
        if files:
            self._on_rebuild_index()
            logger.log("知识库添加文档", "、".join(Path(f).name for f in files))

    def _on_rebuild_index(self) -> None:
        self._rag_index = None
        self._ensure_rag_index()

    def _on_rag_help(self) -> None:
        from widgets.rag_help_dialog import RAGHelpDialog
        RAGHelpDialog(self).exec()

    # -------- 配置持久化 --------
    def _load_config(self) -> None:
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self._base_url.setText(cfg.get("base_url", AI_DEFAULT_BASE_URL))
            self._api_key.setText(cfg.get("api_key", ""))
            self._model.setText(cfg.get("model", AI_DEFAULT_MODEL))
            self._apply_config()
            self._config_status.setText("已从文件加载")
        except FileNotFoundError:
            pass
        except Exception as e:
            self._config_status.setText(f"加载失败：{e}")

    def _save_config_file(self, url: str, key: str, model: str) -> None:
        cfg = {"base_url": url, "api_key": key, "model": model}
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _apply_config(self) -> None:
        url = _normalize_url(self._base_url.text().strip())
        key = self._api_key.text().strip()
        model = self._model.text().strip()
        self._client = AIClient(url, key, model)

    # -------- slots --------
    def _on_save_config(self) -> None:
        url = self._base_url.text().strip()
        key = self._api_key.text().strip()
        model = self._model.text().strip()
        if not url or not model:
            self._config_status.setText("Base URL 和模型名不能为空")
            return
        self._apply_config()
        try:
            self._save_config_file(url, key, model)
            self._config_status.setText(f"已保存到文件（模型：{model}）")
            logger.log("保存AI配置", f"模型={model}")
        except Exception as e:
            self._config_status.setText(f"内存已保存，文件写入失败：{e}")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        snap = (
            f"实际转速={frame.speed_actual:.1f} rpm  给定转速={frame.speed_target:.1f} rpm\n"
            f"实际电流={frame.current_actual:.2f} A   给定电流={frame.current_target:.2f} A\n"
            f"实际转矩={frame.torque_actual:.2f} Nm  给定转矩={frame.torque_target:.2f} Nm\n"
            f"实际角度={frame.angle_actual:.1f}°  温度={frame.temperature:.1f}°C"
        )
        self._snapshot_label.setPlainText(snap)

    def _on_add_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        for p in paths:
            mime = _IMAGE_MIME.get(os.path.splitext(p)[1].lower())
            if mime is None:
                continue
            with open(p, "rb") as f:
                self._attachments.append((os.path.basename(p), mime, f.read()))
        self._update_attach_label()

    def _on_analyze_waveform(self) -> None:
        """截取监控页波形图并立即发送给 AI 分析。"""
        if self._monitor is None:
            self._append_chat("系统", "监控页面不可用，无法截取波形。")
            return
        png = self._monitor.render_waveforms_png()
        if not png:
            self._append_chat("系统", "暂无波形数据，请先启动仿真或连接通信。")
            return
        self._attachments.append(("波形截图.png", "image/png", png))
        self._update_attach_label()
        if not self._input.text().strip():
            self._input.setText(_WAVEFORM_QUESTION)
        self._on_send()

    def _clear_attachments(self) -> None:
        self._attachments.clear()
        self._update_attach_label()

    def _update_attach_label(self) -> None:
        if self._attachments:
            names = "、".join(n for n, _, _ in self._attachments)
            self._attach_label.setText(f"已附 {len(self._attachments)} 张图片：{names}")
        else:
            self._attach_label.setText("")

    def _on_send(self) -> None:
        question = self._input.text().strip()
        if not question:
            return
        if not hasattr(self, "_client"):
            self._append_chat("系统", "请先填写 API 配置并点击「保存配置」。")
            return

        attachments = self._attachments
        self._attachments = []
        self._update_attach_label()

        suffix = f"（附 {len(attachments)} 张图片）" if attachments else ""
        self._append_chat("用户", question + suffix)
        self._input.clear()
        self._btn_send.setEnabled(False)
        self._btn_send.setText("等待回复…")
        logger.log("AI分析提问", (question + suffix)[:80])

        if attachments:
            user_content = [{"type": "text", "text": question}]
            for _name, mime, data in attachments:
                b64 = base64.b64encode(data).decode("ascii")
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
        else:
            user_content = question

        snapshot = self._snapshot_label.toPlainText()
        sys_prompt = _SYS_PROMPT.format(snapshot=snapshot)
        if self._chk_rag.isChecked():
            hits = []
            try:
                index = self._ensure_rag_index()
                if index is None:
                    self._append_chat(
                        "系统", "知识库索引正在后台构建（大文件 OCR 较慢，"
                        "进度见下方状态栏），本次回答未使用检索。")
                else:
                    hits = index.search(question, top_k=4)
            except Exception as e:
                self._append_chat("系统", f"知识库检索失败（不影响回答）：{e}")
            if hits:
                sys_prompt = _SYS_PROMPT_RAG.format(
                    snapshot=snapshot, context=format_context(hits))
                srcs = "、".join(src for _s, src, _t in hits)
                self._append_chat("系统", f"📚 检索到 {len(hits)} 条参考：{srcs}")
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ]

        self._stream_started = False
        self._worker = _Worker(self._client, messages)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished.connect(self._on_stream_done)
        self._worker.error.connect(self._on_stream_error)
        threading.Thread(target=self._worker.run, daemon=True).start()

    # -------- 流式渲染 --------
    def _insert_at_end(self, text: str) -> None:
        cursor = self._chat_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._chat_display.setTextCursor(cursor)
        self._chat_display.ensureCursorVisible()

    def _on_chunk(self, delta: str) -> None:
        if not self._stream_started:
            self._stream_started = True
            self._btn_send.setText("生成中…")
            self._insert_at_end("【AI】")
        self._insert_at_end(delta)

    def _on_stream_done(self, full: str) -> None:
        if not self._stream_started:
            # 服务端未按流式返回增量（或空回答），整段补上
            self._append_chat("AI", full or "[空回答]")
        else:
            self._insert_at_end("\n\n")
        self._btn_send.setEnabled(True)
        self._btn_send.setText("发送")
        logger.log("AI分析回复", full[:80])
        self._worker = None

    def _on_stream_error(self, err: str) -> None:
        if self._stream_started:
            self._insert_at_end(f"\n[错误] {err}\n\n")
        else:
            self._append_chat("AI", f"[错误] {err}")
        self._btn_send.setEnabled(True)
        self._btn_send.setText("发送")
        logger.log("AI分析失败", err[:120])
        self._worker = None

    def _append_chat(self, role: str, text: str) -> None:
        self._chat_display.appendPlainText(f"【{role}】{text}\n")
