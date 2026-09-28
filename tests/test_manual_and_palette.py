import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("HMI_NO_TOUR", "1")

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget

from ai.diagnostic_tools import create_read_only_registry
from ai.harness import AuditLog, DiagnosticTelemetryStore, ToolContext, ToolExecutor
from core.commands import (
    CommandError, CommandRegistry, build_diagnostic_commands, build_ui_commands,
    parse_channel, parse_duration,
)
from core.control_locator import CONTROLS, PAGES, find_pages
from core.manual_cards import (
    GROUP_ORDER, REFERENCE_GROUP, load_cards, parse_card, reference_cards, search_cards,
)
from runtime_paths import resource_path

CARD_TEXT = """# 示例任务 {#demo}
> 分组：排障 · 页面：monitor · 耗时：约 1 分钟 · 风险：会通电
> 关键词：零偏, 偏置, offset

一段简介。

## 步骤
1. 先看状态
2. 点启动 {控件: monitor.start, 危险}
3. 点停止 {控件: monitor.stop}

## 预期
一切正常。

## 失败时
见 [[other]]。

## 原理与公式
公式在这里。
"""


def _app():
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------ 卡片解析
def test_card_parser_reads_meta_steps_and_sections():
    card = parse_card(CARD_TEXT)
    assert (card.id, card.title, card.group, card.page) == ("demo", "示例任务", "排障", "monitor")
    assert card.duration == "约 1 分钟" and card.risk == "会通电"
    assert card.keywords == ("零偏", "偏置", "offset")
    assert card.summary == "一段简介。"
    assert [s.text for s in card.steps] == ["先看状态", "点启动", "点停止"]
    assert card.steps[1].control_id == "monitor.start" and card.steps[1].dangerous
    assert card.steps[2].control_id == "monitor.stop" and not card.steps[2].dangerous
    assert card.controls == ["monitor.start", "monitor.stop"]
    assert set(card.sections) == {"预期", "失败时", "原理与公式"}


def test_reference_cards_split_legacy_manual_by_h2():
    cards = reference_cards("# 标题\n\n## 一、连接\n正文A\n### 小节\n正文B\n## 二、运行\n正文C\n")
    assert [c.title for c in cards] == ["一、连接", "二、运行"]
    assert "正文B" in cards[0].summary and all(c.group == REFERENCE_GROUP for c in cards)


def test_search_requires_all_tokens_and_ranks_title_first():
    cards = [parse_card(CARD_TEXT),
             parse_card("# 零偏标定 {#zero}\n> 分组：常用任务\n\n## 步骤\n1. 做事\n")]
    hits = search_cards(cards, "零偏")
    assert [h.card.id for h in hits] == ["zero", "demo"]      # 标题命中优先
    assert search_cards(cards, "零偏 不存在的词") == []


def test_shipped_cards_are_consistent():
    cards = load_cards(resource_path("manual", "cards"), resource_path("使用说明书.md"))
    by_id = {card.id: card for card in cards}
    task_cards = [c for c in cards if c.group != REFERENCE_GROUP]
    assert len(task_cards) >= 9
    assert {c.group for c in task_cards} <= set(GROUP_ORDER)
    for card in task_cards:
        assert card.steps, f"{card.id} 没有步骤"
        for control in card.controls:
            assert control in CONTROLS, f"{card.id} 引用了未登记的控件 {control}"
        text = card.search_text()
        for target in re.findall(r"\[\[([A-Za-z0-9_\-]+)\]\]", text):
            assert target in by_id, f"{card.id} 链接到不存在的卡片 {target}"
        if card.page:
            assert card.page in {p.key for p in PAGES}
    # 旧说明书内容全部保留为参考卡，可被搜索到
    assert any("数字孪生实验标准流程" in c.title for c in cards)
    # 会让电机通电的步骤必须标成需要亲手操作
    starts = [s for c in task_cards for s in c.steps if s.control_id == "monitor.start"]
    assert starts and all(s.dangerous for s in starts)


# ------------------------------------------------------------------ 命令解析
def test_channel_and_duration_parsing():
    assert parse_channel("IQ") == "iq_a" and parse_channel("转速") == "speed_rpm"
    with pytest.raises(CommandError):
        parse_channel("xyz")
    assert parse_duration("2s") == 2.0 and parse_duration("500ms") == 0.5
    assert parse_duration("3") == 3.0 and parse_duration("abc") is None


def _ui_registry(calls):
    registry = CommandRegistry()
    for command in build_ui_commands(
            navigate=lambda key: calls.append(("nav", key)) or True,
            open_manual=lambda q: calls.append(("help", q)),
            start_guide=lambda card: calls.append(("guide", card)),
            stop_motor=lambda: calls.append(("stop",))):
        registry.register(command)
    return registry


def test_ui_commands_parse_and_run():
    calls = []
    registry = _ui_registry(calls)
    command, args = registry.parse("goto 矢量")
    assert command.name == "goto"
    command.run(command.parse(args))
    assert calls[-1] == ("nav", "vector")
    command, args = registry.parse("停机")
    result = command.run(command.parse(args))
    assert calls[-1] == ("stop",) and result.ok and "紧急" in result.lines[1]
    assert find_pages("监控")[0].key == "monitor"


def test_suggestions_mark_invalid_arguments_and_offer_ai():
    calls = []
    registry = _ui_registry(calls)
    first = registry.suggest("goto 不存在的页面")[0]
    assert first.kind == "command" and not first.runnable
    kinds = [s.kind for s in registry.suggest("辨识")]
    assert kinds[-1] == "ai"
    with pytest.raises(ValueError):
        registry.register(registry.get("goto"))       # 重复注册被拒绝


class _FakeFrame:
    speed_actual = 800.0
    speed_target = 800.0
    current_actual = 2.5
    current_target = 2.5
    vdc = 24.0
    temperature = 30.0
    data_source = "real"
    mc_state = 6
    fault_code = 0
    fault_history_code = 0


class _FakeComm:
    def latest_frame(self):
        return _FakeFrame()

    def is_connected(self):
        return True

    def is_sim_running(self):
        return False

    def telemetry_age_s(self):
        return 0.05

    def protocol_status(self):
        return {"mode": "negotiated-v2"}

    def telemetry_config(self):
        return {}


def test_diagnostic_commands_go_through_harness_executor():
    store = DiagnosticTelemetryStore(max_samples=20000)
    fs, n = 16000, 16000
    t = np.arange(n) / fs
    store.feed_columns({"count": n, "rate_hz": fs,
                        "iq_a": list(2.5 + 0.2 * np.sin(2 * np.pi * 160.0 * t))})
    audit = AuditLog()
    executor = ToolExecutor(create_read_only_registry(),
                            ToolContext(comm=_FakeComm(), telemetry_store=store),
                            audit_log=audit)
    registry = CommandRegistry()
    for command in build_diagnostic_commands(executor):
        registry.register(command)

    command, args = registry.parse("fft iq 1s")
    result = command.run(command.parse(args))
    assert result.ok, result.error
    peaks = [line for line in result.lines if "Hz" in line and "fe" in line]
    assert "160.0 Hz" in peaks[1] and "3.00 fe" in peaks[1]   # 800 rpm × 4 对极 → fe = 53.3 Hz
    # 截取、FFT 以及读取转速都记录在 harness 审计日志里
    tools = [record["tool"] for record in audit.records]
    assert {"capture_signal_window", "analyze_fft", "get_runtime_state"} <= set(tools)

    command, _ = registry.parse("state")
    assert "已连接" in command.run({}).lines[0]
    with pytest.raises(CommandError):
        registry.get("fft").parse(["60s"])


# ------------------------------------------------------------------ 主窗口集成
@pytest.fixture(scope="module")
def window():
    _app()
    from main_window import MainWindow
    win = MainWindow(enable_training=False)
    win.resize(1280, 800)
    win.show()
    QApplication.processEvents()
    yield win
    win.close()


def test_every_control_id_resolves_in_real_window(window):
    from core.control_locator import resolve_control
    missing = [cid for cid in CONTROLS if not isinstance(
        resolve_control(window, cid), QWidget)]
    assert missing == []


def test_palette_navigates_and_manual_opens_cards(window):
    assist = window.assist
    assist.palette.show_palette()
    assert assist.palette.isVisible()
    assist.palette.run_text("goto 矢量")
    assert window.stack.currentWidget() is window.vector_page
    assert "矢量可视化" in assist.palette.last_output

    assist.context_help()                          # F1：当前在矢量页
    assert window.stack.currentWidget() is window.manual_page
    assert window.manual_page.current_card.id == "vector-roundness"

    assist.open_manual("辨识")
    assert window.manual_page.current_card.id == "rl-identification"
    assert "【需你亲手操作】" in window.manual_page._browser.toMarkdown()
    assist.palette.hide_palette()


def test_guide_follows_card_steps_without_clicking(window):
    assist = window.assist
    clicks = []
    window.monitor_page._btn_start.clicked.connect(lambda: clicks.append("start"))
    assert assist.start_guide("rl-identification") is None
    guide = assist.guide
    assert guide.active and guide.current_step.control_id == "monitor.probe_amplitude"
    seen = []
    for _ in range(10):
        if not guide.active:
            break
        seen.append((guide.current_step.control_id, guide.current_step.dangerous))
        QApplication.processEvents()
        guide.next_step()
    assert not guide.active
    assert ("monitor.start", True) in seen
    assert clicks == []                            # 引导从不替用户点击
    assert assist.start_guide("no-such-card") is not None


def test_palette_opens_at_pointer_stays_inside_and_drags(window):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    palette = window.assist.palette
    palette.show_palette(at=QPoint(300, 260))
    assert palette.pos() == QPoint(260, 240)             # 输入框落在鼠标附近
    palette.show_palette(at=QPoint(window.width() - 5, window.height() - 5))
    geometry = palette.geometry()
    assert window.rect().contains(geometry)                # 靠近右下角也不会出界

    start = palette.pos()
    grip_center = QPointF(palette._grip.rect().center())
    origin = QPointF(palette._grip.mapToGlobal(grip_center.toPoint()))

    def mouse(kind, delta):
        point = origin + QPointF(*delta)
        return QMouseEvent(kind, grip_center, point, point, Qt.LeftButton,
                           Qt.LeftButton, Qt.NoModifier)

    palette._drag(mouse(QEvent.MouseButtonPress, (0, 0)))
    palette._drag(mouse(QEvent.MouseMove, (-120, -80)))
    palette._drag(mouse(QEvent.MouseButtonRelease, (-120, -80)))
    assert palette.pos() == start + QPoint(-120, -80)
    palette.hide_palette()


def test_palette_output_can_be_closed(window):
    palette = window.assist.palette
    palette.show_palette(at=QPoint(200, 150))
    compact = palette.height()
    assert not palette.output_visible
    palette.run_text("goto 矢量")
    assert palette.output_visible and palette.height() > compact
    palette.close_output()
    assert not palette.output_visible
    assert palette.height() == compact and palette.last_output == ""
    palette.hide_palette()


def test_stop_command_uses_same_path_as_stop_button(window, monkeypatch):
    called = []
    monkeypatch.setattr(window.monitor_page, "_on_stop", lambda: called.append(True))
    window.assist.palette.run_text("stop")
    assert called == [True]
    window.assist.palette.hide_palette()
