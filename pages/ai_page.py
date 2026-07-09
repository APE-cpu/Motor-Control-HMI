"""AI 分析页面：接入外部大模型，分析电机运行状态与故障诊断。"""
import base64
import json
import os
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ai.ai_client import AIClient
from communications.comm_manager import CommManager, TelemetryFrame
from config.config import AI_DEFAULT_BASE_URL, AI_DEFAULT_MODEL, AI_REQUEST_TIMEOUT
from logs.operation_logger import logger
from runtime_paths import writable_path

_CONFIG_FILE = writable_path("config", "ai_config.json")

_SYS_PROMPT = """你是一名电机控制专家。当前电机遥测数据如下：
{snapshot}
请根据以上数据回答用户的问题，重点关注异常状态和故障诊断。回答简洁专业。"""


def _normalize_url(url: str) -> str:
    """确保 base_url 以 /v1 结尾。"""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


class _Worker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, client: AIClient, messages: list) -> None:
        super().__init__()
        self._client = client
        self._messages = messages

    def run(self) -> None:
        try:
            reply = self._client.chat(self._messages, timeout=AI_REQUEST_TIMEOUT)
            self.finished.emit(reply)
        except Exception as exc:
            self.error.emit(str(exc))


_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".bmp": "image/bmp", ".webp": "image/webp"}

_WAVEFORM_QUESTION = "请分析这张波形截图，判断电机运行状态是否正常，有无异常或故障迹象。"


class AIPage(QWidget):
    def __init__(self, comm: CommManager, monitor_page=None) -> None:
        super().__init__()
        self._comm = comm
        self._monitor = monitor_page
        self._latest: TelemetryFrame = TelemetryFrame()
        self._worker = None  # 持有引用，防止 GC
        self._attachments: list = []  # [(文件名, mime, 原始字节)]

        root = QVBoxLayout(self)

        title = QLabel("AI 电机分析")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        root.addWidget(self._build_config_box())
        root.addWidget(self._build_snapshot_box())
        root.addWidget(self._build_chat_box(), 1)

        comm.telemetryReceived.connect(self._on_telemetry)
        self._load_config()

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
        return box

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
        messages = [
            {"role": "system", "content": _SYS_PROMPT.format(snapshot=snapshot)},
            {"role": "user", "content": user_content},
        ]

        self._worker = _Worker(self._client, messages)
        self._worker.finished.connect(lambda r: self._on_reply(r))
        self._worker.error.connect(lambda e: self._on_reply(f"[错误] {e}"))
        threading.Thread(target=self._worker.run, daemon=True).start()

    def _on_reply(self, text: str) -> None:
        self._append_chat("AI", text)
        self._btn_send.setEnabled(True)
        self._btn_send.setText("发送")
        if not text.startswith("[错误]"):
            logger.log("AI分析回复", text[:80])
        else:
            logger.log("AI分析失败", text)
        self._worker = None

    def _append_chat(self, role: str, text: str) -> None:
        self._chat_display.appendPlainText(f"【{role}】{text}\n")
