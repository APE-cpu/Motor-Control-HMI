"""AI 分析页面：接入外部大模型，分析电机运行状态与故障诊断。

支持 RAG（检索增强生成）：本地 BM25 检索项目文档与 knowledge/ 目录资料，
把相关片段注入提示词，让回答有据可依。
"""
import base64
import json
import os
import shutil
import threading
import statistics
from urllib.parse import urlsplit
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ai.ai_client import AIClient
from ai.rag import RAGIndex, format_context
from communications.comm_manager import CommManager, TelemetryFrame
from config.config import AI_DEFAULT_BASE_URL, AI_DEFAULT_MODEL, AI_REQUEST_TIMEOUT
from logs.operation_logger import logger
from runtime_paths import resource_path, writable_path

_CONFIG_FILE = writable_path("config", "ai_config.json")
_KNOWLEDGE_DIR = writable_path("knowledge", ".keep").parent
_REPORT_DIR = writable_path("reports", ".keep").parent

_AI_PRESETS = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"},
    "Kimi": {
        "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2.6"},
    "Qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-plus"},
    "GLM-5.2": {
        "base_url": "https://api.z.ai/api/paas/v4", "model": "GLM-5.2"},
    "自定义": {"base_url": AI_DEFAULT_BASE_URL, "model": AI_DEFAULT_MODEL},
}

# 随包自带的知识文档（项目根目录）
_BUILTIN_DOCS = ["README.md", "使用说明书.md", "软件介绍.md"]

_SYS_PROMPT = """你是一名电机控制专家。当前电机遥测数据如下：
{snapshot}
若用户提供了带目标值和样本数的历史诊断窗口，该窗口即使在停机后生成仍然有效；
不要因当前快照已停机而否定历史窗口。必须区分“当前状态”和“采样窗口”。
请根据以上数据回答用户的问题，重点关注异常状态和故障诊断。回答简洁专业。"""

_SYS_PROMPT_RAG = """你是一名电机控制专家。当前电机遥测数据如下：
{snapshot}

若用户提供了带目标值和样本数的历史诊断窗口，该窗口即使在停机后生成仍然有效；
不要因当前快照已停机而否定历史窗口。必须区分“当前状态”和“采样窗口”。

以下是从本地知识库检索到的参考资料。回答时优先依据资料内容，引用处标注来源
（如“据资料1”）；资料未覆盖的部分再用你的通用知识并注明：
{context}

请根据以上信息回答用户的问题，重点关注异常状态和故障诊断。回答简洁专业。"""


def _normalize_url(url: str) -> str:
    """补全只有主机名的地址，同时保留服务商明确给出的版本路径。"""
    url = url.rstrip("/")
    if url and urlsplit(url).path in ("", "/"):
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
        self._history = deque(maxlen=300)
        self._high_history = deque(maxlen=5000)
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
        comm.highRateTelemetryReceived.connect(self._on_high_rate)
        comm.highRateTelemetryBatchReceived.connect(self._on_high_rate_batch)
        self._load_config()
        # 启动即后台构建索引：有缓存时 <1s 就绪，首个问题不再错过检索
        self._ensure_rag_index()

    # -------- 子构件 --------
    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("API 配置")
        f = QFormLayout(box)
        self._profile = QComboBox()
        self._profile.addItems(_AI_PRESETS)
        self._base_url = QLineEdit(AI_DEFAULT_BASE_URL)
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.Password)
        self._api_key.setPlaceholderText("sk-...")
        self._model = QLineEdit(AI_DEFAULT_MODEL)
        self._profile.currentTextChanged.connect(self._on_profile_changed)
        btn_save = QPushButton("保存配置")
        btn_save.clicked.connect(self._on_save_config)
        f.addRow("已保存模型", self._profile)
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
        self._btn_pi = QPushButton("PI调参建议")
        self._btn_pi.setToolTip("分析最近运行窗口的转速误差、超调和Iq振荡，并生成可执行调参建议")
        self._btn_pi.clicked.connect(self._on_pi_tuning)
        self._attach_label = QLabel("")
        self._attach_label.setStyleSheet("color: #90a4ae;")
        btn_attach_clear = QPushButton("移除图片")
        btn_attach_clear.clicked.connect(self._clear_attachments)
        h2.addWidget(btn_img)
        h2.addWidget(self._btn_wave)
        h2.addWidget(self._btn_pi)
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
        paths = [resource_path(name) for name in _BUILTIN_DOCS]
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
    def _on_profile_changed(self, name: str) -> None:
        profiles = getattr(self, "_saved_profiles", {})
        cfg = profiles.get(name) or _AI_PRESETS.get(name, {})
        self._base_url.setText(cfg.get("base_url", AI_DEFAULT_BASE_URL))
        self._api_key.setText(cfg.get("api_key", ""))
        self._model.setText(cfg.get("model", AI_DEFAULT_MODEL))
        self._apply_config()
        key_state = "密钥已保存" if cfg.get("api_key") else "请填写该模型的密钥"
        self._config_status.setText(f"已选择 {name}（{key_state}）")

    def _load_config(self) -> None:
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self._saved_profiles = dict(cfg.get("profiles", {}))
            if not self._saved_profiles and (cfg.get("base_url") or cfg.get("model")):
                # 兼容旧版单模型配置；当前实验室配置默认归入 DeepSeek。
                self._saved_profiles["DeepSeek"] = {
                    "base_url": cfg.get("base_url", _AI_PRESETS["DeepSeek"]["base_url"]),
                    "api_key": cfg.get("api_key", ""),
                    "model": cfg.get("model", _AI_PRESETS["DeepSeek"]["model"]),
                }
            selected = cfg.get("selected_profile", "DeepSeek")
            index = self._profile.findText(selected)
            self._profile.setCurrentIndex(index if index >= 0 else 0)
            self._on_profile_changed(self._profile.currentText())
        except FileNotFoundError:
            pass
        except Exception as e:
            self._config_status.setText(f"加载失败：{e}")

    def _save_config_file(self, url: str, key: str, model: str) -> None:
        name = self._profile.currentText()
        self._saved_profiles = getattr(self, "_saved_profiles", {})
        self._saved_profiles[name] = {
            "base_url": url, "api_key": key, "model": model}
        cfg = {"selected_profile": name, "profiles": self._saved_profiles}
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
            self._config_status.setText(
                f"已保存 {self._profile.currentText()}（模型：{model}，密钥已保存）")
            logger.log("保存AI配置", f"模型={model}")
        except Exception as e:
            self._config_status.setText(f"内存已保存，文件写入失败：{e}")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._latest = frame
        if abs(frame.speed_target) > 1.0:
            self._history.append((
                float(frame.speed_actual), float(frame.speed_target),
                float(frame.current_actual), float(frame.current_target)))
        snap = (
            f"实际转速={frame.speed_actual:.1f} rpm  给定转速={frame.speed_target:.1f} rpm\n"
            f"实际电流={frame.current_actual:.2f} A   给定电流={frame.current_target:.2f} A\n"
            f"实际转矩={frame.torque_actual:.2f} Nm  给定转矩={frame.torque_target:.2f} Nm\n"
            f"实际角度={frame.angle_actual:.1f}°  温度={frame.temperature:.1f}°C\n"
            f"速度PI：Kp={frame.speed_kp} Ki={frame.speed_ki}  "
            f"Iq限流={frame.current_limit_a:.3f} A"
        )
        self._snapshot_label.setPlainText(snap)

    def _on_high_rate(self, sample: dict) -> None:
        target = float(self._latest.speed_target)
        # 把采样时的目标冻结进样本；停机后分析不能使用已归零的最新目标。
        if abs(target) > 1.0:
            frozen = dict(sample)
            frozen["target_rpm"] = target
            self._high_history.append(frozen)

    def _on_high_rate_batch(self, samples: list[dict]) -> None:
        for sample in samples:
            self._on_high_rate(sample)

    def _pi_report(self) -> str:
        if len(self._high_history) >= 400:
            rate_hz = int(self._high_history[-1].get("rate_hz", 200))
            window_size = min(len(self._high_history), rate_hz * 10)
            hs = list(self._high_history)[-window_size:]
            samples = [(x["speed_rpm"], x["target_rpm"], x["iq_a"], x["iqref_a"])
                       for x in hs]
            source = f"{rate_hz}Hz高速诊断通道"
        else:
            samples = list(self._history)
            source = "10Hz常规遥测"
        if len(samples) < 20:
            return "有效运行数据不足20帧，请稳定运行至少3秒后再分析。"
        # 先保留最近窗口，再自动切出进入目标带后的稳态段。
        samples = samples[-rate_hz * 10:] if "高速诊断" in source else samples[-100:]
        raw_samples = samples
        rate_hz = rate_hz if "高速诊断" in source else 10
        final_target = raw_samples[-1][1]
        # 只分析最后一次相同目标的连续区段，避免阶跃前数据污染。
        target_start = 0
        for index in range(len(raw_samples) - 1, -1, -1):
            if abs(raw_samples[index][1] - final_target) > 0.5:
                target_start = index + 1
                break
        target_segment = raw_samples[target_start:]
        settle_required = max(5, int(rate_hz * 0.5))
        settle_index = None
        band = max(abs(final_target) * 0.10, 20.0)
        for index in range(0, len(target_segment) - settle_required + 1):
            window = target_segment[index:index + settle_required]
            if all(abs(speed - target) <= band for speed, target, _, _ in window):
                settle_index = index
                break
        if settle_index is not None:
            samples = target_segment[settle_index:]
            settle_text = f"进入目标±10%用时约{settle_index / rate_hz:.2f}s"
        else:
            samples = target_segment[-max(20, rate_hz * 3):]
            settle_text = "未检测到连续0.5s进入目标±10%，以下为末段统计"
        speeds = [x[0] for x in samples]
        targets = [x[1] for x in samples]
        iqs = [x[2] for x in samples]
        iqrefs = [x[3] for x in samples]
        target = statistics.mean(targets)
        if abs(target) <= 1.0:
            return "目标转速无效，无法进行PI分析；请重新运行并采集数据。"
        errors = [t - s for s, t, _, _ in samples]
        mae = statistics.mean(abs(x) for x in errors)
        speed_std = statistics.pstdev(speeds)
        iq_std = statistics.pstdev(iqs)
        overshoot = max(0.0, (max(speeds) - target) / max(abs(target), 1.0) * 100.0)
        reversals = sum(1 for a, b in zip(iqs, iqs[1:]) if a * b < 0)
        reversal_rate = reversals / max(len(iqs) - 1, 1) * 100.0
        iqref_std = statistics.pstdev(iqrefs)
        ref_reversals = sum(1 for a, b in zip(iqrefs, iqrefs[1:]) if a * b < 0)
        ref_reversal_rate = ref_reversals / max(len(iqrefs) - 1, 1) * 100.0
        kp, ki = int(self._latest.speed_kp), int(self._latest.speed_ki)
        advice = []
        if speed_std > max(0.02 * abs(target), 15.0) or overshoot > 10.0:
            advice.append("速度存在明显振荡/超调：Kp先降低10%～20%，Ki降低20%～35%。")
        elif mae > 0.05 * abs(target):
            advice.append("稳态误差偏大且未明显振荡：Ki可提高10%～15%。")
        else:
            advice.append("转速跟踪已较稳定，不建议大幅修改Kp。")
        change_kp = speed_std > max(0.02 * abs(target), 15.0) or overshoot > 10.0
        change_ki = ref_reversal_rate > 10.0 or iqref_std > 0.15
        if change_ki:
            advice.append("Iqref本身波动明显：优先降低速度Ki约20%，并检查速度反馈滤波；不要先提高限流。")
        elif reversal_rate > 10.0 or iq_std > 0.25:
            advice.append("实际Iq波动但Iqref相对平稳：先检查电流采样/电流环，不应仅凭实际Iq降低速度PI。")
        if kp > 0 and ki > 0:
            next_kp = max(1, round(kp * 0.9)) if change_kp else kp
            next_ki = max(1, round(ki * 0.8)) if change_ki else ki
            advice.append(
                f"建议首轮试值：Kp≈{next_kp}，Ki≈{next_ki}；"
                "只修改被诊断为异常的参数并复测。")
        return (
            f"采集窗口：{len(raw_samples)}帧；稳态分析：{len(samples)}帧（{source}，{settle_text}）\n"
            f"目标均值={target:.1f} rpm，速度均值={statistics.mean(speeds):.1f} rpm，"
            f"MAE={mae:.1f} rpm，速度标准差={speed_std:.1f} rpm，超调={overshoot:.1f}%\n"
            f"Iq均值={statistics.mean(iqs):.3f} A，Iq标准差={iq_std:.3f} A，"
            f"正负切换率={reversal_rate:.1f}%\n"
            f"Iqref均值={statistics.mean(iqrefs):.3f} A，Iqref标准差={iqref_std:.3f} A，"
            f"Iqref正负切换率={ref_reversal_rate:.1f}%\n"
            f"下位机速度PI：Kp={kp}，Ki={ki}\n" + "\n".join(advice))

    def _on_pi_tuning(self) -> None:
        report = self._pi_report()
        self._append_chat("PI诊断", report)
        if report.startswith("有效运行数据不足"):
            return
        self._input.setText(
            "以下是本机统计的PI诊断：\n" + report + "\n"
            "请结合当前电机状态，判断速度环Kp/Ki应如何调整。"
            "请给出调整顺序、下一组具体数值、风险和复测判据；不要建议一次大幅修改。")
        if hasattr(self, "_client"):
            self._on_send()

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
