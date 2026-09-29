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
import math
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
    _CONCLUSION_STATUS, _METRIC_NAMES, _summarize_faults, _workflow_results,
)
from experiments import log_layout as L
from experiments.media_subtitles import build_cues, vtt_data_uri, write_session_subtitles
from experiments.telemetry import summarize_telemetry
from widgets import animation_render as render
from widgets.media_recorder import read_media_manifest

LOG_HTML = "实验日志.html"
LOG_PDF = "实验日志.pdf"
AI_FILE = "ai_conclusion.json"
USER_FILE = "conclusion.json"          # 对比日志的操作者结论（单次实验用会话里的结论）
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


def _copy_chalk(assets: Path) -> None:
    """HTML 屏幕阅读用的粉笔背景插图（随包资源，约 650 KB）；缺失时背景自动留白。"""
    from runtime_paths import resource_path
    source = resource_path("assets", "report_chalk")
    target = assets / "chalk"
    target.mkdir(parents=True, exist_ok=True)
    for image in source.glob("*.jpg"):
        if not (target / image.name).exists():
            shutil.copy2(image, target / image.name)


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

    def _high_rate_suite(self, executor, experiment_id, batch, t0, t1,
                         load_mode: str | None = None) -> dict:
        window = {"experiment_id": experiment_id, "batch": batch, "t0": t0, "t1": t1}
        suite = {"statistics": self._call(executor, "capture_statistics", **window)}
        if not suite["statistics"]["ok"]:
            return suite
        suite["iq_spectrum"] = self._call(executor, "capture_spectrum", channel="iq_a",
                                          peaks=8, **window)
        suite["complex_spectrum"] = self._call(executor, "capture_spectrum",
                                               channel="current_complex", peaks=10, **window)
        suite["vector"] = self._call(executor, "decompose_current_vector", **window)
        suite["power"] = self._call(executor, "power_balance", load_mode=load_mode, **window)
        suite["step"] = self._call(executor, "step_response", **window)
        suite["time_frequency"] = self._call(executor, "time_frequency", channel="iq_a", **window)
        suite["dyno"] = self._call(executor, "dyno_load_check", load_mode=load_mode, **window)
        return suite

    # ------------------------------------------------------------ 单次实验
    def generate(self, experiment_id: str, batch: str | None = None,
                 t0: float | None = None, t1: float | None = None,
                 progress: Callable[[int, str], None] | None = None,
                 make_pdf: bool = True, reuse_assets: bool = False,
                 load_mode: str | None = None) -> LogResult:
        """reuse_assets：只更新文字（如写入 AI 结论后），沿用上次生成的图与动图。
        load_mode：对拖负载工况（覆盖实验记录里的值；None 表示用实验记录）。"""
        step = progress or (lambda _p, _t: None)
        session = self.repository.load(experiment_id)
        session_dir = self.repository.session_dir(experiment_id)
        folder = session_dir / "report"
        assets = folder / "log_assets"
        if assets.exists() and not reuse_assets:
            shutil.rmtree(assets)
        assets.mkdir(parents=True, exist_ok=True)
        _copy_chalk(assets)

        def reuse(name: str) -> Path | None:
            path = assets / name
            return path if reuse_assets and path.exists() else None
        executor, audit = self._executor(folder)
        warnings: list[str] = []

        step(5, "读取实验档案")
        listing = self._call(executor, "list_experiment_data", experiment_id=experiment_id)
        overview = self._call(executor, "experiment_overview", experiment_id=experiment_id)
        step(15, "分析高速数据")
        suite = self._high_rate_suite(executor, experiment_id, batch, t0, t1, load_mode)
        has_high = suite["statistics"]["ok"]
        if not has_high:
            warnings.append(suite["statistics"]["error"] or "没有高速数据")

        figures = {}
        if has_high:
            figures = self._single_figures(experiment_id, batch, t0, t1, suite, folder,
                                           assets, reuse, step)
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
        subtitles = {}
        for entry in media:
            cues = build_cues(session, entry, telemetry, events)
            if cues:
                subtitles[entry.get("file")] = vtt_data_uri(cues)
        try:
            write_session_subtitles(self.repository, experiment_id)
        except OSError as exc:
            warnings.append(f"录像字幕写入失败：{exc}")
        page = _single_html(session, events, telemetry, suite, figures, screenshots, media,
                            load_json(folder / AI_FILE), list(audit.records), warnings,
                            folder, subtitles)
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

    def _single_figures(self, experiment_id, batch, t0, t1, suite, folder, assets, reuse,
                        step) -> dict:
        """单次实验的全部图与动图（纯 QImage 绘制，可在后台线程执行）。"""
        import analysis_dynamic as dyn
        from ai.experiment_tools import downsample_speed, stft_for
        from core.vector_shape import trajectory_shape

        args = {"experiment_id": experiment_id, "batch": batch,
                **({"t0": t0} if t0 is not None else {}),
                **({"t1": t1} if t1 is not None else {})}
        whole, _ = self.data.window(args)
        steady, steady_source = self.data.window(args, prefer_steady=True)
        vector = suite["vector"]["data"] if suite["vector"]["ok"] else {}
        fe = vector.get("electrical_frequency_hz") or \
            suite["statistics"]["data"].get("electrical_frequency_hz")
        figures: dict = {"steady_source": steady_source}

        def keep(name: str, build):
            path = reuse(name)
            if path is None:
                path = build(assets / name)
            if path is not None:
                figures[name] = path

        def animated(name: str, still: str, make_frames, frame_ms: int, pick) -> None:
            """动图 + 打印用静态帧（PDF 里放不了动画，首帧也常常不具代表性）。"""
            def build(path):
                frames = make_frames()
                if not frames:
                    return None
                render.save_png(frames[pick(len(frames))], assets / still)
                return render.save_gif(frames, path, frame_ms)
            keep(name, build)
            if name in figures and (assets / still).exists():
                figures[still] = assets / still

        step(30, "频谱（稳态段）")
        keep("频谱.png", lambda path: render.save_png(render.spectrum_image(steady, fe), path))
        animated("滑动频谱.gif", "滑动频谱_打印.png",
                 lambda: render.sliding_spectrum_frames(whole, fe), 180, lambda n: n // 2)
        step(40, "STFT 时频图")
        for channel, title, name in (("iq_a", "iq STFT 时频图", "时频_iq.png"),
                                     ("speed_rpm", "转速 STFT 时频图", "时频_转速.png")):
            values = whole.speed_rpm if channel == "speed_rpm" else whole.extra.get(channel)
            if values is None:
                continue
            if channel == "speed_rpm":
                t_s, v_s = downsample_speed(whole.time, values)
                rate = (len(t_s) - 1) / max(t_s[-1] - t_s[0], 1e-9)
            else:
                t_s, v_s, rate = whole.time, values, whole.rate_hz
            keep(name, lambda path, t_s=t_s, v_s=v_s, rate=rate, title=title: render.save_png(
                render.heatmap_image(*(lambda m: (m["time"], m["axis"], m["power"]))(
                    stft_for(t_s, v_s, rate)), title, "频率 / Hz"), path))
        step(50, "动态响应")
        if whole.speed_rpm is not None:
            t_s, v_s = downsample_speed(whole.time, whole.speed_rpm)
            found = dyn.suggest_step(t_s, v_s)
            if found is not None:
                keep("转速阶跃响应.png", lambda path: render.save_png(render.response_image(
                    dyn.response_metrics(t_s, v_s, found["event"], found["target"]),
                    "转速阶跃响应", "rpm"), path))
        step(60, "矢量可视化与分解")
        analysis = analyze(steady, float(steady.time[0]), float(steady.time[-1]))
        alpha = steady.ia
        beta = (steady.ia + 2.0 * steady.ib) / np.sqrt(3.0)
        keep("电流圆.png", lambda path: render.save_png(render.current_circle_image(
            alpha, beta, trajectory_shape(alpha, beta)), path))
        previous = load_json(folder / "log_data.json") if reuse("矢量合成.gif") else {}
        figures["vector_omitted"] = previous.get("vector_animation_omitted", [])

        def phasor(follow):
            frames, figures["vector_omitted"] = render.phasor_frames(analysis, follow=follow)
            return frames

        animated("矢量合成.gif", "矢量合成_打印.png", lambda: phasor(False), 70, lambda n: n // 8)
        animated("矢量合成_跟随.gif", "矢量合成_跟随_打印.png", lambda: phasor(True), 70,
                 lambda n: n // 8)
        keep("分量柱状图.png", lambda path: render.save_png(
            render.component_bars_image(analysis.components), path))
        dyno = suite.get("dyno") or {}
        if suite["power"]["ok"]:
            step(72, "功率流动图")
            vbus = float(np.mean(whole.extra.get("vbus_v", [24.0])))
            power = suite["power"]["data"]
            used = power.get("parameters") or {}
            # 与 power_balance 工具同一套参数（含对拖的等效惯量与负载短路铜损）
            params = self.data.power_params().with_dyno(
                used.get("load_mode"), self.data.motor_params(), used.get("load_inertia"))
            keep("功率流.gif", lambda path: render.save_gif(render.sankey_frames(
                estimate_power_series(whole.time, whole.columns(), params), vbus), path, 120))
            mean = dict(power["mean_w"], stored_j=power.get("peak_stored_j", 0.0))
            keep("功率流_平均.png", lambda path: render.save_png(render.sankey_mean_image(
                mean, vbus, f"{power['duration_s']:.1f} s 平均功率（储能为最高转速时）"), path))
        if dyno.get("ok") and _dyno_applies(dyno["data"]) and "iq_a" in whole.extra:
            keep("对拖负载_转矩转速.png", lambda path: render.save_png(
                _dyno_figure(dyno["data"], whole), path))
        return figures

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
        _copy_chalk(assets)
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
            arguments = {"experiment_id": run["id"],
                         **({"t0": t0} if t0 is not None else {}),
                         **({"t1": t1} if t1 is not None else {})}
            window, _ = self.data.window(arguments)
            # 频谱与矢量合成用稳态段（与单次日志、工具结果一致）；曲线叠加用整段
            steady, _ = self.data.window(arguments, prefer_steady=True)
            label = f"{run['id']} {run['session'].name}"[:32]
            if "iq_a" in window.extra:
                f, a = render.iq_spectrum(steady.extra["iq_a"], steady.rate_hz)
                spectra.append((label, f, a))
                iqs.append((label, window.time, _smooth(window.extra["iq_a"], window.rate_hz)))
            if window.speed_rpm is not None:
                speeds.append((label, window.time, window.speed_rpm))
            gif = assets / f"矢量合成_{run['id']}.gif"
            if not (reuse_assets and gif.exists()):
                analysis = analyze(steady, float(steady.time[0]), float(steady.time[-1]))
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
# 版式见 experiments/log_layout.py；本节只负责“把工具结果放进哪一章、写什么说明”，
# 不修改任何数值。章节顺序：概览 → 过程 → 电流频谱 → 矢量 → 功率 → 设备 → 事件 → 影音 → 结论 → 来源。
_STATUS_TEXT = {"completed": ("已完成 · 正常结束", ""), "aborted": ("异常中止", "review"),
                "running": ("记录中", "review"), "created": ("未开始", "review")}


def _no_high_rate(error: str | None) -> str:
    return L.note(f"本实验没有可用的高速数据（{_esc(error)}）。在实验进行中到监控页点“保存所有波形”，"
                  "就会在实验目录保存 16 kHz 的 高速数据.csv。")


def _stats_rows(stats: dict) -> list[list[Any]]:
    names = {"speed_rpm": "转速 / rpm", "iq_a": "iq / A", "iqref_a": "iq 给定 / A",
             "ia_a": "Ia / A", "ib_a": "Ib / A", "vbus_v": "母线电压 / V"}
    rows = []
    for key, name in names.items():
        item = stats.get(key)
        if isinstance(item, dict):
            rows.append([name, _num(item["mean"]), _num(item["rms"]), _num(item["std"]),
                         _num(item["min"]), _num(item["max"])])
    return rows


def _peaks_table(result: dict, two_sided: bool) -> str:
    if not result.get("ok"):
        return L.note(_esc(result.get("error")))
    rows = []
    for peak in result["data"].get("peaks", []):
        freq = peak["frequency_hz"]
        row = [_num(freq, 6), _num(peak.get("order_of_fe"), 4)]
        if two_sided:
            # 0 Hz 是静止偏置（零偏），不按正负频率标相序
            row.append("静止（零偏）" if abs(freq) < 1e-6 else "正序" if freq > 0 else "负序")
        rows.append(row + [_num(peak["amplitude"])])
    headers = ["频率 Hz", "电频率倍数"] + (["相序"] if two_sided else []) + ["幅值 A"]
    return L.table(headers, rows, cls="compact", numeric_from=0)


def _asset(figures: dict, name: str) -> str | None:
    path = figures.get(name)
    return f"log_assets/{path.name}" if path else None


def _fig(doc: L.Doc, figures: dict, name: str, caption: str, origin: str = "",
         print_name: str | None = None) -> str:
    src = _asset(figures, name)
    if not src:
        return ""
    return doc.figure(src, caption, print_src=_asset(figures, print_name) if print_name else None,
                      origin=origin)


def _duration_s(session, telemetry) -> float | None:
    try:
        start = datetime.fromisoformat(session.started_at)
        end = datetime.fromisoformat(session.ended_at)
        return (end - start).total_seconds()
    except (TypeError, ValueError):
        times = [r.get("monotonic_s") for r in telemetry if r.get("monotonic_s") is not None]
        return float(times[-1]) if times else None


def _key_moments(events: list[dict]) -> list[tuple[float, str]]:
    """全过程图上标注的关键时刻（从事件里按含义挑选，不插值）。"""
    out = []
    for event in events:
        when = event.get("monotonic_s")
        message = str(event.get("message") or "")
        detail = str((event.get("details") or {}).get("detail", ""))
        label = None
        if message == "运行预检通过":
            label = "预检通过"
        elif message == "运行状态切换" and "→ running" in detail:
            label = "设备 ACK 启动"
        elif message == "运行状态切换" and detail.startswith("running → stopping"):
            label = "请求停机"
        elif message == "运行状态切换" and detail.startswith("stopping →"):
            label = "停机确认"
        elif event.get("type") in ("session_aborted", "fault_locked"):
            label = message or "异常"
        if label and when is not None:
            out.append((float(when), label))
    return out[:6]


def _event_summary(event: dict) -> str:
    details = event.get("details") or {}
    if isinstance(details, dict):
        if details.get("detail"):
            return str(details["detail"])
        keep = {k: v for k, v in details.items() if k not in ("source_timestamp",) and v not in ("", None, [])}
        return "；".join(f"{k}={v}" for k, v in keep.items())
    return str(details)


def _flatten(prefix: str, value: Any, out: dict) -> dict:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    else:
        out[prefix] = value
    return out


class _Fields:
    """按字段路径取值并记下用过的字段；没用到的字段最后原样列出，不静默丢弃。"""

    def __init__(self, **roots: dict) -> None:
        self.values: dict[str, Any] = {}
        for name, root in roots.items():
            _flatten(name, root or {}, self.values)
        self.used: set[str] = set()

    def get(self, path: str) -> Any:
        self.used.add(path)
        return self.values.get(path)

    def prefix(self, prefix: str) -> dict[str, Any]:
        items = {k: v for k, v in self.values.items() if k.startswith(prefix + ".")}
        self.used.update(items)
        return items

    def rows(self, spec: list[tuple[str, str, str]]) -> list[tuple[str, Any]]:
        rows = []
        for path, label, unit in spec:
            value = self.get(path)
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                value = "、".join(str(v) for v in value)
            elif isinstance(value, float):
                value = _num(value, 6)
            rows.append((label, f"{value} {unit}".strip()))
        return rows

    def leftover(self) -> dict[str, Any]:
        return {k: v for k, v in self.values.items() if k not in self.used}


_IDENTITY_FIELDS = [
    ("device.name", "设备编号", ""), ("device.motor_type", "电机类型", ""),
    ("device.rated_power_w", "额定功率", "W"), ("device.dc_bus_voltage_v", "母线额定", "V"),
    ("device.extra.pole_pairs", "极对数", ""), ("device.extra.max_rpm", "最高转速", "rpm"),
    ("device.extra.current_limit_a", "电流限幅", "A"), ("device.inverter", "驱动板", ""),
    ("device.controller", "控制板", ""), ("device.sensors", "位置传感器", "")]
_FIRMWARE_FIELDS = [
    ("device.extra.hardware_version", "硬件版本", ""), ("device.firmware_version", "固件版本", ""),
    ("device.protocol_version", "协议版本", ""), ("device.extra.device_id", "设备 ID", ""),
    ("device.extra.equipment_profile_id", "设备档案", ""),
    ("device.extra.equipment_revision", "档案修订", ""),
    ("device.extra.dyno_load.text", "对拖负载工况", "")]
_SAMPLING_FIELDS = [
    ("control.mode_params.spd_sample_time", "速度环周期", "s"),
    ("control.mode_params.cur_sample_time", "电流环周期", "s"),
    ("control.speed_feedback_fifo.speed_fifo_depth", "速度 FIFO 深度", "点"),
    ("control.speed_feedback_fifo.group_delay_ms", "速度 FIFO 群延迟", "ms"),
    ("control.current_feedback_filter.current_filter_enabled", "电流反馈滤波", ""),
    ("control.current_feedback_filter.cutoff_hz", "电流滤波截止", "Hz")]
_MEASURED_FIELDS = [
    ("device.extra.measured.Rs_ohm", "Rs", "Ω"), ("device.extra.measured.Ld_mH", "Ld", "mH"),
    ("device.extra.measured.Lq_mH", "Lq", "mH"), ("device.extra.measured.psi_f_Wb", "永磁磁链 ψ", "Wb"),
    ("device.extra.measured.J_kgm2", "转动惯量 J", "kg·m²"),
    ("device.extra.measured.B", "粘滞系数 B", "N·m·s"), ("device.extra.measured.Tc_Nm", "库仑摩擦 Tc", "N·m")]
_LIMIT_FIELDS = [
    ("protection.max_rpm", "运行保护 · 最高转速", "rpm"),
    ("protection.max_current_a", "运行保护 · 最大电流", "A"), ("protection.iq_max", "运行保护 · iq 上限", "A"),
    ("device.extra.equipment_safety_limits.max_rpm", "设备档案 · 最高转速", "rpm"),
    ("device.extra.equipment_safety_limits.max_current_a", "设备档案 · 最大电流", "A"),
    ("device.extra.equipment_safety_limits.max_bus_voltage_v", "设备档案 · 最高母线", "V"),
    ("device.extra.equipment_safety_limits.max_temperature_c", "设备档案 · 最高温度", "°C")]


def _device_chapter(session) -> tuple[str, _Fields]:
    fields = _Fields(device=session.device.to_dict() if session.device else {},
                     control=session.controller_params or {},
                     protection=session.protection_params or {})
    identity = L.kv(fields.rows(_IDENTITY_FIELDS))
    firmware = L.kv(fields.rows(_FIRMWARE_FIELDS + _SAMPLING_FIELDS))
    mode = fields.get("control.control_mode")
    target = fields.get("control.target_speed_rpm")
    loops = [["Kp", _num(fields.get("control.mode_params.kp_spd"), 6), _num(fields.get("control.mode_params.kp_cur"), 6)],
             ["Ki", _num(fields.get("control.mode_params.ki_spd"), 6), _num(fields.get("control.mode_params.ki_cur"), 6)],
             ["Kd", _num(fields.get("control.mode_params.kd_spd"), 6), "—"]]
    for alias in ("kp", "ki", "kd", "sample_time"):          # 与 kp_spd 等重复的旧字段
        fields.get(f"control.mode_params.{alias}")
    iq_max = fields.get("control.mode_params.iq_max")
    control = (L.table(["参数（保存原值）", "速度环", "电流环"], loops) +
               L.quiet(f"控制方式 {_esc(mode)}；目标转速 {_num(target)} rpm；iq 上限 {_num(iq_max)} A。"
                       "PI 参数按下位机保存的原始数值列出，未换算成物理单位。"))
    sensors = fields.prefix("control.sensor_params")
    sensor_rows = [(k.split(".", 2)[-1], v) for k, v in sensors.items()]
    measured = L.kv(fields.rows(_MEASURED_FIELDS))
    limits = L.kv(fields.rows(_LIMIT_FIELDS))
    twin = fields.prefix("control.mechanical_load")
    fields.get("device.extra.dyno_load.mode")         # 已用中文 text 显示
    parts = [L.cols("<h3>设备身份</h3>" + identity, "<h3>固件与采样</h3>" + firmware),
             "<h3>控制环</h3>", control]
    if sensor_rows:
        parts.append("<h3>位置传感器设置</h3>" + L.kv(sensor_rows))
    parts.append(L.cols("<h3>设备档案中的电机参数</h3>" + (measured or L.quiet("未记录。")) +
                        L.quiet("档案未注明这些值的测量方法与日期，功率与对拖模型不使用它们，见第 05 章“参数来源”。"),
                        "<h3>限值</h3>" + (limits or L.quiet("未记录。"))))
    if twin:
        parts.append(L.details("数字孪生仿真配置（controller_params.mechanical_load）",
                               L.quiet("这些是数字孪生仿真用的负载设置；本实验数据来源为 "
                                       f"{_esc(session.data_source)}，真机实验时不参与任何计算。") +
                               L.kv([(k.split(".", 2)[-1], v) for k, v in twin.items()]),
                               f"{len(twin)} 项"))
    rest = fields.leftover()
    if rest:
        parts.append(L.details("其余字段（原样，未做中文映射）",
                               L.kv([(k, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict))
                                      else v) for k, v in rest.items()]), f"{len(rest)} 项"))
    return "".join(parts), fields


def _param_source(suite: dict, fields: _Fields, review: list[str]) -> str:
    """功率 / 对拖计算实际使用的参数与设备档案对照；相差 20% 以上标为待复核。"""
    power = suite.get("power") or {}
    used = (power.get("data") or {}).get("parameters") or {}
    dyno = ((suite.get("dyno") or {}).get("data") or {}).get("motor") or {}
    measured = {k.rsplit(".", 1)[-1]: v for k, v in fields.values.items()
                if k.startswith("device.extra.measured.")}
    ld, lq = measured.get("Ld_mH"), measured.get("Lq_mH")
    rows_spec = [
        ("Rs / Ω", used.get("rs_ohm"), measured.get("Rs_ohm")),
        ("L / mH", (dyno.get("ls_h") or 0) * 1e3 or None,
         (ld + lq) / 2 if isinstance(ld, (int, float)) and isinstance(lq, (int, float)) else None),
        ("ψ / mWb（转矩常数口径）", (dyno.get("psi_wb") or 0) * 1e3 or None,
         measured.get("psi_f_Wb") * 1e3 if isinstance(measured.get("psi_f_Wb"), (int, float)) else None),
        ("转矩常数 kt / N·m·A⁻¹", used.get("kt_nm_per_a"), None),
        ("J / kg·m²", used.get("inertia"), measured.get("J_kgm2")),
        ("极对数", used.get("pole_pairs"), fields.values.get("device.extra.pole_pairs"))]
    rows, flagged = [], []
    for name, a, b in rows_spec:
        if a is None and b is None:
            continue
        diff = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
            ratio = b / a
            if abs(ratio - 1.0) > 0.2:
                diff = L.Raw(f'<span class="review-cell">档案为计算值的 {ratio:.2g} 倍</span>')
                flagged.append(name.split(" /")[0])
        rows.append([name, _num(a, 4), _num(b, 4), diff])
    if flagged:
        review.append("设备档案电机参数与计算所用参数不一致：" + "、".join(flagged))
    return (L.table(["参数", "功率 / 对拖计算使用", "设备档案", "差异"], rows) +
            L.note("<b>参数选择策略</b>：功率流与对拖模型使用数字孪生电机参数（来自电机铭牌，"
                   "磁链按上位机转矩常数 kt 换算，保证“实测转矩 = kt·iq”与模型同一口径）。设备档案中的"
                   "电机参数没有注明测量方法，只列出对照；两者相差 20% 以上的项已标出，"
                   "需在参数辨识后更新档案或数字孪生参数。", "info"))


def _vector_block(doc: L.Doc, vector: dict, stats: dict, figures: dict, review: list[str]) -> tuple[str, str]:
    """矢量章节正文与状态标签。数值异常时标“待复核”，不装饰成结论。"""
    omitted = figures.get("vector_omitted") or []
    omit = ("；动图省略了每帧转角过大的分量：" + "、".join(omitted)) if omitted else ""
    circle = _fig(doc, figures, "电流圆.png", "αβ 电流圆：稳态段全部采样点，虚线为平均半径",
                  "Iα = Ia，Iβ = (Ia + 2Ib)/√3")
    anim = _fig(doc, figures, "矢量合成.gif",
                "各分量首尾相接的合成（深色线为合成轨迹，浅灰为实测）" + omit,
                "打印版为动图中的一帧", print_name="矢量合成_打印.png")
    follow = _fig(doc, figures, "矢量合成_跟随.gif",
                  "同一合成，以基波末端为中心放大其余小分量" + omit, print_name="矢量合成_跟随_打印.png")
    bars = _fig(doc, figures, "分量柱状图.png", "各分量占基波的百分比（铜色 = 正序，青绿 = 负序，灰 = 静止矢量）")
    if not vector.get("ok"):
        return L.cols(circle, anim) + L.note(_esc(vector.get("error"))), L.tag("未完成", "review")
    data = vector["data"]
    components = data["components"]
    fundamental = components[0]["amplitude_a"] if components else None
    phase_rms = max((stats.get(k) or {}).get("rms") or 0.0 for k in ("ia_a", "ib_a"))
    status = L.tag("已完成")
    checks = []
    if fundamental and phase_rms and fundamental > 3.0 * math.sqrt(2.0) * phase_rms:
        checks.append(f"拟合基波 {_num(fundamental)} A 远大于相电流 RMS {_num(phase_rms)} A 对应的幅值，"
                      "可能是时间窗含启停或设计矩阵病态")
    residual = data.get("residual_pct_of_fundamental")
    if residual is not None and residual > 50:
        checks.append(f"未解释残差 {_num(residual, 3)}% 基波，分解对这段数据不适用")
    if checks:
        status = L.tag("量级待复核", "review")
        review.append("矢量分解：" + "；".join(checks))
    rows = [[c["label"], _num(c["stationary_freq_hz"], 6), _num(c["dq_ripple_freq_hz"], 6),
             _num(c["amplitude_a"]), f"{c['pct_of_fundamental']:.2f}%"] for c in components]
    parts = [L.cols(circle, anim), L.cols(follow, ""), bars] if follow else [L.cols(circle, anim), bars]
    if checks:
        parts.append(L.note("<b>先核对量级，再解释形状</b>：" + "；".join(_esc(c) for c in checks) +
                            "。原值保留在下表，不作为正式结论。"))
    parts += [f"<h3>分量分解（电频率 fe = {_num(data['electrical_frequency_hz'], 5)} Hz，"
              f"未解释残差 {_num(residual, 3)}% 基波）</h3>",
              L.table(["分量", "静止系频率 Hz", "dq 中纹波 Hz", "平均幅值 A", "占基波"], rows, cls="compact")]
    roundness = data.get("roundness")
    eq = data.get("equivalent_distortion")
    side = []
    if roundness:
        side.append("<h3>圆度分解</h3>" + L.kv([
            ("偏心", f"{_num(roundness['eccentric_pct'], 3)} %"), ("椭圆", f"{_num(roundness['ellipse_pct'], 3)} %"),
            ("椭圆长轴", f"{_num(roundness['ellipse_axis_deg'], 3)} °"),
            ("三角", f"{_num(roundness['triangle_pct'], 3)} %"), ("六边形", f"{_num(roundness['hexagon_pct'], 3)} %")]))
    if eq:
        side.append("<h3>等效采样误差</h3>" + L.kv([
            ("Ia 零偏", f"{_num(eq['offset_a_pct'], 3)} %"), ("Ib 零偏", f"{_num(eq['offset_b_pct'], 3)} %"),
            ("Ib 增益误差", f"{_num(eq['gain_b_pct'], 3)} %"), ("Ib 相位误差", f"{_num(eq['phase_b_deg'], 3)} °"),
            ("等效死区", f"{_num(eq['dead_time_pct'], 3)} %")]))
    if side:
        parts.append(L.cols(*side) if len(side) == 2 else side[0])
    pairs = data.get("oscillation_pairs") or []
    if pairs:
        parts.append("<h3>成对分量（±f 合成的摆动）</h3>" + L.table(
            ["频率 ±Hz", "直线度", "摆动方向 °"],
            [[_num(p["freq_hz"], 6), f"{100 * p['linearity']:.0f}%", _num(p["axis_deg"], 3)] for p in pairs],
            cls="compact"))
    causes = "".join(f"<li>{_esc(line)}</li>" for line in data.get("possible_causes", []))
    if causes:
        parts.append(L.note(f"<b>按形状推断的可能原因（规则判断，非结论）</b><ul>{causes}</ul>", "info"))
    return "".join(parts), status


def _step_block(doc: L.Doc, step: dict, figures: dict) -> str:
    if not step or not step.get("ok"):
        return L.quiet(_esc((step or {}).get("error") or "无转速数据"))
    data = step["data"]
    if not data.get("step_found"):
        return L.quiet(_esc(data.get("note")))
    m = data["metrics"]
    rows = [("事件时刻", f"{_num(data['event_s'], 5)} s（{data.get('event_basis', '')}）"),
            ("基线 → 目标", f"{_num(m.get('baseline'), 5)} → {_num(m.get('target'), 5)} rpm"),
            ("上升时间 10%→90%", f"{_num(m.get('rise_s'), 4)} s"),
            ("超调", f"{_num(m.get('overshoot_pct'), 4)} %"),
            ("调节时间（±2%）", f"{_num(m.get('settling_s'), 4)} s" if m.get("settling_s") is not None
             else "记录内未进入并保持在 ±2% 带内"),
            ("稳态误差", f"{_num(m.get('steady_error'), 4)} rpm（末段均值 − 目标）"),
            ("最大加速度", f"{_num(m.get('peak_slope'), 4)} rpm/s"),
            ("振铃频率", f"{_num(m.get('ringing_hz'), 4)} Hz")]
    parts = [_fig(doc, figures, "转速阶跃响应.png", "转速阶跃响应（算法与“离线傅里叶 → 动态响应”子页相同）",
                  "高速数据转速，降采样到 1 kHz")]
    if not data.get("step_valid"):
        parts.append(L.note(_esc(data.get("message"))))
    parts.append(L.kv(rows))
    parts.append(L.quiet("超调与调节时间基于转速测量值；转速本身有 1 倍机械频率的波动时，"
                         "±2% 调节带可能始终无法满足，此时看稳态误差与峰峰值更合适。"))
    return "".join(parts)


def _dyno_block(doc: L.Doc, dyno: dict | None, figures: dict, review: list[str]) -> str:
    if not dyno or not dyno.get("ok"):
        return ""
    data = dyno["data"]
    mode = data.get("load_mode")
    head = (f"<h3>对拖负载</h3><p>负载工况：{_esc(data.get('load_mode_text'))}"
            f"（来源：{_esc(data.get('mode_source'))}）。</p>")
    if mode in ("none", "off"):
        return head + L.quiet("负载侧无电磁制动，“负载与摩擦”只含机械摩擦与负载本身。")
    if mode == "torque":
        return head + L.quiet("负载电机由其控制器施加转矩，“负载与摩擦”含该负载转矩做的功。")
    if not data.get("levels"):
        return head + L.quiet(_esc(data.get("note") or "没有稳速段"))
    motor = data["motor"]
    parts = [head]
    if mode == "unknown":
        parts.append(L.note("实验没有记录负载工况。下面按“负载驱动器上电未启动、三相绕组等效短接”计算；"
                            "实测与预测接近时，“负载与摩擦”主要是负载电机的短路铜损。"
                            "可在实验日志页的“负载工况”里指定后重新生成。"))
    parts.append(L.quiet(
        "负载驱动器上电但未启动时三相占空比停在 50%，线电压为零，负载电机三相绕组经开关管等效短接，"
        "成为一台短路发电机：T = 1.5·p·ψ²·ωe·R / (R² + ωe²L²)，吸收的功率全部变成负载电机绕组与开关管的铜损；"
        "低速时 T 近似与转速成正比（等效粘滞），所以加速段转矩反而比稳速段小。"))
    parts.append(L.table(
        ["稳速平台 rpm", "持续 s", "实测转矩 N·m", "预测 N·m", "实测/预测", "预测负载铜损 W", "负载相电流峰值 A"],
        [[_num(r["speed_rpm"], 5), _num(r["seconds"], 3), _num(r["measured_torque_nm"]),
          _num(r["predicted_brake_nm"]), _num(r["ratio"], 3), _num(r["predicted_load_loss_w"]),
          _num(r["predicted_load_current_a"], 3)] for r in data["levels"]], cls="compact"))
    ratios = [r["ratio"] for r in data["levels"] if r.get("ratio")]
    if ratios and any(abs(r - 1.0) > 0.2 for r in ratios):
        review.append("对拖负载：实测转矩与短路制动模型相差超过 20%（工况或参数需核对）")
    alt = data.get("levels_nameplate_psi") or []
    parts.append(L.source(
        f"R = {_num(motor['rs_ohm'])} Ω（附加回路电阻 {_num(motor['extra_r_ohm'])} Ω），"
        f"L = {_num(motor['ls_h'] * 1e3)} mH，ψ = {_num(motor['psi_wb'] * 1e3)} mWb（{motor['psi_basis']}），"
        f"p = {motor['pole_pairs']}；{data.get('assumption')}"
        + (f"；按数字孪生铭牌磁链 {_num(motor['nameplate_psi_wb'] * 1e3)} mWb 计算时实测/预测 = "
           + "、".join(_num(r["ratio"], 3) for r in alt) if alt else "")))
    ch = data["characteristics"]
    parts.append(L.note(
        f"<b>高速风险</b>：制动转矩在约 {_num(ch['peak_speed_rpm'], 4)} rpm 处最大（{_num(ch['peak_torque_nm'], 3)} N·m），"
        f"此时负载相电流峰值约 {_num(ch['peak_current_a'], 3)} A、发热约 {_num(ch['peak_power_w'], 3)} W；"
        f"转速更高时电流趋近 {_num(ch['limit_current_a'], 3)} A。这些电流不受任何控制器限制，"
        "发热全部在负载电机绕组与负载板开关管上；高速实验前请先让负载驱动器断电或进入转矩控制。"))
    parts.append(_fig(doc, figures, "对拖负载_转矩转速.png",
                      "短路制动模型与实测：稳速点应落在铜色线上；变速点偏离来自加减速转矩与转速测量延迟",
                      "实测转矩 = kt·iq，50 ms 块平均"))
    return "".join(parts)


def _power_block(doc: L.Doc, suite: dict, figures: dict, fields: _Fields, review: list[str]) -> str:
    power = suite["power"]
    if not power.get("ok"):
        return L.note(_esc(power.get("error")))
    data = power["data"]
    mean = data["mean_w"]
    used = data.get("parameters") or {}
    short_model = bool(used.get("load_short_model"))
    names = [("inv", "逆变器电气输入"), ("cu", "定子铜损"), ("em", "电磁功率"),
             ("kinetic", "转子动能变化（等效惯量）")]
    if short_model:
        names += [("load_cu", "负载电机短路铜损（模型）"), ("fric", "摩擦与其余负载")]
    else:
        names.append(("fric", "负载与摩擦"))
    rows = [[name, _num(mean.get(key)), _num(data["energy_j"].get(key))] for key, name in names]
    unexplained = data.get("unexplained_w")
    rows.append(["未解释（铁损、死区电压误差等）", _num(unexplained), "—"])
    if unexplained is not None and mean["inv"] and abs(unexplained) > 0.25 * abs(mean["inv"]):
        review.append(f"功率：未解释功率 {_num(unexplained)} W，超过逆变器输入的 25%")
    efficiency = data.get("motor_efficiency")
    sankey = _fig(doc, figures, "功率流.gif", "功率流随时间回放（整段均匀取 48 帧）",
                  f"打印版为统计窗平均功率（{_num(data['duration_s'], 4)} s）", print_name="功率流_平均.png")
    inertia_text = (f"等效转动惯量 J = {_num(used.get('inertia'))} + {_num(used.get('load_inertia'))}"
                    f"（负载侧）= {_num(used.get('total_inertia'))} kg·m²"
                    if used.get("load_inertia") else f"转动惯量 J = {_num(used.get('inertia'))} kg·m²（单机）")
    table = (L.table(["项目", "平均功率 W", "能量 J"], rows, cls="compact") +
             L.quiet(f"电机电磁效率 η = 电磁功率 / 逆变器输入 = "
                     f"{'—' if efficiency is None else f'{efficiency:.1%}'}；统计时长 {_num(data['duration_s'], 4)} s。"
                     f"{_esc(power['metadata'].get('note'))}。") +
             L.note(f"<b>转子动能为什么小</b>：{_esc(inertia_text)}。转轴储能 ½Jω² 在最高转速时为 "
                    f"{_num(data.get('peak_stored_j'), 3)} J，动能功率 J·ω·dω/dt 只在加减速时出现，"
                    f"本段峰值 {_num(data.get('peak_kinetic_w'), 3)} W、整段平均约为 0（加速存入、停机释放）。"
                    "对拖时负载电机转子一起转，等效惯量按两台电机加联轴器计；"
                    "要让动能一支明显，需要更高转速（储能 ∝ ω²）或更陡的加减速。", "info"))
    return "".join([sankey, table, "<h3>参数来源</h3>", _param_source(suite, fields, review),
                    _dyno_block(doc, suite.get("dyno"), figures, review)])


def _media_block(media: list[dict], folder: Path, subtitles: dict | None) -> str:
    if not media:
        return L.quiet("本实验没有录像/录音（可在实验管理页“音视频”标签开启自动录制）。")
    parts = []
    for entry in media:
        src = f"../media/{entry.get('file')}"
        exists = (folder.parent / "media" / str(entry.get("file"))).exists()
        span = (f"相对实验开始 {_num(entry.get('start_offset_s'), 5)}–{_num(entry.get('end_offset_s'), 5)} s")
        info = (f"{entry.get('file')} · {entry.get('camera') or '无摄像头'} · "
                f"{entry.get('microphone') or '无麦克风'} · {span} · 时长 {_num(entry.get('duration_s'), 4)} s")
        if not exists:
            parts.append(L.note(f"录制文件缺失：{_esc(info)}"))
            continue
        tag = "video" if entry.get("kind") == "video" else "audio"
        track = ""
        uri = (subtitles or {}).get(entry.get("file"))
        if uri and tag == "video":
            track = f'<track kind="subtitles" srclang="zh" label="实验信息" default src="{uri}">'
            info += " · 字幕：日期、转速、电流与关键事件（同名 .srt 可在播放器中加载）"
        parts.append(
            f'<figure class="screen-only"><{tag} controls preload="none" src="{html.escape(src)}">{track}</{tag}>'
            f'<figcaption>{_esc(info)}</figcaption><p class="video-hint quiet" hidden>内置预览无法解码此录像'
            f'（缺少 H.264 解码器）；请点“浏览器打开”或直接打开 <a href="{html.escape(src)}">录像文件</a>。</p></figure>'
            f'<div class="print-only file-row"><span class="file-icon">MP4</span><span>{_esc(info)}'
            f"<small>{_esc(src)}（请在 HTML 版中播放）</small></span><span></span></div>")
    return "".join(parts)


def _conclusion_block(session, ai: dict) -> str:
    value = session.conclusion or {}
    fields = (("主要观察", "observations"), ("异常与可能原因", "anomalies"),
              ("改进建议", "recommendations"), ("下一次实验计划", "next_plan"))
    if any(value.get(k) for _n, k in fields):
        status = _CONCLUSION_STATUS.get(value.get("result_status"), "待判断")
        body = L.kv([("结论状态", status)] + [(name, value.get(key) or "—") for name, key in fields] +
                    [("更新时间", value.get("updated_at") or "—")])
    else:
        body = ('<div class="conclusion"><p>操作者尚未填写结论（在实验日志页点“编辑我的结论”）。</p>'
                + "".join(f"<p class=quiet>{name}</p><div class=blank></div>" for name, _k in fields)
                + "</div>")
    parts = [f"<h3>操作者结论 {L.tag('人工填写')}</h3>", body]
    if session.notes:
        parts.append(L.quiet("实验备注：" + _esc(session.notes)))
    if ai.get("text"):
        parts += [f"<h3>AI 分析 {L.tag('需人工确认', 'ai')}</h3>",
                  L.source(f"模型 {ai.get('model')} · {ai.get('generated_at')} · 依据 log_data.json"
                           f"（SHA-256 {str(ai.get('evidence_sha256', ''))[:12]}）"),
                  f'<div class="ai-text">{_esc(ai.get("text"))}</div>']
    else:
        parts.append(f"<h3>AI 分析 {L.tag('未生成', 'ai')}</h3>" +
                     L.quiet("可在实验日志页点“AI 写结论”，由诊断助手依据本日志的数据起草；不会覆盖操作者结论。"))
    return "".join(parts)


def _provenance_block(records: list[dict], sources: list[tuple[str, dict]]) -> str:
    """同一文件与哈希只列一次，列出用到它的工具与时间窗。"""
    grouped: dict[tuple, dict] = {}
    for tool, src in sources:
        if not src:
            continue
        key = (src.get("file"), src.get("sha256"))
        item = grouped.setdefault(key, {"windows": set(), "tools": [], "samples": src.get("samples"),
                                        "rate": src.get("rate_hz")})
        item["tools"].append(tool)
        item["windows"].add(f"{_num(src.get('t0'), 5)}–{_num(src.get('t1'), 5)} s"
                            + ("（稳态段）" if "稳态" in str(src.get("selection", "")) else ""))
    blocks = []
    for (file, sha), item in grouped.items():
        blocks.append(
            f'<div class="file-row"><span class="file-icon">CSV</span><span>{_esc(file)}'
            f'<small class="hash">SHA-256 {_esc(sha)}</small>'
            f"<small>时间窗 {_esc('、'.join(sorted(item['windows'])))} · {_esc(item['samples'])} 点 @ "
            f"{_num(item['rate'], 6)} Hz</small><small>工具：{_esc('、'.join(item['tools']))}</small></span>"
            "<span></span></div>")
    calls = L.table(["工具", "参数", "结果", "耗时 ms"], [
        [r["tool"], json.dumps(r["arguments"], ensure_ascii=False),
         "成功" if r["ok"] else f"失败：{r.get('error')}", _num(r["elapsed_ms"], 4)] for r in records],
        cls="compact")
    return ("".join(blocks) or L.quiet("没有高速数据来源。")) + L.details(
        "工具调用记录（log_audit.jsonl）", calls, f"{len(records)} 次调用")


def _files_block(session_dir: Path, media: list[dict], telemetry: list, events: list) -> str:
    rows = []
    for batch in waveform_batches(session_dir):
        if batch.get("high_rate_file"):
            rel = f"../waveforms/{batch['batch']}/{batch['high_rate_file']}"
            rows.append(("CSV", f"高速数据 · 批次 {batch['batch']}", rel))
    rows.append(("LOG", f"慢遥测 {len(telemetry)} 条", "../telemetry.csv"))
    rows.append(("LOG", f"事件 {len(events)} 条", "../events.jsonl"))
    for entry in media:
        rows.append(("MP4" if entry.get("kind") == "video" else "AUD",
                     f"{entry.get('file')} · {_num(entry.get('duration_s'), 4)} s", f"../media/{entry.get('file')}"))
    rows += [("JSON", "日志数据（全部工具结果）", "log_data.json"),
             ("JSON", "工具调用审计", "log_audit.jsonl"), ("PDF", "本日志 PDF 版", LOG_PDF)]
    return "".join(
        f'<div class="file-row"><span class="file-icon">{kind}</span><span>{_esc(text)}'
        f'<small>{_esc(path)}</small></span><a class="screen-only" href="{html.escape(path)}">打开</a></div>'
        for kind, text, path in rows)


def _single_html(session, events, telemetry, suite, figures, screenshots, media, ai,
                 records, warnings, folder: Path, subtitles: dict | None = None) -> str:
    started = str(session.started_at or "")[:19].replace("T", " ")
    doc = L.Doc(f"实验日志：{session.name}", session.experiment_id,
                f"{started[:10]} · {session.operator or '—'} · 驭衡 {session.software_version or ''}".strip(),
                reader_key=session.experiment_id)
    review: list[str] = list(warnings)
    has_high = suite["statistics"]["ok"]
    stats = suite["statistics"].get("data", {}) if has_high else {}
    source = suite["statistics"].get("source") or {}
    summary = summarize_telemetry(telemetry)
    moments = _key_moments(events)
    session_dir = folder.parent

    # 先生成后面各章（它们会往 review 里补充待复核项），最后生成概览
    device_html, fields = _device_chapter(session)
    chapters: list[tuple[str, str, str, str, str]] = []   # (id, 标题, 副题, 正文, 状态)

    # 02 过程与响应
    panels = [("转速 / rpm", [("speed_target", "给定", "dash"), ("speed_actual", "实际", "solid")]),
              ("电流 / A", [("current_target", "给定", "dash"), ("current_actual", "实际", "solid")]),
              ("母线 / V · 温度 / °C", [("vdc", "母线电压", "solid"), ("temperature", "温度", "copper")])]
    process = []
    svg = L.telemetry_svg(telemetry, panels, moments)
    if svg:
        process.append(doc.svg_figure(svg, "全过程慢遥测：给定与实际同一时间轴，竖线为关键事件",
                                      "telemetry.csv，monotonic_s 为实验相对时间"))
    video = next(({"offset": m.get("start_offset_s"), "end": m.get("end_offset_s")}
                  for m in media if m.get("kind") == "video"), None)
    process.append(doc.time_explorer(telemetry, moments, video))
    if moments:
        process.append('<div class="timeline">' + "".join(
            f"<div><b>{when:.3f} s</b>{_esc(label)}</div>" for when, label in moments) + "</div>")
    metric_rows = [[_METRIC_NAMES.get(k, k), v.get("count", 0), _num(v.get("min")), _num(v.get("max")),
                    _num(v.get("mean")), _num(v.get("rms"))] for k, v in summary.get("metrics", {}).items()]
    if metric_rows:
        process += ["<h3>全过程统计</h3>", L.table(["量", "样本", "最小", "最大", "均值", "RMS"], metric_rows),
                    L.quiet(f"统计包含准备、启动、运行与停机全过程（{summary.get('samples', 0)} 条，"
                            f"{_num(summary.get('duration_s'))} s），不等同于稳态跟踪精度；"
                            f"转速平均绝对误差 {_num(summary.get('speed_mae_rpm'))} rpm。")]
    faults = _summarize_faults(telemetry)
    if faults:
        process += ["<h3>故障与告警记录</h3>", L.table(["故障", "代码", "样本数", "首次 s", "最后 s"], [
            [f["fault_text"], f"0x{int(f['fault_code']):X}", f["samples"], _num(f["first_monotonic_s"], 5),
             _num(f["last_monotonic_s"], 5)] for f in faults], cls="compact")]
    process += ["<h3>转速阶跃响应</h3>", _step_block(doc, suite.get("step"), figures) if has_high
                else _no_high_rate(suite["statistics"].get("error"))]
    chapters.append(("s2", "过程与响应", "给定与实际、关键事件、全过程统计与转速阶跃响应", "".join(process), ""))

    # 03 电流与频谱
    if has_high:
        steady = figures.get("steady_source") or {}
        spectrum = [L.facts([
            (_num(stats.get("duration_s"), 5), "s", "高速数据时长", f"{source.get('samples')} 点 @ {_num(source.get('rate_hz'), 6)} Hz"),
            (_num(stats.get("iq_tracking_error_rms_a"), 4), "A", "iq 跟踪误差 RMS", "iq 给定 − iq，整段"),
            (_num(stats.get("electrical_frequency_hz"), 5), "Hz", "电频率", "由电角度斜率计算")])]
        if steady:
            spectrum.append(L.quiet(f"频谱与矢量分解只用{_esc(steady.get('selection'))} "
                                    f"{_num(steady.get('t0'), 5)}–{_num(steady.get('t1'), 5)} s：启停过程会让频谱展宽、"
                                    "让按电角度锁定的阶次分解失去意义；时频图与阶跃响应用整段。"))
        spectrum += [
            _fig(doc, figures, "频谱.png", "稳态段频谱：上 iq 单边谱；下 电流复数 iα+jiβ 双边谱（铜色虚线为电频率倍数）",
                 "Hann 窗、去均值，幅值为相干增益校正后的峰值 A，纵轴 20·log₁₀ 相对满格"),
            _fig(doc, figures, "滑动频谱.gif", "iq 频谱随时间变化（0.5 s 窗口从头滑到尾）",
                 "打印版为中间一帧", print_name="滑动频谱_打印.png"),
            _fig(doc, figures, "时频_iq.png", "iq STFT 时频图（整段，含启停过程）"),
            _fig(doc, figures, "时频_转速.png", "转速 STFT 时频图（整段，降采样到 1 kHz）"),
            "<h3>时域统计（整段高速数据）</h3>",
            L.table(["信号", "均值", "RMS", "标准差", "最小", "最大"], _stats_rows(stats), cls="compact"),
            L.cols("<h3>iq 频谱峰值（稳态段）</h3>" + _peaks_table(suite["iq_spectrum"], False),
                   "<h3>电流复数频谱峰值</h3>" + _peaks_table(suite["complex_spectrum"], True)),
            L.source(f"{source.get('file')}（SHA-256 {str(source.get('sha256', ''))[:16]}…）")]
        chapters.append(("s3", "电流与频谱", "高速采样 16 kHz：时域统计、稳态频谱、时频图", "".join(spectrum), ""))
        vector_html, vector_status = _vector_block(doc, suite["vector"], stats, figures, review)
        chapters.append(("s4", "矢量与分解", "αβ 电流圆、分量合成动图、分量分解与畸变诊断",
                         vector_html, vector_status))
        chapters.append(("s5", "功率与假设", "能量链路平均功率、参数来源与对拖负载模型",
                         _power_block(doc, suite, figures, fields, review), L.tag("模型估算", "review")))
    else:
        notice = _no_high_rate(suite["statistics"].get("error"))
        chapters += [("s3", "电流与频谱", "需要高速数据", notice, ""),
                     ("s4", "矢量与分解", "需要高速数据", notice, ""),
                     ("s5", "功率与假设", "需要高速数据", notice, "")]

    chapters.append(("s6", "设备与参数", "实验开始时冻结的设备身份、控制与保护参数", device_html, ""))

    # 07 事件时间线
    template = session.template_snapshot or {}
    results = _workflow_results(events)
    timeline = []
    if template.get("steps"):
        timeline += ["<h3>实验方案步骤</h3>", L.table(["#", "步骤", "属性", "结果", "备注"], [
            [i, s.get("title", ""), "必做" if s.get("required", True) else "可选",
             results.get(s.get("step_id"), {}).get("status", "未完成"),
             results.get(s.get("step_id"), {}).get("note", "—")]
            for i, s in enumerate(template["steps"], 1)], numeric_from=9)]
    t_first = next((float(e["monotonic_s"]) for e in events if e.get("monotonic_s") is not None), 0.0)
    timeline.append(L.table(["相对时间 s", "事件", "摘要"], [
        [_num((e.get("monotonic_s") or 0.0) - t_first, 6), e.get("message") or e.get("type"), _event_summary(e)]
        for e in events], cls="event-table", numeric_from=9) if events else L.quiet("无事件。"))
    timeline.append(L.details("原始事件载荷（events.jsonl）", L.pre_json(events), f"{len(events)} 条"))
    chapters.append(("s7", "事件时间线", f"{len(events)} 条事件，时间以实验开始为零点", "".join(timeline), ""))

    # 08 波形与影音
    wave = [doc.figure(f"log_assets/{path.name}", f"保存波形时的监控页截图（批次 {batch}，点击查看原尺寸）")
            for path, batch in screenshots] or [L.quiet("没有保存波形截图。")]
    wave += ["<h3>音视频</h3>", _media_block(media, folder, subtitles)]
    chapters.append(("s8", "波形与影音", "保存波形时的截图与实验录像", "".join(wave), ""))

    ai_state = L.tag("AI 草稿待确认", "ai") if ai.get("text") else ""
    chapters.append(("s9", "结论", "操作者结论与 AI 分析分开记录", _conclusion_block(session, ai), ai_state))
    chapters.append(("s10", "附件与来源", "记录文件、数据来源与计算工具",
                     "<h3>记录文件</h3>" + _files_block(session_dir, media, telemetry, events) +
                     "<h3>数据来源与计算</h3>" +
                     L.quiet("第 02–08 章的数值全部由下列文件经只读工具计算；同一文件只列一次。") +
                     _provenance_block(records, [(name, suite[name].get("source"))
                                                 for name in suite if isinstance(suite[name], dict)]),
                     ""))

    # 01 概览（最后生成：需要汇总各章的待复核项）
    status_text, status_kind = _STATUS_TEXT.get(session.status.value, (session.status.value, ""))
    conclusion_set = any((session.conclusion or {}).get(k) for k in ("observations", "anomalies",
                                                                      "recommendations", "next_plan"))
    tags = L.tag(status_text, status_kind) + (
        L.tag("操作者结论已填写") if conclusion_set else L.tag("技术结论待填写", "review"))
    duration = _duration_s(session, telemetry)
    target = (session.controller_params or {}).get("target_speed_rpm")
    batches = waveform_batches(session_dir)
    ended = str(session.ended_at or "")[11:19]
    hero = (f'<div class="hero">{L.MOTIF_SVG}<p class="eyebrow">Yuheng · Experiment Journal</p>'
            f"<h1>{_esc(session.name)}</h1><p class=cn-title>{_esc(session.purpose or '（未填写实验目的）')}</p>"
            f'<p class="meta-line">{_esc(fields.values.get("device.name"))} · '
            f'{_esc(fields.values.get("device.motor_type"))} · {_esc((session.controller_params or {}).get("control_mode"))}'
            f"<br>{_esc(started)} – {_esc(ended)} · 操作者 {_esc(session.operator)} · 数据来源 {_esc(session.data_source)}</p>"
            f'<div class="tags">{tags}</div></div>')
    overview = [hero, L.facts([
        (_num(duration, 4), "s", "实验总时长", "含准备、运行与归档"),
        (_num(target, 5), "rpm", "目标转速", "控制参数快照"),
        (f"{source.get('samples'):,}" if has_high and source.get("samples") else "—", "",
         "高速采样点", f"{_num(source.get('rate_hz', 0) / 1000, 3)} kHz · {_num(stats.get('duration_s'), 4)} s"
         if has_high else "未保存高速数据")])]
    speed_svg = L.telemetry_svg(telemetry, [("转速 / rpm", [("speed_target", "给定", "dash"),
                                                           ("speed_actual", "实际", "solid")])],
                                moments, panel_h=150)
    if speed_svg:
        overview.append(doc.svg_figure(speed_svg, "全过程转速：虚线为给定，实线为实际", "telemetry.csv"))
    review_text = "；".join(_esc(r) for r in review) if review else "无"
    overview.append(L.summary_rows([
        ("记录完整性", _esc(f"{len(events)} 条事件、{len(batches)} 批波形"
                           f"（{sum(1 for b in batches if b.get('high_rate_file'))} 批含高速数据）、"
                           f"{len(media)} 段录像；{status_text}")),
        ("阅读顺序", "先看过程与响应，再看电流频谱与矢量；功率一章写明了计算参数与假设。"),
        ("待复核项", f'<span class="review-cell">{review_text}</span>' if review else "无")]))
    overview_body = "".join(overview)

    # 按顺序组装：概览编号为 01，所以先登记概览，再登记其余章节
    doc.chapters.clear()
    sections = [doc.chapter("s1", "实验概览", f"{session.experiment_id} · {template.get('name', '自由实验')}",
                            overview_body + "__TOC__" + L.quiet(
                                "固定模板：数据部分由驭衡根据实验档案与保存的原始数据自动计算，未经人工或 AI 修改；"
                                "每个数值的来源见第 10 章。"))]
    for sid, title, deck, body, status in chapters:
        sections.append(doc.chapter(sid, title, deck, body, status))
    sections[0] = sections[0].replace("__TOC__", doc.toc())
    return doc.render(sections)


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
    step = suite.get("step", {})
    if step.get("ok") and step["data"].get("step_found"):
        response = step["data"]["metrics"]
        out["阶跃上升时间 s"] = response.get("rise_s")
        out["阶跃超调 %"] = response.get("overshoot_pct")
        out["调节时间 ±2% s"] = response.get("settling_s")
        out["稳态误差 rpm"] = response.get("steady_error")
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


def _delta(base, value) -> L.Raw:
    if base is None or value is None:
        return L.Raw("—")
    diff = value - base
    pct = f"（{diff / abs(base) * 100:+.1f}%）" if abs(base) > 1e-12 else ""
    color = "#a55c3a" if diff > 0 else "#39786b" if diff < 0 else "inherit"
    return L.Raw(f'<span style="color:{color}">{diff:+.4g}{pct}</span>')


def _comparison_html(runs, figures, user, ai, records, warnings) -> str:
    base = runs[0]
    title = f"对比实验日志：{base['session'].name} vs " + "、".join(r["session"].name for r in runs[1:])
    label = f"{base['id']} 对比 {len(runs) - 1} 组"
    doc = L.Doc(title, label, datetime.now().strftime("%Y-%m-%d") + " · 对比实验",
                reader_key="compare:" + "+".join(r["id"] for r in runs))
    changed_total = sum(len((r.get("params", {}).get("data") or {}).get("changed", [])) for r in runs[1:])
    overview = [
        f'<div class="hero">{L.MOTIF_SVG}<p class="eyebrow">Yuheng · Comparison Journal</p>'
        f"<h1>对比实验</h1><p class=cn-title>{_esc(title)}</p></div>",
        L.facts([(str(len(runs)), "组", "参与对比的实验", f"基准 {base['id']}"),
                 (str(changed_total), "项", "参数差异", "相对基准，只计不同项"),
                 (str(sum(1 for r in runs if r["suite"]["statistics"]["ok"])), "组", "含高速数据", "可做频谱与矢量对比")]),
        L.table(["角色", "实验编号", "名称", "状态", "开始时间", "高速数据时间窗"], [[
            "基准" if i == 0 else f"对照 {i}", r["id"], r["session"].name, r["session"].status.value,
            str(r["session"].started_at or "")[:19].replace("T", " "),
            (f"{_num(r['suite']['statistics']['source']['t0'], 6)}–{_num(r['suite']['statistics']['source']['t1'], 6)} s")
            if r["suite"]["statistics"]["ok"] else "无高速数据"] for i, r in enumerate(runs)], numeric_from=9)]
    if warnings:
        overview.append(L.note("；".join(_esc(w) for w in warnings)))
    conditions = []
    for run in runs[1:]:
        result = run.get("params", {})
        changed = result.get("data", {}).get("changed", []) if result.get("ok") else []
        conditions.append(f"<h3>{_esc(run['id'])} 相对基准</h3>")
        conditions.append(L.table(["参数", "基准", "本实验"], [
            [c["parameter"], c["baseline"], c["value"]] for c in changed], numeric_from=9)
            if changed else L.quiet("参数快照与基准完全相同。"))
    metrics = [comparison_metrics(run) for run in runs]
    headers = ["指标", f"基准 {base['id']}"]
    for run in runs[1:]:
        headers += [run["id"], "变化"]
    rows = []
    for key in dict.fromkeys(key for m in metrics for key in m):   # 某些实验才有的指标（如阶跃）也列出
        row = [key, _num(metrics[0].get(key))]
        for m in metrics[1:]:
            row += [_num(m.get(key)), _delta(metrics[0].get(key), m.get(key))]
        rows.append(row)
    result_html = (L.table(headers, rows, cls="compact") +
                   L.quiet("变化 = 对照 − 基准（括号内为相对基准的百分比）；铜色为增大，青绿为减小，"
                           "好坏需结合指标含义判断（如误差、纹波减小为改善）。"))
    curves = []
    for key, caption in (("speed", "转速叠加（各自记录的相对时间）"), ("iq", "iq 叠加（20 ms 滑动平均）"),
                         ("spectrum", "iq 频谱叠加（稳态段）")):
        if figures.get(key):
            curves.append(doc.figure(f"log_assets/{figures[key].name}", caption))
    for run in runs:
        curves.append(f"<h3>{_esc(run['id'])} iq 频谱峰值</h3>" +
                      _peaks_table(run["suite"].get("iq_spectrum", {"ok": False, "error": "无数据"}), False))
    labels: dict[str, str] = {}
    table_rows: dict[str, list] = {}
    for index, run in enumerate(runs):
        vector = run["suite"].get("vector", {})
        for comp in vector.get("data", {}).get("components", []) if vector.get("ok") else []:
            labels.setdefault(comp["key"], comp["label"])
            table_rows.setdefault(comp["key"], [None] * len(runs))[index] = comp["pct_of_fundamental"]
    vector_html = L.table(["分量"] + [r["id"] for r in runs], [
        [labels[key]] + [("—" if v is None else f"{v:.2f}%") for v in values]
        for key, values in table_rows.items()], cls="compact")
    gifs = [doc.figure(f"log_assets/{path.name}", f"{name} 分量合成") for path, name in figures["vector"]]
    vector_html += "".join(L.cols(*gifs[i:i + 2]) for i in range(0, len(gifs), 2))
    status = _CONCLUSION_STATUS.get(user.get("result_status"), "待判断")
    conclusion = [f"<h3>操作者结论 {L.tag('人工填写')}</h3>",
                  L.kv([("结论状态", status), ("主要观察", user.get("observations") or "—"),
                        ("异常与可能原因", user.get("anomalies") or "—"),
                        ("改进建议", user.get("recommendations") or "—"),
                        ("下一次实验计划", user.get("next_plan") or "—")]) if user
                  else L.quiet("结论待填写（在实验日志页点“编辑我的结论”）。")]
    if ai.get("text"):
        conclusion += [f"<h3>AI 分析 {L.tag('需人工确认', 'ai')}</h3>",
                       L.source(f"模型 {ai.get('model')} · {ai.get('generated_at')}"),
                       f'<div class="ai-text">{_esc(ai.get("text"))}</div>']
    sources = []
    for run in runs:
        for name, result in run["suite"].items():
            if isinstance(result, dict) and result.get("source"):
                sources.append((f"{run['id']} {name}", result["source"]))
    sections = [doc.chapter("c1", "对比概况", "参与对比的实验与数据窗口", "".join(overview) + "__TOC__"),
                doc.chapter("c2", "条件差异", "改了什么：只列与基准不同的参数", "".join(conditions)),
                doc.chapter("c3", "结果差值", "同一指标：基准、对照与差值", result_html),
                doc.chapter("c4", "曲线与频谱", "对齐后的转速、iq 与频谱叠加", "".join(curves)),
                doc.chapter("c5", "矢量分解对比", "各分量占基波百分比与合成动图", vector_html),
                doc.chapter("c6", "结论", "操作者结论与 AI 分析分开记录", "".join(conclusion)),
                doc.chapter("c7", "数据来源", "同一文件只列一次", _provenance_block(records, sources))]
    sections[0] = sections[0].replace("__TOC__", doc.toc())
    return doc.render(sections)


def _dyno_applies(data: dict) -> bool:
    """短路制动模型只对“上电未启动”或未记录的对拖工况有意义。"""
    return data.get("load_mode") in ("shorted", "unknown") and bool(data.get("levels"))


def _dyno_figure(data: dict, whole):
    from core import dyno_load
    motor_data = data["motor"]
    motor = dyno_load.LoadMotor(motor_data["rs_ohm"], motor_data["ls_h"], motor_data["psi_wb"],
                                int(motor_data["pole_pairs"]), motor_data["extra_r_ohm"])
    kt = 1.5 * motor.pole_pairs * motor.psi_wb
    compare = dyno_load.compare_with_measurement(whole.time, whole.speed_rpm,
                                                 whole.extra["iq_a"], kt, motor)
    top_rpm = max(3000.0, 1.3 * data["characteristics"]["peak_speed_rpm"],
                  1.2 * float(np.max(whole.speed_rpm)))
    rpm = np.linspace(0.0, top_rpm, 240)
    model = dyno_load.short_circuit_brake(rpm * 2 * np.pi / 60.0, motor)
    return render.torque_speed_image(rpm, model["torque_nm"], model["current_peak_a"],
                                     compare["blocks"], compare["steady_mask"])


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
def pdf_page_layout():
    """A4，左右 12.7 mm、上下 15 mm。QtWebEngine 的页边距以这里为准（CSS @page 的 margin
    不生效），页眉、页码由日志 CSS 的 @page 页边框画在这段页边距里。"""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize
    return QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                       QMarginsF(12.7, 15.0, 12.7, 15.0), QPageLayout.Millimeter)


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
        page.printToPdf(str(pdf_path), pdf_page_layout())

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
