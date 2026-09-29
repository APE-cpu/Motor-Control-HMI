"""把命令面板、任务卡说明书和新手引导接到主窗口上。

主窗口只需要在构造末尾调用一次 install_assist_features(self)：
  Ctrl+K  打开命令面板
  F1      打开当前页面对应的说明书卡片
说明书页面的"在界面中定位""带我做"也在这里转成引导层的步骤。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, QSettings, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut

from ai.diagnostic_tools import create_read_only_registry
from ai.harness import (
    AuditLog, DiagnosticTelemetryStore, ToolContext, ToolExecutor,
)
from core.commands import CommandRegistry, build_diagnostic_commands, build_ui_commands
from core.control_locator import (
    PAGE_BY_KEY, current_page_key, page_for_control, resolve_control, show_page,
)
from widgets.command_palette import CommandPalette
from widgets.guide_overlay import GuideOverlay, GuideStep

OVERVIEW_STEPS = (
    GuideStep("左侧导航", "页面按用途分成四组：运行控制、分析可视化、AI 智能、系统。",
              "main.nav"),
    GuideStep("外观栏", "顶部可以切换主题与字体，不影响任何控制参数。", "main.appearance"),
    GuideStep("先连接设备", "第一次使用请到“通信设置”选择链路并点“连接”。"
              "真机默认使用 negotiated-v2 安全模式，握手成功才算连接。",
              "communication.connect"),
    GuideStep("运行控制", "启动、停止、紧急停止都在监控页面的这一行。"
              "紧急停止会立即封波并锁定故障；这里只是告诉你位置，不需要现在点击。",
              "monitor.emergency"),
    GuideStep("状态栏", "底部显示通信状态；断开、超时和握手失败都会写在这里。",
              "main.statusbar"),
    GuideStep("实验管理", "做实验先到这里点“开始记录实验”。四个标签：记录与数据、设备与方案、"
              "音视频（开始实验时自动录像，结束后生成带日期、转速、电流和关键事件的字幕）、实验日志。",
              "experiment.tabs"),
    GuideStep("实验日志", "实验结束后在这里一键生成固定模板的日志（HTML + PDF）：频谱与时频、阶跃响应、"
              "矢量分解动图、功率流、录像都在里面，每个数值可追溯到源文件。"
              "生成在后台进行，期间可以继续操作；也支持改参数前后的对比实验。",
              "experiment.log_generate"),
    GuideStep("矢量可视化", "三个标签：实时轨迹；畸变图谱（零偏、三相不对称等把电流圆拉成什么样）；"
              "分量合成——用保存的高速数据离线回放各次分量首尾相接的旋转矢量，可指定时间段。",
              "vector.tabs"),
    GuideStep("功率流离线回放", "勾选“离线回放”，打开保存的高速数据，就能按时间回放桑基功率流和能量平衡。",
              "power_flow.offline"),
    GuideStep("离线傅里叶", "除 FFT 外还有 STFT 时频、小波能量、阶次分析和动态响应（上升、超调、调节时间）子页。",
              "fourier.tabs"),
    GuideStep("数字孪生 · 算法验证", "用保存的实测数据离线回放 ESO / RLS 算法，和实测结果对照，改算法前先在这里验证。",
              "twin.validation"),
    GuideStep("随时求助", "任何页面按 Ctrl+K 打开命令面板（查状态、做 FFT、跳转页面），"
              "按 F1 打开当前页面对应的说明书卡片。"),
)
_SETTINGS = ("MotorControlHMI", "assist")
_FIRST_RUN_KEY = "guide/overview_done"


def _fallback_executor(window) -> ToolExecutor:
    """诊断助手页不可用时自建一套只读执行器（仍经过同一套校验与审计）。"""
    store = DiagnosticTelemetryStore(max_samples=5000)
    comm = window.comm_manager
    comm.highRateTelemetryColumnsReceived.connect(store.feed_columns)
    comm.highRateTelemetryReceived.connect(store.feed_sample)
    context = ToolContext(comm=comm, telemetry_store=store)
    return ToolExecutor(create_read_only_registry(), context, audit_log=AuditLog())


class AssistFeatures(QObject):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.manual = window.manual_page
        self.guide = GuideOverlay(window, self._resolve, self.show_page)

        executor = getattr(getattr(window, "ai_page", None), "_tool_executor", None)
        self.registry = CommandRegistry()
        for command in build_diagnostic_commands(executor or _fallback_executor(window)):
            self.registry.register(command)
        for command in build_ui_commands(
                navigate=self.show_page, open_manual=self.open_manual,
                start_guide=self.start_guide, stop_motor=self.stop_motor):
            self.registry.register(command)

        self.palette = CommandPalette(
            window, self.registry, lambda: self.manual.cards,
            self.show_page, self.open_card, self.ask_ai)

        palette_key = QShortcut(QKeySequence("Ctrl+K"), window)
        palette_key.setContext(Qt.WindowShortcut)
        palette_key.activated.connect(lambda: self.palette.show_palette())
        help_key = QShortcut(QKeySequence(Qt.Key_F1), window)
        help_key.setContext(Qt.WindowShortcut)
        help_key.activated.connect(self.context_help)
        self._shortcuts = (palette_key, help_key)

        self.manual.locateRequested.connect(self.locate)
        self.manual.guideRequested.connect(
            lambda card_id: self.start_guide(card_id or None))
        self.manual.pageRequested.connect(self.show_page)

    # ------------------------------------------------------------ 定位
    def _resolve(self, control_id: str):
        page = page_for_control(control_id)
        return resolve_control(self.window, control_id), page.key if page else None

    def show_page(self, page_key: str) -> bool:
        return show_page(self.window, page_key)

    def locate(self, control_id: str) -> None:
        text = "就是这里。"
        card = self.manual.current_card
        if card is not None:
            for step in card.steps:
                if step.control_id == control_id:
                    text = step.text
                    break
        self.guide.start([GuideStep("在界面中定位", text, control_id)])

    # ------------------------------------------------------------ 说明书与引导
    def open_card(self, card_id: str) -> None:
        self.show_page("manual")
        self.manual.open_card(card_id)

    def open_manual(self, query: str) -> None:
        self.show_page("manual")
        query = query.strip()
        if query and self.manual.card(query) is not None:
            self.manual.open_card(query)
        elif query:
            self.manual.search(query)
        self.manual.focus_search()

    def context_help(self) -> None:
        key = current_page_key(self.window)
        cards = self.manual.cards_for_page(key) if key else []
        self.show_page("manual")
        if cards:
            self.manual.open_card(cards[0].id)
        elif key in PAGE_BY_KEY and key != "manual":
            self.manual.search(PAGE_BY_KEY[key].title)

    def start_guide(self, card_id: str | None) -> str | None:
        """返回错误信息；None 表示已开始。"""
        if not card_id:
            self.guide.start(list(OVERVIEW_STEPS))
            return None
        card = self.manual.card(card_id)
        if card is None:
            return f"没有 id 为 {card_id} 的卡片"
        if not card.steps:
            return f"卡片“{card.title}”没有可引导的步骤"
        total = len(card.steps)
        self.guide.start([
            GuideStep(f"{card.title}（{index}/{total}）", step.text,
                      step.control_id, step.dangerous)
            for index, step in enumerate(card.steps, 1)])
        return None

    def maybe_start_first_run_tour(self) -> None:
        if os.environ.get("HMI_NO_TOUR"):
            return
        settings = QSettings(*_SETTINGS)
        if settings.value(_FIRST_RUN_KEY, False, type=bool):
            return
        settings.setValue(_FIRST_RUN_KEY, True)
        QTimer.singleShot(900, lambda: self.start_guide(None))

    # ------------------------------------------------------------ 其他动作
    def ask_ai(self, text: str) -> None:
        """打开诊断助手并填入问题；不自动发送，联网请求由用户确认。"""
        if not self.show_page("ai"):
            return
        box = getattr(self.window.ai_page, "_input", None)
        if box is not None:
            box.setText(text)
            box.setFocus()

    def stop_motor(self) -> None:
        """与监控页“停止”按钮完全相同的调用。"""
        self.window.monitor_page._on_stop()


def install_assist_features(window) -> AssistFeatures:
    window.assist = AssistFeatures(window)
    return window.assist
