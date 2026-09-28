"""命令面板的命令层（不依赖 Qt）。

诊断命令一律经由 ai.harness.ToolExecutor 执行，与诊断助手共用同一套参数
校验、只读策略、超时和审计日志；命令面板不会绕过 harness 直接读硬件。

权限分级：
  read_only  读取状态、截取信号、FFT 等，直接执行
  ui         切换页面、打开说明书、启动引导，直接执行
  safety     停机：只允许"让电机更安全"方向的操作，行为与界面停止按钮相同
第一版不提供启动、调速、改参数等会让电机通电或改变运行状态的命令。
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable

from ai.harness import ToolCall, ToolExecutor
from core.control_locator import PAGES, find_pages
from core.manual_cards import Card, search_cards

POLE_PAIRS = 4

# 用户输入的通道别名 → harness 通道名
CHANNEL_ALIASES = {
    "iq": "iq_a", "iqref": "iqref_a", "ia": "ia_a", "ib": "ib_a",
    "speed": "speed_rpm", "转速": "speed_rpm", "angle": "angle_deg",
    "角度": "angle_deg", "vd": "vd_raw", "vq": "vq_raw", "vbus": "vbus_v",
    "母线": "vbus_v",
}
HARNESS_CHANNELS = set(CHANNEL_ALIASES.values())
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|秒)?$", re.IGNORECASE)


class CommandError(ValueError):
    """参数不合法；消息直接显示给用户。"""


@dataclass
class CommandResult:
    ok: bool
    title: str
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Command:
    name: str
    title: str
    permission: str
    usage: str
    description: str
    parse: Callable[[list[str]], dict]
    run: Callable[[dict], CommandResult]
    describe: Callable[[dict], str] = lambda args: ""
    aliases: tuple[str, ...] = ()
    background: bool = False          # True：在工作线程执行，避免阻塞界面


@dataclass(frozen=True)
class Suggestion:
    kind: str          # command / page / card / ai
    title: str
    detail: str
    tag: str
    payload: object = None
    runnable: bool = True


PERMISSION_TAG = {"read_only": "只读", "ui": "界面", "safety": "停机"}


def parse_channel(token: str) -> str:
    name = CHANNEL_ALIASES.get(token.lower(), token.lower())
    if name not in HARNESS_CHANNELS:
        raise CommandError(
            f"不认识的通道 {token}；可用：{', '.join(sorted(CHANNEL_ALIASES))}")
    return name


def parse_duration(token: str) -> float | None:
    match = _DURATION.match(token.strip())
    if not match:
        return None
    value = float(match.group(1))
    if (match.group(2) or "").lower() == "ms":
        value /= 1000.0
    return value


def _split_channels_and_duration(args: list[str], default_channels: list[str],
                                 default_s: float, max_channels: int) -> dict:
    channels, duration = [], None
    for token in args:
        seconds = parse_duration(token)
        if seconds is not None:
            duration = seconds
            continue
        for part in re.split(r"[,，]", token):
            if part:
                channels.append(parse_channel(part))
    channels = channels or list(default_channels)
    if len(channels) > max_channels:
        raise CommandError(f"最多 {max_channels} 个通道")
    duration = default_s if duration is None else duration
    if not 0.1 <= duration <= 30.0:
        raise CommandError("时长必须在 0.1~30 s 之间")
    return {"channels": list(dict.fromkeys(channels)), "duration_s": duration}


def _no_args(args: list[str]) -> dict:
    if args:
        raise CommandError("此命令不需要参数")
    return {}


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._lookup: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        for key in (command.name, *command.aliases):
            key = key.lower()
            if key in self._lookup:
                raise ValueError(f"命令名重复：{key}")
            self._lookup[key] = command
        self._commands[command.name] = command

    @property
    def commands(self) -> list[Command]:
        return list(self._commands.values())

    def get(self, name: str) -> Command | None:
        return self._lookup.get(name.lower())

    def parse(self, text: str) -> tuple[Command | None, list[str]]:
        tokens = text.replace("　", " ").split()
        if not tokens:
            return None, []
        return self.get(tokens[0]), tokens[1:]

    def suggest(self, text: str, cards: list[Card] | None = None,
                limit: int = 8) -> list[Suggestion]:
        text = text.strip()
        out: list[Suggestion] = []
        command, args = self.parse(text)
        if command is not None:
            try:
                parsed = command.parse(args)
                out.append(Suggestion(
                    "command", command.title,
                    command.describe(parsed) or command.usage,
                    PERMISSION_TAG[command.permission], (command, parsed)))
            except CommandError as exc:
                out.append(Suggestion(
                    "command", command.title, f"{command.usage}　— {exc}",
                    PERMISSION_TAG[command.permission], None, runnable=False))
        lowered = text.lower()
        for other in self.commands:
            if other is command:
                continue
            names = (other.name, other.title, *other.aliases)
            if not lowered or any(lowered in n.lower() for n in names):
                out.append(Suggestion(
                    "command", other.title, other.usage,
                    PERMISSION_TAG[other.permission], ("complete", other.name),
                    runnable=False))
        if text:
            for page in find_pages(text)[:3]:
                out.append(Suggestion("page", f"打开页面：{page.title}", "",
                                      "界面", page.key))
            for hit in search_cards(cards or [], text, limit=3):
                out.append(Suggestion("card", f"说明书：{hit.card.title}",
                                      hit.snippet, "帮助", hit.card.id))
            out.append(Suggestion("ai", f"问诊断助手：“{text}”",
                                  "打开诊断助手并填入问题（需手动发送）", "AI", text))
        return out[:limit] if not text else out


# ------------------------------------------------------------------ 诊断命令
def _call(executor: ToolExecutor, name: str, arguments: dict):
    return executor.execute(ToolCall(
        id=f"palette_{uuid.uuid4().hex[:10]}", name=name, arguments=arguments))


def _fail(title: str, result) -> CommandResult:
    return CommandResult(False, title, warnings=list(result.warnings),
                         error=result.error or "执行失败")


def _fmt(value, digits: int = 3) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}g}" if math.isfinite(value) else "—"
    return str(value)


def build_diagnostic_commands(executor: ToolExecutor) -> list[Command]:
    def run_state(_args):
        result = _call(executor, "get_runtime_state", {})
        if not result.ok:
            return _fail("运行状态", result)
        d = result.data
        lines = [
            f"连接：{'已连接' if d['connected'] else '未连接'}"
            f"　仿真：{'运行中' if d['simulation_running'] else '否'}"
            f"　数据源：{d['data_source']}　遥测时延：{_fmt(d['telemetry_age_s'])} s",
            f"转速：{_fmt(d['speed_actual_rpm'], 4)} / 给定 {_fmt(d['speed_target_rpm'], 4)} rpm"
            f"　Iq：{_fmt(d['iq_actual_a'])} / 给定 {_fmt(d['iq_target_a'])} A",
            f"母线：{_fmt(d['vbus_v'])} V　温度：{_fmt(d['temperature_c'])} °C"
            f"　MC 状态：{d['mc_state']}",
            f"故障：{d['fault_text'] or '无'}（码 {d['fault_code']}）"
            f"　历史：{d['fault_history_text'] or '无'}",
        ]
        return CommandResult(True, "运行状态", lines, list(result.warnings))

    def run_params(_args):
        result = _call(executor, "get_firmware_parameters", {})
        if not result.ok:
            return _fail("固件参数", result)
        lines = [f"{key}：{_fmt(value)}" for key, value in result.data.items()]
        return CommandResult(True, "固件参数", lines, list(result.warnings))

    def run_loop(_args):
        result = _call(executor, "calculate_current_loop", {})
        if not result.ok:
            return _fail("电流环理论分析", result)
        d = result.data
        lines = [
            f"开环穿越 {_fmt(d['gain_cross_hz'], 4)} Hz　相位裕度 {_fmt(d['phase_margin_deg'])}°"
            f"　增益裕度 {_fmt(d['gain_margin_db'])} dB",
            f"闭环带宽 {_fmt(d['closed_loop_bandwidth_hz'], 4)} Hz"
            f"　谐振峰 {_fmt(d['closed_loop_peak_db'])} dB @ {_fmt(d['closed_loop_peak_hz'], 4)} Hz",
            f"Ms {_fmt(d['ms'])} @ {_fmt(d['ms_frequency_hz'], 4)} Hz"
            f"　最大极点模 {_fmt(d['maximum_pole_magnitude'], 4)}",
        ]
        return CommandResult(True, "电流环理论分析", lines, list(result.warnings))

    def electrical_hz() -> float:
        state = _call(executor, "get_runtime_state", {})
        if not state.ok:
            return 0.0
        return abs(float(state.data.get("speed_actual_rpm", 0.0))) * POLE_PAIRS / 60.0

    def run_fft(args):
        channel = args["channels"][0]
        capture = _call(executor, "capture_signal_window",
                        {"channels": [channel], "duration_s": args["duration_s"]})
        if not capture.ok:
            return _fail("FFT", capture)
        result = _call(executor, "analyze_fft", {
            "snapshot_id": capture.data["snapshot_id"], "channel": channel,
            "remove_dc": True})
        if not result.ok:
            return _fail("FFT", result)
        d = result.data
        fe = electrical_hz()
        lines = [f"{channel}　{d['sample_rate_hz']} Hz × {d['sample_count']} 点"
                 f"（{_fmt(d['duration_s'])} s，分辨率 {_fmt(d['resolution_hz'])} Hz）"
                 f"　直流 {_fmt(d['dc'])}　交流 RMS {_fmt(d['ac_rms'])}"]
        if fe > 1.0:
            lines.append(f"当前电频率 fe ≈ {fe:.2f} Hz（按 {POLE_PAIRS} 对极）")
        # harness 返回前 6 个局部极大值；低于最大峰 2% 的多为窗函数泄漏，不显示
        largest = max((p["amplitude"] for p in d["peaks"]), default=0.0)
        for peak in d["peaks"]:
            if peak["amplitude"] < 0.02 * largest:
                continue
            note = f"　≈ {peak['frequency_hz'] / fe:.2f} fe" if fe > 1.0 else ""
            lines.append(f"{peak['frequency_hz']:9.1f} Hz　{peak['amplitude']:.4g} "
                         f"{peak['unit']}{note}")
        return CommandResult(True, f"FFT：{channel}", lines,
                             list(capture.warnings) + list(result.warnings))

    def run_stats(args):
        capture = _call(executor, "capture_signal_window", args)
        if not capture.ok:
            return _fail("时域统计", capture)
        result = _call(executor, "analyze_time_domain",
                       {"snapshot_id": capture.data["snapshot_id"]})
        if not result.ok:
            return _fail("时域统计", result)
        lines = [f"{result.data['sample_rate_hz']} Hz × {result.data['sample_count']} 点"]
        for name, m in result.data["metrics"].items():
            lines.append(f"{name}：均值 {_fmt(m['mean'])}　交流 RMS {_fmt(m['ac_rms'])}"
                         f"　峰峰 {_fmt(m['peak_to_peak'])} {m['unit']}")
        return CommandResult(True, "时域统计", lines,
                             list(capture.warnings) + list(result.warnings))

    def fft_parse(args):
        parsed = _split_channels_and_duration(args, ["iq_a"], 1.0, 1)
        return parsed

    def describe_capture(args):
        return (f"通道 {', '.join(args['channels'])} · 时长 {args['duration_s']:g} s"
                "（原始高速数据，未经显示滤波）")

    return [
        Command("state", "查看运行状态", "read_only", "state",
                "连接、转速、电流、母线、温度与故障", _no_args, run_state,
                aliases=("状态", "zt"), background=True),
        Command("params", "查看固件参数", "read_only", "params",
                "电流环 PI、采样率、反馈滤波与遥测配置", _no_args, run_params,
                aliases=("参数表",), background=True),
        Command("fft", "截取并做 FFT", "read_only", "fft [通道=iq] [时长=1s]",
                "从高速缓冲截取原始信号，Hann 窗单边谱，列出主要尖峰",
                fft_parse, run_fft, describe_capture, aliases=("频谱分析", "pp"),
                background=True),
        Command("stats", "截取并做时域统计", "read_only", "stats [通道…] [时长=1s]",
                "均值、交流 RMS、峰峰值",
                lambda a: _split_channels_and_duration(a, ["iq_a", "ia_a", "ib_a"], 1.0, 6),
                run_stats, describe_capture, aliases=("统计", "tj"), background=True),
        Command("loop", "电流环理论分析", "read_only", "loop",
                "按当前固件 PI 计算带宽、裕度与闭环极点", _no_args, run_loop,
                aliases=("电流环",), background=True),
    ]


# ------------------------------------------------------------------ 界面命令
def build_ui_commands(*, navigate: Callable[[str], bool],
                      open_manual: Callable[[str], None],
                      start_guide: Callable[[str | None], str | None],
                      stop_motor: Callable[[], None]) -> list[Command]:
    def goto_parse(args):
        if not args:
            raise CommandError("请输入页面名，如 goto 监控")
        pages = find_pages(" ".join(args))
        if not pages:
            raise CommandError("没有匹配的页面")
        return {"page": pages[0].key, "title": pages[0].title}

    def run_goto(args):
        ok = navigate(args["page"])
        return CommandResult(ok, "切换页面",
                             [f"已切换到 {args['title']}"] if ok else [],
                             error=None if ok else "该页面当前不可用")

    def run_help(args):
        open_manual(args["query"])
        return CommandResult(True, "使用说明书",
                             [f"已打开说明书{'并搜索：' + args['query'] if args['query'] else ''}"])

    def run_guide(args):
        error = start_guide(args["card"])
        return CommandResult(error is None, "新手引导",
                             [] if error else [], error=error)

    def run_stop(_args):
        stop_motor()
        return CommandResult(True, "停机", [
            "已执行与界面“停止”按钮相同的停机操作；以设备应答和遥测为准。",
            "紧急情况请使用“紧急停止”按钮或硬件急停，不要依赖命令面板。"])

    page_names = "、".join(page.title for page in PAGES[:6]) + "…"
    return [
        Command("goto", "切换页面", "ui", "goto <页面>", f"如 {page_names}",
                goto_parse, run_goto, lambda a: f"切换到 {a['title']}",
                aliases=("打开", "页面", "go")),
        Command("help", "打开使用说明书", "ui", "help [关键词]", "按关键词搜索任务卡",
                lambda a: {"query": " ".join(a)}, run_help,
                lambda a: f"搜索：{a['query']}" if a["query"] else "打开说明书首页",
                aliases=("帮助", "说明书", "?")),
        Command("guide", "新手引导", "ui", "guide [卡片id]",
                "不带参数：界面总览；带卡片 id：按卡片步骤逐个高亮控件",
                lambda a: {"card": a[0] if a else None}, run_guide,
                lambda a: f"按卡片 {a['card']} 引导" if a["card"] else "界面总览",
                aliases=("引导", "教程")),
        Command("stop", "停机", "safety", "stop",
                "与界面“停止”按钮相同；急停请用专用按钮或硬件",
                _no_args, run_stop, lambda a: "发送停机命令（与停止按钮相同）",
                aliases=("停机", "停止")),
    ]
