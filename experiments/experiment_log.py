"""实验日志：固定模板，数据部分全部经 harness 只读工具从驭衡记录的文件计算得到。

两种模板：
  单次实验  —— 概况、参数、工作流与事件、慢遥测、高速数据统计、频谱、矢量分解、
                功率流、波形截图、音视频、结论（操作者 / AI 分开标注）、数据来源审计
  对比实验  —— 基准 vs 对照：参数差异、结果指标对比（含变化率）、叠加频谱与曲线、
                矢量分解对比、功率效率对比、结论、数据来源审计
输出 HTML（可播放动图/录像），并用 QtWebEngine 打印 PDF（无 WebEngine 时退回 QTextDocument）。
AI 结论单独存 ai_conclusion.json，不写进实验会话、也不覆盖操作者结论。
"""
from __future__ import annotations

import html
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ai.experiment_tools import create_experiment_registry, waveform_batches
from ai.harness import AuditLog, ToolCall, ToolContext, ToolExecutor
from core.phasor_decomposition import analyze
from core.power_estimate import estimate_power_series
from experiments.report import (
    _CONCLUSION_STATUS, _METRIC_NAMES, _build_svg, _conclusion_html, _detail_text,
    _summarize_faults, _workflow_results,
)
from experiments.telemetry import summarize_telemetry
from widgets import animation_render as render
from widgets.media_recorder import read_media_manifest

LOG_HTML = "实验日志.html"
LOG_PDF = "实验日志.pdf"
AI_FILE = "ai_conclusion.json"
USER_FILE = "conclusion.json"          # 对比日志的操作者结论（单次实验用会话里的结论）
_CSS = """
body{font-family:"Microsoft YaHei",Arial,sans-serif;max-width:1180px;margin:28px auto;padding:0 24px;
color:#243447;line-height:1.6}
h1{color:#123a5a;border-bottom:3px solid #ff865f;padding-bottom:6px}
h2{color:#123a5a;margin-top:34px;border-left:5px solid #4fc3f7;padding-left:10px}
h3{color:#28506f}
table{border-collapse:collapse;width:100%;margin:10px 0 20px}
th,td{border:1px solid #cbd5df;padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}
th{background:#edf4f8} td.num{text-align:right;font-variant-numeric:tabular-nums}
pre{background:#f4f7f9;padding:12px;overflow:auto;border-radius:5px;font-size:12px}
.muted{color:#60778b} .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.fixed{background:#e3f2fd;color:#1565c0} .ai{background:#fff3e0;color:#e65100}
.user{background:#e8f5e9;color:#2e7d32}
.figs{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}
.figs figure{margin:0} figure img{max-width:100%;border-radius:6px;border:1px solid #d7e1e8}
figcaption{font-size:12px;color:#60778b;margin-top:4px}
.notice{background:#fff8e1;border:1px solid #ffe082;padding:8px 12px;border-radius:5px}
.ai-box{background:#fffaf3;border:1px dashed #ffb74d;padding:10px 14px;border-radius:6px;white-space:pre-wrap}
.up{color:#c62828} .down{color:#2e7d32}
video{max-width:100%;border-radius:6px;background:#000}
.print-only{display:none}
@media print{video,audio{display:none}.print-only{display:block}h2{page-break-after:avoid}
figure{page-break-inside:avoid}}
"""


@dataclass
class LogResult:
    html: Path
    pdf: Path | None
    folder: Path
    warnings: list[str] = field(default_factory=list)


def _esc(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value))


def _num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if not np.isfinite(number):
        return "—"
    return f"{number:.{digits}g}"


def _table(headers: list[str], rows: list[list[Any]], numeric_from: int = 1) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            raw = isinstance(value, _Raw)
            text = value.text if raw else _esc(value)
            cls = ' class="num"' if index >= numeric_from and not raw and _is_number(value) else ""
            cells.append(f"<td{cls}>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


class _Raw:
    """表格里直接输出的 HTML 片段。"""

    def __init__(self, text: str) -> None:
        self.text = text


def _is_number(value: Any) -> bool:
    try:
        float(str(value).rstrip("%"))
        return True
    except ValueError:
        return False


def _figure(src: str, caption: str, width: int | None = None) -> str:
    style = f' style="width:{width}px"' if width else ""
    return (f'<figure{style}><img src="{html.escape(src)}" alt="{html.escape(caption)}">'
            f"<figcaption>{html.escape(caption)}</figcaption></figure>")


def load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ─── 生成器 ──────────────────────────────────────────────────
class ExperimentLogGenerator:
    def __init__(self, repository, params_provider=None) -> None:
        self.repository = repository
        self.registry, self.data = create_experiment_registry(repository, params_provider)

    # ------------------------------------------------------------ 公共
    def _executor(self, folder: Path) -> tuple[ToolExecutor, AuditLog]:
        audit_path = folder / "log_audit.jsonl"
        if audit_path.exists():
            audit_path.unlink()
        audit = AuditLog(audit_path)
        return ToolExecutor(self.registry, ToolContext(None, None), audit_log=audit), audit

    @staticmethod
    def _call(executor: ToolExecutor, name: str, **arguments) -> dict:
        clean = {k: v for k, v in arguments.items() if v is not None}
        result = executor.execute(ToolCall(f"log_{name}", name, clean))
        return {"ok": result.ok, "data": result.data, "error": result.error,
                "source": result.metadata.get("source"), "metadata": result.metadata}

    @staticmethod
    def comparison_folder(root: Path, baseline_id: str, other_ids: list[str]) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{baseline_id}_vs_{'_'.join(other_ids)}_{stamp}"
        return Path(root) / "_comparisons" / name

    def _high_rate_suite(self, executor, experiment_id, batch, t0, t1) -> dict:
        window = {"experiment_id": experiment_id, "batch": batch, "t0": t0, "t1": t1}
        suite = {"statistics": self._call(executor, "capture_statistics", **window)}
        if not suite["statistics"]["ok"]:
            return suite
        suite["iq_spectrum"] = self._call(executor, "capture_spectrum", channel="iq_a",
                                          peaks=8, **window)
        suite["complex_spectrum"] = self._call(executor, "capture_spectrum",
                                               channel="current_complex", peaks=10, **window)
        suite["vector"] = self._call(executor, "decompose_current_vector", **window)
        suite["power"] = self._call(executor, "power_balance", **window)
        return suite

    # ------------------------------------------------------------ 单次实验
    def generate(self, experiment_id: str, batch: str | None = None,
                 t0: float | None = None, t1: float | None = None,
                 progress: Callable[[int, str], None] | None = None,
                 make_pdf: bool = True, reuse_assets: bool = False) -> LogResult:
        """reuse_assets：只更新文字（如写入 AI 结论后），沿用上次生成的图与动图。"""
        step = progress or (lambda _p, _t: None)
        session = self.repository.load(experiment_id)
        session_dir = self.repository.session_dir(experiment_id)
        folder = session_dir / "report"
        assets = folder / "log_assets"
        if assets.exists() and not reuse_assets:
            shutil.rmtree(assets)
        assets.mkdir(parents=True, exist_ok=True)

        def reuse(name: str) -> Path | None:
            path = assets / name
            return path if reuse_assets and path.exists() else None
        executor, audit = self._executor(folder)
        warnings: list[str] = []

        step(5, "读取实验档案")
        listing = self._call(executor, "list_experiment_data", experiment_id=experiment_id)
        overview = self._call(executor, "experiment_overview", experiment_id=experiment_id)
        step(15, "分析高速数据")
        suite = self._high_rate_suite(executor, experiment_id, batch, t0, t1)
        has_high = suite["statistics"]["ok"]
        if not has_high:
            warnings.append(suite["statistics"]["error"] or "没有高速数据")

        figures = {}
        if has_high:
            step(40, "生成频谱图与动图")
            window, _source = self.data.window({"experiment_id": experiment_id, "batch": batch,
                                                **({"t0": t0} if t0 is not None else {}),
                                                **({"t1": t1} if t1 is not None else {})})
            fe = suite["statistics"]["data"].get("electrical_frequency_hz")
            figures["spectrum"] = reuse("频谱.png") or render.save_png(
                render.spectrum_image(window, fe), assets / "频谱.png")
            figures["spectrum_gif"] = reuse("滑动频谱.gif")
            if figures["spectrum_gif"] is None:
                frames = render.sliding_spectrum_frames(window, fe)
                if frames:
                    figures["spectrum_gif"] = render.save_gif(frames, assets / "滑动频谱.gif", 180)
            step(55, "生成矢量合成动图")
            previous = load_json(folder / "log_data.json") if reuse_assets else {}
            figures["vector_omitted"] = previous.get("vector_animation_omitted", [])
            figures["vector_gif"] = reuse("矢量合成.gif")
            if figures["vector_gif"] is None:
                analysis = analyze(window, float(window.time[0]), float(window.time[-1]))
                frames, figures["vector_omitted"] = render.phasor_frames(analysis)
                figures["vector_gif"] = render.save_gif(frames, assets / "矢量合成.gif", 70)
            if suite["power"]["ok"]:
                step(70, "生成功率流动图")
                figures["power_gif"] = reuse("功率流.gif")
                if figures["power_gif"] is None:
                    series = estimate_power_series(window.time, window.columns(),
                                                   self.data.power_params())
                    vbus = float(np.mean(window.extra.get("vbus_v", [24.0])))
                    figures["power_gif"] = render.save_gif(
                        render.sankey_frames(series, vbus), assets / "功率流.gif", 120)
        step(82, "整理波形截图与音视频")
        screenshots = []
        for item in waveform_batches(session_dir):
            if item["has_waveform_png"]:
                target = assets / f"波形_{item['batch']}.png"
                shutil.copy(session_dir / "waveforms" / item["batch"] / "波形.png", target)
                screenshots.append((target, item["batch"]))
        media = read_media_manifest(session_dir / "media")

        step(88, "写入日志")
        evidence = {"template": "single", "experiment_id": experiment_id,
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "listing": listing, "overview": overview, "high_rate": suite,
                    "vector_animation_omitted": figures.get("vector_omitted", [])}
        _write_json(folder / "log_data.json", evidence)
        events = self.repository.read_events(experiment_id)
        telemetry = self.repository.read_telemetry(experiment_id)
        page = _single_html(session, events, telemetry, suite, figures, screenshots, media,
                            load_json(folder / AI_FILE), list(audit.records), warnings,
                            folder)
        html_path = folder / LOG_HTML
        html_path.write_text(page, encoding="utf-8")
        pdf = None
        if make_pdf:
            step(94, "打印 PDF")
            pdf = print_pdf(html_path, folder / LOG_PDF)
            if pdf is None:
                warnings.append("PDF 未生成（缺少 QtWebEngine 或打印失败）")
        step(100, "完成")
        return LogResult(html_path, pdf, folder, warnings)

    # ------------------------------------------------------------ 对比实验
    def generate_comparison(self, baseline_id: str, other_ids: list[str],
                            t0: float | None = None, t1: float | None = None,
                            progress: Callable[[int, str], None] | None = None,
                            folder: Path | None = None, make_pdf: bool = True,
                            reuse_assets: bool = False) -> LogResult:
        step = progress or (lambda _p, _t: None)
        if not other_ids:
            raise ValueError("至少选择一个对照实验")
        ids = [baseline_id] + [i for i in other_ids if i != baseline_id]
        folder = folder or self.comparison_folder(self.repository.root, baseline_id, ids[1:])
        assets = folder / "log_assets"
        if assets.exists() and not reuse_assets:
            shutil.rmtree(assets)
        assets.mkdir(parents=True, exist_ok=True)
        executor, audit = self._executor(folder)
        warnings: list[str] = []
        runs = []
        for index, experiment_id in enumerate(ids):
            step(5 + int(50 * index / len(ids)), f"分析 {experiment_id}")
            run = {"id": experiment_id, "session": self.repository.load(experiment_id),
                   "overview": self._call(executor, "experiment_overview",
                                          experiment_id=experiment_id),
                   "suite": self._high_rate_suite(executor, experiment_id, None, t0, t1)}
            if index:
                run["params"] = self._call(executor, "compare_parameters",
                                           baseline_id=baseline_id, experiment_id=experiment_id)
            if not run["suite"]["statistics"]["ok"]:
                warnings.append(f"{experiment_id}：{run['suite']['statistics']['error']}")
            runs.append(run)

        step(60, "生成叠加对比图")
        figures = {"vector": []}
        spectra, speeds, iqs = [], [], []
        for run in runs:
            if not run["suite"]["statistics"]["ok"]:
                continue
            window, _ = self.data.window({"experiment_id": run["id"],
                                          **({"t0": t0} if t0 is not None else {}),
                                          **({"t1": t1} if t1 is not None else {})})
            label = f"{run['id']} {run['session'].name}"[:32]
            if "iq_a" in window.extra:
                f, a = render.iq_spectrum(window.extra["iq_a"], window.rate_hz)
                spectra.append((label, f, a))
                iqs.append((label, window.time, _smooth(window.extra["iq_a"], window.rate_hz)))
            if window.speed_rpm is not None:
                speeds.append((label, window.time, window.speed_rpm))
            gif = assets / f"矢量合成_{run['id']}.gif"
            if not (reuse_assets and gif.exists()):
                analysis = analyze(window, float(window.time[0]), float(window.time[-1]))
                frames, _omitted = render.phasor_frames(analysis, width=380, height=360,
                                                        frames=60)
                render.save_gif(frames, gif, 80)
            figures["vector"].append((gif, label))
        if spectra:
            fe = max(abs(r["suite"]["statistics"]["data"].get("electrical_frequency_hz") or 0)
                     for r in runs if r["suite"]["statistics"]["ok"])
            figures["spectrum"] = render.save_png(render.overlay_spectrum_image(
                spectra, "iq 频谱叠加", (0.0, max(1000.0, 14 * fe))), assets / "频谱对比.png")
            figures["iq"] = render.save_png(render.overlay_series_image(
                iqs, "iq（20 ms 滑动平均）叠加", "A"), assets / "iq对比.png")
        if speeds:
            figures["speed"] = render.save_png(render.overlay_series_image(
                speeds, "转速叠加", "rpm"), assets / "转速对比.png")

        step(88, "写入日志")
        evidence = {"template": "comparison", "baseline_id": baseline_id, "compared": ids[1:],
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "runs": [{k: v for k, v in run.items() if k != "session"} for run in runs]}
        _write_json(folder / "log_data.json", evidence)
        page = _comparison_html(runs, figures, load_json(folder / USER_FILE),
                                load_json(folder / AI_FILE), list(audit.records), warnings)
        html_path = folder / LOG_HTML
        html_path.write_text(page, encoding="utf-8")
        pdf = print_pdf(html_path, folder / LOG_PDF) if make_pdf else None
        if make_pdf and pdf is None:
            warnings.append("PDF 未生成（缺少 QtWebEngine 或打印失败）")
        step(100, "完成")
        return LogResult(html_path, pdf, folder, warnings)


def _smooth(values, rate: float, seconds: float = 0.02) -> np.ndarray:
    size = max(1, int(rate * seconds))
    kernel = np.ones(size) / size
    return np.convolve(np.asarray(values, float), kernel, mode="same")


# ─── 单次实验 HTML ───────────────────────────────────────────
def _head(title: str) -> str:
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>")


def _fixed_banner() -> str:
    return ('<p class="muted"><span class="tag fixed">固定模板 · 数据部分</span> '
            "由驭衡根据实验档案与保存的原始数据自动计算生成，未经人工或 AI 修改；"
            "每项数值的来源文件、时间窗与计算工具见最后一节“数据来源与处理”。</p>")


def _no_high_rate(error: str | None) -> str:
    return (f'<p class="notice">本实验没有可用的高速数据（{_esc(error)}）。'
            "在实验进行中到监控页点“保存所有波形”，就会在实验目录保存 16 kHz 的 高速数据.csv。</p>")


def _stats_rows(stats: dict) -> list[list[Any]]:
    names = {"speed_rpm": "转速 (rpm)", "iq_a": "iq (A)", "iqref_a": "iq 给定 (A)",
             "ia_a": "Ia (A)", "ib_a": "Ib (A)", "vbus_v": "母线电压 (V)"}
    rows = []
    for key, name in names.items():
        item = stats.get(key)
        if isinstance(item, dict):
            rows.append([name, _num(item["mean"]), _num(item["rms"]), _num(item["std"]),
                         _num(item["min"]), _num(item["max"]), _num(item["peak_to_peak"])])
    return rows


def _peaks_table(result: dict, two_sided: bool) -> str:
    if not result.get("ok"):
        return f'<p class="notice">{_esc(result.get("error"))}</p>'
    rows = []
    for peak in result["data"].get("peaks", []):
        freq = peak["frequency_hz"]
        row = [_num(freq, 6), _num(peak.get("order_of_fe"), 4)]
        if two_sided:
            row.append("正序" if freq > 0 else "负序")
        rows.append(row + [_num(peak["amplitude"])])
    headers = ["频率 Hz", "电频率倍数"] + (["相序"] if two_sided else []) + ["幅值 A"]
    return _table(headers, rows, numeric_from=0)


def _kv_table(values: dict, empty: str) -> str:
    return _table(["项目", "值"], [[k, v] for k, v in values.items()]) if values \
        else f'<p class="muted">{empty}</p>'


def _vector_section(vector: dict, figures: dict) -> str:
    if not vector.get("ok"):
        return f'<p class="notice">{_esc(vector.get("error"))}</p>'
    data = vector["data"]
    rows = [[c["label"], _num(c["stationary_freq_hz"], 6), _num(c["dq_ripple_freq_hz"], 6),
             _num(c["amplitude_a"]), f"{c['pct_of_fundamental']:.1f}%"] for c in data["components"]]
    parts = [f"<p>电频率 fe = {_num(data['electrical_frequency_hz'], 5)} Hz；"
             f"未解释残差 {_num(data['residual_pct_of_fundamental'], 3)}% 基波。</p>",
             _table(["分量", "静止系频率 Hz", "dq 中纹波 Hz", "平均幅值 A", "占基波"], rows)]
    pairs = data.get("oscillation_pairs") or []
    if pairs:
        parts.append("<h3>成对分量（±f 合成的摆动）</h3>")
        parts.append(_table(["频率 ±Hz", "直线度", "摆动方向 °"], [
            [_num(p["freq_hz"], 6), f"{100 * p['linearity']:.0f}%", _num(p["axis_deg"], 3)]
            for p in pairs]))
    roundness = data.get("roundness")
    if roundness:
        parts.append("<h3>圆度分解</h3>")
        parts.append(_table(["偏心 %", "椭圆 %", "椭圆长轴 °", "三角 %", "六边形 %"], [[
            _num(roundness["eccentric_pct"], 3), _num(roundness["ellipse_pct"], 3),
            _num(roundness["ellipse_axis_deg"], 3), _num(roundness["triangle_pct"], 3),
            _num(roundness["hexagon_pct"], 3)]], numeric_from=0))
    eq = data.get("equivalent_distortion")
    if eq:
        parts.append(_table(["等效 Ia 零偏 %", "等效 Ib 零偏 %", "Ib 增益误差 %",
                             "Ib 相位误差 °", "等效死区 %"], [[
            _num(eq["offset_a_pct"], 3), _num(eq["offset_b_pct"], 3), _num(eq["gain_b_pct"], 3),
            _num(eq["phase_b_deg"], 3), _num(eq["dead_time_pct"], 3)]], numeric_from=0))
    causes = "".join(f"<li>{_esc(line)}</li>" for line in data.get("possible_causes", []))
    parts.append(f"<h3>按形状推断的可能原因（规则判断，非结论）</h3><ul>{causes}</ul>")
    if figures.get("vector_gif"):
        omitted = figures.get("vector_omitted") or []
        caption = "一个电周期内各分量首尾相接的合成（白线为合成轨迹，灰线为实测）"
        if omitted:
            caption += "；动图省略了每帧转角过大的分量：" + "、".join(omitted)
        parts.append('<div class="figs">' + _figure(
            f"log_assets/{figures['vector_gif'].name}", caption, 480) + "</div>")
    return "".join(parts)


def _power_section(power: dict, figures: dict) -> str:
    if not power.get("ok"):
        return f'<p class="notice">{_esc(power.get("error"))}</p>'
    data = power["data"]
    mean = data["mean_w"]
    names = (("inv", "逆变器电气输入"), ("cu", "定子铜损"), ("em", "电磁功率"),
             ("kinetic", "转子动能变化"), ("fric", "负载与摩擦"))
    rows = [[name, _num(mean[key]), _num(data["energy_j"][key])] for key, name in names]
    rows.append(["未解释（铁损、死区电压误差等）", _num(data.get("unexplained_w")), "—"])
    efficiency = data.get("motor_efficiency")
    parts = [_table(["项目", "平均功率 W", "能量 J"], rows),
             f"<p>电机电磁效率 η = 电磁功率 / 逆变器输入 = "
             f"{'—' if efficiency is None else f'{efficiency:.1%}'}；"
             f"时长 {_num(data['duration_s'], 4)} s。</p>",
             f'<p class="muted">{_esc(power["metadata"].get("note"))}；参数 {_esc(data.get("parameters"))}</p>']
    if figures.get("power_gif"):
        parts.append('<div class="figs">' + _figure(
            f"log_assets/{figures['power_gif'].name}", "功率流随时间回放（整段均匀取 48 帧）",
            640) + "</div>")
    return "".join(parts)


def _media_section(media: list[dict], folder: Path) -> str:
    if not media:
        return "<p>本实验没有录像/录音（可在实验管理页“音视频”标签开启自动录制）。</p>"
    parts = []
    for entry in media:
        src = f"../media/{entry.get('file')}"
        exists = (folder.parent / "media" / str(entry.get("file"))).exists()
        span = (f"相对实验开始 {_num(entry.get('start_offset_s'), 5)}–"
                f"{_num(entry.get('end_offset_s'), 5)} s")
        info = (f"{_esc(entry.get('file'))} · {_esc(entry.get('camera') or '无摄像头')} · "
                f"{_esc(entry.get('microphone') or '无麦克风')} · {span} · "
                f"时长 {_num(entry.get('duration_s'), 4)} s")
        if not exists:
            parts.append(f'<p class="notice">录制文件缺失：{info}</p>')
            continue
        tag = ("video" if entry.get("kind") == "video" else "audio")
        parts.append(f'<figure><{tag} controls preload="metadata" src="{html.escape(src)}"></{tag}>'
                     f"<figcaption>{info}</figcaption>"
                     f'<p class="print-only">录制文件：{html.escape(src)}（请在 HTML 版中播放）</p></figure>')
    return "".join(parts)


def _ai_block(ai: dict) -> str:
    if not ai.get("text"):
        return ('<p class="muted"><span class="tag ai">AI 分析</span> 未生成。'
                "可在实验日志页点“AI 写结论”，由诊断助手依据上面的数据起草。</p>")
    return (f'<p><span class="tag ai">AI 分析 · 需人工确认</span> '
            f'<span class="muted">模型 {_esc(ai.get("model"))} · {_esc(ai.get("generated_at"))} · '
            f"依据 log_data.json（SHA-256 {_esc(str(ai.get('evidence_sha256', ''))[:12])}）</span></p>"
            f'<div class="ai-box">{_esc(ai.get("text"))}</div>')


def _provenance(records: list[dict], sources: list[tuple[str, dict]]) -> str:
    source_rows = [[tool, s.get("file"), (s.get("sha256") or "")[:16],
                    f"{_num(s.get('t0'), 6)}–{_num(s.get('t1'), 6)} s", s.get("samples"),
                    _num(s.get("rate_hz"), 6)] for tool, s in sources if s]
    call_rows = [[r["tool"], json.dumps(r["arguments"], ensure_ascii=False),
                  "成功" if r["ok"] else f"失败：{r.get('error')}", _num(r["elapsed_ms"], 4)]
                 for r in records]
    return ("<h3>源文件</h3>" + (_table(["工具", "文件（实验目录内）", "SHA-256 前 16 位",
                                          "时间窗", "样本数", "采样率 Hz"], source_rows)
                                  if source_rows else "<p>—</p>")
            + "<h3>工具调用记录（log_audit.jsonl）</h3>"
            + _table(["工具", "参数", "结果", "耗时 ms"], call_rows))


def _single_html(session, events, telemetry, suite, figures, screenshots, media, ai,
                 records, warnings, folder: Path) -> str:
    device = session.device.to_dict() if session.device else {}
    summary = summarize_telemetry(telemetry)
    summary["faults"] = _summarize_faults(telemetry)
    template = session.template_snapshot
    results = _workflow_results(events)
    workflow_rows = [[i, s.get("title", ""), "必做" if s.get("required", True) else "可选",
                      results.get(s.get("step_id"), {}).get("status", "未完成"),
                      results.get(s.get("step_id"), {}).get("note", "—")]
                     for i, s in enumerate(template.get("steps", []), 1)]
    metric_rows = [[_METRIC_NAMES.get(k, k), v.get("count", 0), _num(v.get("min")),
                    _num(v.get("max")), _num(v.get("mean")), _num(v.get("rms")),
                    _num(v.get("peak_to_peak"))] for k, v in summary.get("metrics", {}).items()]
    event_rows = [[_num(e.get("monotonic_s"), 6), e.get("timestamp", ""), e.get("type", ""),
                   e.get("message", ""), _detail_text(e.get("details", {}))] for e in events]
    has_high = suite["statistics"]["ok"]
    stats = suite["statistics"].get("data", {})
    parts = [_head(f"实验日志：{session.name}"),
             f"<h1>实验日志：{html.escape(session.name)}</h1>", _fixed_banner()]
    if warnings:
        parts.append('<p class="notice">' + "；".join(_esc(w) for w in warnings) + "</p>")
    parts += ["<h2>1. 实验概况</h2>", _table(["项目", "内容"], [
        ["实验编号", session.experiment_id], ["实验名称", session.name],
        ["状态", session.status.value], ["实验目的", session.purpose],
        ["操作者", session.operator], ["数据来源", session.data_source],
        ["开始 / 结束", f"{session.started_at or '—'} / {session.ended_at or '—'}"],
        ["结束原因", session.end_reason], ["软件版本", session.software_version],
        ["实验模板", template.get("name", "自由实验")]], numeric_from=9),
        "<h2>2. 设备与参数快照</h2>",
        "<h3>设备</h3>", _kv_table(device, "未记录设备信息"),
        "<h3>控制参数</h3>", _kv_table(session.controller_params, "未记录控制参数"),
        "<h3>保护参数</h3>", _kv_table(session.protection_params, "未记录保护参数"),
        "<h2>3. 工作流与事件</h2>",
        (_table(["#", "步骤", "属性", "结果", "备注"], workflow_rows) if workflow_rows
         else "<p>自由实验，没有模板步骤。</p>"),
        _table(["相对时间 s", "时间", "类型", "事件", "详情"], event_rows) if event_rows else "<p>无事件。</p>",
        "<h2>4. 慢遥测统计与曲线</h2>",
        f"<p>样本 {summary.get('samples', 0)}；持续 {_num(summary.get('duration_s'))} s；"
        f"转速平均绝对误差 {_num(summary.get('speed_mae_rpm'))} rpm。</p>",
        _table(["指标", "样本", "最小", "最大", "均值", "RMS", "峰峰值"], metric_rows),
        f'<div class="chart">{_build_svg(telemetry, events)}</div>' if telemetry else "<p>本实验没有慢遥测。</p>",
        "<h2>5. 高速数据时域统计</h2>"]
    if has_high:
        source = suite["statistics"]["source"]
        parts += [f"<p>文件 {_esc(source['file'])}；时间窗 {_num(source['t0'], 6)}–{_num(source['t1'], 6)} s；"
                  f"{source['samples']} 点 @ {_num(source['rate_hz'], 6)} Hz；电频率 "
                  f"{_num(stats.get('electrical_frequency_hz'), 5)} Hz；iq 跟踪误差 RMS "
                  f"{_num(stats.get('iq_tracking_error_rms_a'))} A。</p>",
                  _table(["量", "均值", "RMS", "标准差", "最小", "最大", "峰峰值"], _stats_rows(stats)),
                  "<h2>6. 频谱分析</h2>", '<div class="figs">',
                  _figure(f"log_assets/{figures['spectrum'].name}",
                          "上：iq 单边谱；下：电流复数双边谱（橙色虚线为电频率倍数）", 900)]
        if figures.get("spectrum_gif"):
            parts.append(_figure(f"log_assets/{figures['spectrum_gif'].name}",
                                 "iq 频谱随时间变化（0.5 s 窗口滑动）", 640))
        parts += ["</div>", "<h3>iq 频谱峰值</h3>", _peaks_table(suite["iq_spectrum"], False),
                  "<h3>电流复数频谱峰值（正频率 = 正序，负频率 = 负序）</h3>",
                  _peaks_table(suite["complex_spectrum"], True),
                  "<h2>7. 电流矢量分解</h2>", _vector_section(suite["vector"], figures),
                  "<h2>8. 功率流</h2>", _power_section(suite["power"], figures)]
    else:
        notice = _no_high_rate(suite["statistics"].get("error"))
        parts += [notice, "<h2>6. 频谱分析</h2>", notice, "<h2>7. 电流矢量分解</h2>", notice,
                  "<h2>8. 功率流</h2>", notice]
    parts.append("<h2>9. 波形截图</h2>")
    parts.append('<div class="figs">' + "".join(
        _figure(f"log_assets/{path.name}", f"保存批次 {batch}", 540) for path, batch in screenshots)
        + "</div>" if screenshots else "<p>没有保存波形截图。</p>")
    parts += ["<h2>10. 音视频记录</h2>", _media_section(media, folder),
              "<h2>11. 结论</h2>",
              '<p><span class="tag user">操作者结论</span></p>', _conclusion_html(session),
              _ai_block(ai), "<h2>12. 数据来源与处理</h2>",
              "<p>第 1–10 节全部来自实验档案与下列文件，经下列只读工具计算；本节可用于复核。</p>",
              _provenance(records, [(name, suite[name].get("source"))
                                    for name in suite if isinstance(suite[name], dict)]),
              "</body></html>"]
    return "".join(parts)


# ─── 对比实验 HTML ───────────────────────────────────────────
def comparison_metrics(run: dict) -> dict:
    """从一次实验的工具结果里取对比用的指标（缺数据时为 None）。"""
    suite = run["suite"]
    out: dict[str, float | None] = {}
    stats = suite["statistics"].get("data", {}) if suite["statistics"]["ok"] else {}
    speed = stats.get("speed_rpm") or {}
    iq = stats.get("iq_a") or {}
    out["转速均值 rpm"] = speed.get("mean")
    out["转速标准差 rpm"] = speed.get("std")
    out["转速峰峰值 rpm"] = speed.get("peak_to_peak")
    out["iq 均值 A"] = iq.get("mean")
    out["iq 标准差 A"] = iq.get("std")
    out["iq 跟踪误差 RMS A"] = stats.get("iq_tracking_error_rms_a")
    out["电频率 Hz"] = stats.get("electrical_frequency_hz")
    vector = suite.get("vector", {})
    if vector.get("ok"):
        comps = {c["key"]: c["pct_of_fundamental"] for c in vector["data"]["components"]}
        out["基波幅值 A"] = vector["data"]["components"][0]["amplitude_a"]
        out["零偏 %基波"] = comps.get("k+0")
        out["负序 %基波"] = comps.get("k-1")
        out["−2 次 %基波"] = comps.get("k-2")
        out["−5 次 %基波"] = comps.get("k-5")
        out["+7 次 %基波"] = comps.get("k+7")
        out["未解释残差 %"] = vector["data"]["residual_pct_of_fundamental"]
    power = suite.get("power", {})
    if power.get("ok"):
        mean = power["data"]["mean_w"]
        out["逆变器输入 W"] = mean["inv"]
        out["定子铜损 W"] = mean["cu"]
        out["电磁功率 W"] = mean["em"]
        eff = power["data"].get("motor_efficiency")
        out["电机效率 %"] = None if eff is None else 100.0 * eff
    return out


def _delta(base, value) -> _Raw:
    if base is None or value is None:
        return _Raw("—")
    diff = value - base
    pct = f"（{diff / abs(base) * 100:+.1f}%）" if abs(base) > 1e-12 else ""
    cls = "up" if diff > 0 else "down" if diff < 0 else ""
    return _Raw(f'<span class="{cls}">{diff:+.4g}{pct}</span>')


def _comparison_html(runs, figures, user, ai, records, warnings) -> str:
    base = runs[0]
    title = f"对比实验日志：{base['session'].name} vs " + "、".join(r["session"].name for r in runs[1:])
    parts = [_head(title), f"<h1>{html.escape(title)}</h1>", _fixed_banner()]
    if warnings:
        parts.append('<p class="notice">' + "；".join(_esc(w) for w in warnings) + "</p>")
    parts.append("<h2>1. 对比概况</h2>")
    parts.append(_table(["角色", "实验编号", "名称", "状态", "开始时间", "高速数据时间窗"], [[
        "基准" if i == 0 else f"对照 {i}", r["id"], r["session"].name, r["session"].status.value,
        r["session"].started_at,
        (f"{_num(r['suite']['statistics']['source']['t0'], 6)}–"
         f"{_num(r['suite']['statistics']['source']['t1'], 6)} s")
        if r["suite"]["statistics"]["ok"] else "无高速数据"] for i, r in enumerate(runs)],
        numeric_from=9))
    parts.append("<h2>2. 参数差异（只列不同项）</h2>")
    for run in runs[1:]:
        result = run.get("params", {})
        changed = result.get("data", {}).get("changed", []) if result.get("ok") else []
        parts.append(f"<h3>{_esc(run['id'])} 相对基准</h3>")
        parts.append(_table(["参数", "基准", "本实验"], [
            [c["parameter"], c["baseline"], c["value"]] for c in changed])
            if changed else "<p>参数快照与基准完全相同。</p>")
    parts.append("<h2>3. 结果指标对比</h2>")
    metrics = [comparison_metrics(run) for run in runs]
    headers = ["指标", f"基准 {base['id']}"]
    for run in runs[1:]:
        headers += [run["id"], "变化"]
    rows = []
    for key in metrics[0]:
        row = [key, _num(metrics[0][key])]
        for m in metrics[1:]:
            row += [_num(m.get(key)), _delta(metrics[0][key], m.get(key))]
        rows.append(row)
    parts.append(_table(headers, rows))
    parts.append('<p class="muted">变化 = 对照 − 基准（括号内为相对基准的百分比）；红色为增大，绿色为减小，'
                 "好坏需结合指标含义判断（如误差、纹波减小为改善）。</p>")
    parts.append("<h2>4. 频谱与波形对比</h2><div class=\"figs\">")
    for key, caption in (("spectrum", "iq 频谱叠加"), ("speed", "转速叠加"), ("iq", "iq 叠加")):
        if figures.get(key):
            parts.append(_figure(f"log_assets/{figures[key].name}", caption, 900))
    parts.append("</div>")
    for run in runs:
        parts.append(f"<h3>{_esc(run['id'])} iq 频谱峰值</h3>")
        parts.append(_peaks_table(run["suite"].get("iq_spectrum", {"ok": False, "error": "无数据"}),
                                  False))
    parts.append("<h2>5. 电流矢量分解对比</h2>")
    labels: dict[str, str] = {}
    table_rows: dict[str, list] = {}
    for index, run in enumerate(runs):
        vector = run["suite"].get("vector", {})
        for comp in vector.get("data", {}).get("components", []) if vector.get("ok") else []:
            labels.setdefault(comp["key"], comp["label"])
            table_rows.setdefault(comp["key"], [None] * len(runs))[index] = comp["pct_of_fundamental"]
    parts.append(_table(["分量"] + [r["id"] for r in runs], [
        [labels[key]] + [("—" if v is None else f"{v:.1f}%") for v in values]
        for key, values in table_rows.items()]))
    parts.append('<div class="figs">' + "".join(
        _figure(f"log_assets/{path.name}", label, 380) for path, label in figures["vector"])
        + "</div>")
    parts.append("<h2>6. 结论</h2>")
    status = _CONCLUSION_STATUS.get(user.get("result_status"), "待判断")
    parts.append('<p><span class="tag user">操作者结论</span></p>')
    parts.append(_table(["项目", "内容"], [
        ["结论状态", status], ["主要观察", user.get("observations") or "—"],
        ["异常与可能原因", user.get("anomalies") or "—"],
        ["改进建议", user.get("recommendations") or "—"],
        ["下一次实验计划", user.get("next_plan") or "—"]]) if user else "<p>结论待填写。</p>")
    parts.append(_ai_block(ai))
    parts.append("<h2>7. 数据来源与处理</h2>")
    sources = []
    for run in runs:
        for name, result in run["suite"].items():
            if isinstance(result, dict) and result.get("source"):
                sources.append((f"{run['id']} {name}", result["source"]))
    parts.append(_provenance(records, sources))
    parts.append("</body></html>")
    return "".join(parts)


# ─── AI 结论 ────────────────────────────────────────────────
_AI_SYSTEM = (
    "你是电机控制实验分析助手。下面给出驭衡上位机按固定模板计算的实验数据（JSON），"
    "你也可以调用只读工具复核。要求：只依据这些数据和工具结果下结论；不得编造任何数值，"
    "引用的每个数值后用括号注明来源（如“capture_statistics”）；数据不足以判断时明确写“需要进一步实验验证”。"
    "输出四段，标题依次为：主要观察、异常与可能原因、改进建议、下一步实验。"
    "如果是对比实验，重点说明参数变化与结果变化之间的对应关系，并指出哪些变化可能只是测量波动。")


def draft_ai_conclusion(client, generator: ExperimentLogGenerator, folder: Path,
                        model_name: str = "", on_event=None) -> dict:
    """用诊断助手的模型客户端起草结论，存 ai_conclusion.json；不改动操作者结论。"""
    import hashlib
    from ai.harness import AgentRuntime

    evidence_path = Path(folder) / "log_data.json"
    if not evidence_path.exists():
        raise RuntimeError("请先生成实验日志，再让 AI 依据日志数据写结论")
    evidence = evidence_path.read_text(encoding="utf-8")
    executor, _audit = generator._executor(Path(folder) / "ai_tools")
    runtime = AgentRuntime(client, generator.registry, executor, max_rounds=6)
    text = runtime.run([
        {"role": "system", "content": _AI_SYSTEM},
        {"role": "user", "content": "实验数据：\n" + evidence[:60000]},
    ], on_event=on_event)
    result = {"text": text, "model": model_name,
              "generated_at": datetime.now().isoformat(timespec="seconds"),
              "evidence_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest()}
    _write_json(Path(folder) / AI_FILE, result)
    return result


# ─── PDF ────────────────────────────────────────────────────
def print_pdf(html_path: Path, pdf_path: Path, timeout_ms: int = 60000) -> Path | None:
    """优先用 QtWebEngine 打印（与网页一致）；不可用时退回 QTextDocument。"""
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    try:
        from PySide6.QtWebEngineCore import QWebEnginePage
    except ImportError:
        return _print_pdf_textdocument(html_path, pdf_path)
    page = QWebEnginePage()
    loop = QEventLoop()
    state = {"ok": False}

    def loaded(ok: bool) -> None:
        if not ok:
            loop.quit()
            return
        page.printToPdf(str(pdf_path))

    def printed(_path: str, ok: bool) -> None:
        state["ok"] = ok
        loop.quit()

    page.loadFinished.connect(loaded)
    page.pdfPrintingFinished.connect(printed)
    QTimer.singleShot(timeout_ms, loop.quit)
    page.load(QUrl.fromLocalFile(str(Path(html_path).resolve())))
    loop.exec()
    page.deleteLater()
    return pdf_path if state["ok"] and Path(pdf_path).exists() else None


def _print_pdf_textdocument(html_path: Path, pdf_path: Path) -> Path | None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument
    document = QTextDocument()
    document.setBaseUrl(QUrl.fromLocalFile(str(Path(html_path).resolve())))
    document.setHtml(Path(html_path).read_text(encoding="utf-8"))
    writer = QPdfWriter(str(pdf_path))
    writer.setPageSize(QPageSize(QPageSize.A4))
    document.print_(writer)
    return pdf_path if Path(pdf_path).exists() else None
