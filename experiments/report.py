"""从冻结实验档案生成可复现的Markdown/HTML报告和SVG曲线。"""
from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ExperimentSession
from .repository import ExperimentRepository
from .telemetry import summarize_telemetry


_METRIC_NAMES = {
    "speed_actual": "实际转速 (rpm)",
    "current_actual": "实际电流 (A)",
    "torque_actual": "实际转矩 (N·m)",
    "vdc": "母线电压 (V)",
    "temperature": "温度 (°C)",
}


@dataclass(frozen=True)
class ReportPaths:
    markdown: Path
    html: Path
    svg: Path


class ExperimentReportGenerator:
    def __init__(self, repository: ExperimentRepository) -> None:
        self.repository = repository

    def generate(self, experiment_id: str) -> ReportPaths:
        session = self.repository.load(experiment_id)
        events = self.repository.read_events(experiment_id)
        telemetry = self.repository.read_telemetry(experiment_id)
        summary = summarize_telemetry(telemetry)
        summary["faults"] = _summarize_faults(telemetry)
        report_dir = self.repository.session_dir(experiment_id) / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        svg_path = report_dir / "telemetry.svg"
        markdown_path = report_dir / "report.md"
        html_path = report_dir / "report.html"
        svg = _build_svg(telemetry, events)
        _write_text(svg_path, svg)
        _write_text(markdown_path, _build_markdown(
            session, events, summary, bool(telemetry)))
        _write_text(html_path, _build_html(
            session, events, summary, svg, bool(telemetry)))
        return ReportPaths(markdown_path, html_path, svg_path)


def _build_markdown(session: ExperimentSession, events: list[dict[str, Any]],
                    summary: dict[str, Any], has_telemetry: bool) -> str:
    device = session.device.to_dict() if session.device else {}
    template = session.template_snapshot
    lines = [
        f"# 实验报告：{session.name}", "",
        "> 由 Motor-Control-HMI 根据冻结实验档案自动生成。报告不修改原始数据。", "",
        "## 1. 实验概况", "",
        "| 项目 | 内容 |", "|---|---|",
        f"| 实验编号 | {_md(session.experiment_id)} |",
        f"| 状态 | {_md(session.status.value)} |",
        f"| 实验目的 | {_md(session.purpose or '—')} |",
        f"| 操作者 | {_md(session.operator or '—')} |",
        f"| 数据来源 | {_md(session.data_source)} |",
        f"| 创建/开始/结束 | {_md(session.created_at)} / {_md(session.started_at or '—')} / {_md(session.ended_at or '—')} |",
        f"| 结束原因 | {_md(session.end_reason or '—')} |",
        f"| 软件版本 | {_md(session.software_version or '—')} |",
        f"| 实验模板 | {_md(template.get('name', '自由实验'))} v{template.get('version', '—')} |",
        "", "## 2. 设备与参数快照", "",
        "### 设备", "", "```json",
        json.dumps(device, ensure_ascii=False, indent=2), "```", "",
        "### 控制参数", "", "```json",
        json.dumps(session.controller_params, ensure_ascii=False, indent=2), "```", "",
        "### 保护参数与模板边界", "", "```json",
        json.dumps({"protection_params": session.protection_params,
                    "template_safety_limits": template.get("safety_limits", {})},
                   ensure_ascii=False, indent=2), "```", "",
        "## 3. 工作流执行结果", "",
    ]
    lines.extend(_workflow_markdown(session, events))
    lines.extend(["", "## 4. 遥测统计", ""])
    lines.extend(_summary_markdown(summary))
    lines.extend(["", "### 遥测曲线", ""])
    lines.append("![遥测曲线](telemetry.svg)" if has_telemetry else "本实验没有遥测数据。")
    lines.extend(["", "## 5. 状态转换、故障与通信", ""])
    lines.extend(_context_markdown(session, events, summary))
    lines.extend(["", "## 6. 完整事件时间线", "",
                  "| 相对时间 (s) | 时间 | 类型 | 事件 | 详情 |",
                  "|---:|---|---|---|---|"])
    if events:
        for event in events:
            lines.append(
                f"| {_fmt(event.get('monotonic_s'))} | {_md(event.get('timestamp', ''))} | "
                f"{_md(event.get('type', ''))} | {_md(event.get('message', ''))} | "
                f"{_md(_detail_text(event.get('details', {})))} |")
    else:
        lines.append("| — | — | — | 无事件记录 | — |")
    lines.extend(["", "## 7. 实验结论", ""])
    lines.extend(_conclusion_markdown(session))
    lines.append("")
    return "\n".join(lines)


def _build_html(session: ExperimentSession, events: list[dict[str, Any]],
                summary: dict[str, Any], svg: str, has_telemetry: bool) -> str:
    device = session.device.to_dict() if session.device else {}
    template = session.template_snapshot
    workflow_rows = []
    results = _workflow_results(events)
    for index, step in enumerate(template.get("steps", []), 1):
        result = results.get(step.get("step_id"), {})
        workflow_rows.append([
            str(index), step.get("title", ""), "必做" if step.get("required", True) else "可选",
            step.get("required_runtime_state") or "—", result.get("status", "未完成"),
            result.get("note", "—"),
        ])
    metric_rows = []
    for field, values in summary.get("metrics", {}).items():
        metric_rows.append([
            _METRIC_NAMES.get(field, field), str(values.get("count", 0)),
            _fmt(values.get("min")), _fmt(values.get("max")),
            _fmt(values.get("mean")), _fmt(values.get("rms")),
            _fmt(values.get("peak_to_peak")),
        ])
    event_rows = [[
        _fmt(item.get("monotonic_s")), item.get("timestamp", ""),
        item.get("type", ""), item.get("message", ""),
        _detail_text(item.get("details", {})),
    ] for item in events]
    context = session.runtime_context
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(session.name)}</title>
<style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;max-width:1120px;margin:32px auto;padding:0 24px;color:#243447;line-height:1.6}}
h1,h2,h3{{color:#123a5a}} table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}
th,td{{border:1px solid #cbd5df;padding:7px 9px;text-align:left;vertical-align:top}}th{{background:#edf4f8}}
pre{{background:#f4f7f9;padding:14px;overflow:auto;border-radius:5px}}.muted{{color:#60778b}}.chart{{border:1px solid #d7e1e8;padding:8px}}
</style></head><body>
<h1>实验报告：{html.escape(session.name)}</h1><p class="muted">由 Motor-Control-HMI 根据冻结实验档案自动生成。报告不修改原始数据。</p>
<h2>1. 实验概况</h2>{_html_table(["项目","内容"], [
 ["实验编号",session.experiment_id],["状态",session.status.value],["实验目的",session.purpose or "—"],
 ["操作者",session.operator or "—"],["数据来源",session.data_source],
 ["开始/结束",f"{session.started_at or '—'} / {session.ended_at or '—'}"],
 ["结束原因",session.end_reason or "—"],["模板",template.get("name","自由实验")]])}
<h2>2. 设备与参数快照</h2><h3>设备</h3>{_pre(device)}<h3>控制参数</h3>{_pre(session.controller_params)}
<h3>保护参数与模板边界</h3>{_pre({"protection_params":session.protection_params,"template_safety_limits":template.get("safety_limits",{})})}
<h2>3. 工作流执行结果</h2>{_html_table(["#","步骤","属性","要求状态","结果","备注"], workflow_rows) if workflow_rows else '<p>自由实验，没有模板步骤。</p>'}
<h2>4. 遥测统计</h2><p>样本数：{summary.get('samples',0)}；持续时间：{_fmt(summary.get('duration_s'))} s；转速MAE：{_fmt(summary.get('speed_mae_rpm'))} rpm</p>
{_html_table(["指标","样本","最小","最大","均值","RMS","峰峰值"],metric_rows)}
<h3>遥测曲线</h3>{f'<div class="chart">{svg}</div>' if has_telemetry else '<p>本实验没有遥测数据。</p>'}
<h2>5. 运行与通信上下文</h2>{_pre({"runtime_context":context,"telemetry_faults":summary.get("faults",[])})}
<h2>6. 完整事件时间线</h2>{_html_table(["相对时间(s)","时间","类型","事件","详情"],event_rows)}
<h2>7. 实验结论</h2>{_conclusion_html(session)}
</body></html>"""


def _workflow_results(events: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    results = {}
    for event in events:
        details = event.get("details", {})
        step_id = details.get("step_id") if isinstance(details, dict) else None
        if not step_id:
            continue
        if event.get("type") == "workflow_step_confirmed":
            results[step_id] = {"status": "已确认", "note": details.get("note") or "—"}
        elif event.get("type") == "workflow_step_skipped":
            results[step_id] = {"status": "已跳过", "note": details.get("reason") or "—"}
    return results


def _workflow_markdown(session: ExperimentSession,
                       events: list[dict[str, Any]]) -> list[str]:
    steps = session.template_snapshot.get("steps", [])
    if not steps:
        return ["自由实验，没有模板步骤。"]
    results = _workflow_results(events)
    lines = ["| # | 步骤 | 属性 | 要求状态 | 结果 | 备注 |",
             "|---:|---|---|---|---|---|"]
    for index, step in enumerate(steps, 1):
        result = results.get(step.get("step_id"), {})
        lines.append(
            f"| {index} | {_md(step.get('title', ''))} | "
            f"{'必做' if step.get('required', True) else '可选'} | "
            f"{_md(step.get('required_runtime_state') or '—')} | "
            f"{_md(result.get('status', '未完成'))} | {_md(result.get('note', '—'))} |")
    return lines


def _summary_markdown(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"- 样本数：{summary.get('samples', 0)}",
        f"- 持续时间：{_fmt(summary.get('duration_s'))} s",
        f"- 转速平均绝对跟踪误差：{_fmt(summary.get('speed_mae_rpm'))} rpm", "",
        "| 指标 | 样本 | 最小 | 最大 | 均值 | RMS | 峰峰值 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for field, values in summary.get("metrics", {}).items():
        lines.append(
            f"| {_METRIC_NAMES.get(field, field)} | {values.get('count', 0)} | "
            f"{_fmt(values.get('min'))} | {_fmt(values.get('max'))} | "
            f"{_fmt(values.get('mean'))} | {_fmt(values.get('rms'))} | "
            f"{_fmt(values.get('peak_to_peak'))} |")
    return lines


def _context_markdown(session: ExperimentSession,
                      events: list[dict[str, Any]],
                      summary: dict[str, Any]) -> list[str]:
    transitions = [item for item in events
                   if item.get("type") == "operation" and
                   item.get("message") == "运行状态切换"]
    faults = []
    for item in events:
        text = f"{item.get('message', '')} {_detail_text(item.get('details', {}))}"
        if any(word in text for word in ("故障", "过压", "急停", "超时", "错误")):
            faults.append(item)
    lines = ["### 开始/结束上下文", "", "```json",
             json.dumps(session.runtime_context, ensure_ascii=False, indent=2), "```", "",
             "### 状态转换", ""]
    lines.extend([f"- {_md(item.get('timestamp', ''))}：{_md(_detail_text(item.get('details', {})))}"
                  for item in transitions] or ["- 无状态转换记录"])
    lines.extend(["", "### 故障与异常", ""])
    telemetry_faults = summary.get("faults", [])
    if telemetry_faults:
        lines.append("遥测故障汇总：")
        for fault in telemetry_faults:
            lines.append(
                f"- code=0x{int(fault.get('fault_code', 0)):X}；"
                f"{_md(fault.get('fault_text', ''))}；样本={fault.get('samples', 0)}；"
                f"区间={_fmt(fault.get('first_monotonic_s'))}–"
                f"{_fmt(fault.get('last_monotonic_s'))} s")
        lines.append("")
    lines.extend([f"- {_md(item.get('timestamp', ''))}：{_md(item.get('message', ''))} {_md(_detail_text(item.get('details', {})))}"
                  for item in faults] or ["- 未记录故障或异常事件"])
    return lines


def _summarize_faults(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        code_value = _num(row.get("fault_code"))
        code = int(code_value or 0)
        text = str(row.get("fault_text") or "").strip()
        if code == 0 and not text:
            continue
        key = (code, text)
        item = grouped.setdefault(key, {
            "fault_code": code, "fault_text": text or f"故障码0x{code:X}",
            "samples": 0, "first_monotonic_s": row.get("monotonic_s"),
            "last_monotonic_s": row.get("monotonic_s"),
        })
        item["samples"] += 1
        item["last_monotonic_s"] = row.get("monotonic_s")
    return list(grouped.values())


def _build_svg(rows: list[dict[str, Any]], events: list[dict[str, Any]] | None = None,
               max_points: int = 1000) -> str:
    width, height = 1000, 620
    if not rows:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="180" '
                f'viewBox="0 0 {width} 180"><rect width="100%" height="100%" fill="#fff"/>'
                '<text x="500" y="90" text-anchor="middle" fill="#60778b">无遥测数据</text></svg>')
    if len(rows) > max_points:
        last = len(rows) - 1
        rows = [rows[round(i * last / (max_points - 1))] for i in range(max_points)]
    times = [_num(row.get("monotonic_s")) for row in rows]
    if not any(value is not None for value in times):
        times = [float(index) for index in range(len(rows))]
    else:
        times = [value if value is not None else float(index)
                 for index, value in enumerate(times)]
    t_min, t_max = min(times), max(times)
    if t_max == t_min:
        t_max = t_min + 1.0
    panels = [
        ("转速 (rpm)", [("speed_actual", "实际", "#1976d2"),
                        ("speed_target", "给定", "#ef6c00")]),
        ("电流 (A)", [("current_actual", "实际", "#00897b"),
                      ("current_target", "给定", "#8e24aa")]),
        ("母线/温度", [("vdc", "Vdc (V)", "#d32f2f"),
                       ("temperature", "温度 (°C)", "#455a64")]),
    ]
    left, right, top, panel_h, gap = 72, 24, 26, 165, 25
    plot_w = width - left - right
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             '<style>text{font-family:Arial,"Microsoft YaHei";font-size:12px;fill:#425b6d}.grid{stroke:#dfe7ec;stroke-width:1}.axis{stroke:#78909c;stroke-width:1}</style>']
    markers = []
    for event in events or []:
        if event.get("type") != "experiment_marker":
            continue
        timestamp = _num(event.get("monotonic_s"))
        if timestamp is not None:
            markers.append((timestamp, str(event.get("message", "事件")),
                            str(event.get("details", {}).get("category", "custom"))))
    for panel_index, (title, series) in enumerate(panels):
        y0 = top + panel_index * (panel_h + gap)
        values = [_num(row.get(field)) for row in rows for field, _, _ in series]
        valid = [value for value in values if value is not None]
        y_min, y_max = (min(valid), max(valid)) if valid else (0.0, 1.0)
        if y_max == y_min:
            margin = max(1.0, abs(y_max) * 0.05)
            y_min, y_max = y_min - margin, y_max + margin
        parts.append(f'<text x="{left}" y="{y0 - 8}" font-weight="bold">{html.escape(title)}</text>')
        for grid in range(5):
            y = y0 + grid * panel_h / 4
            value = y_max - grid * (y_max - y_min) / 4
            parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}"/>')
            parts.append(f'<text x="{left - 7}" y="{y + 4:.1f}" text-anchor="end">{value:.3g}</text>')
        parts.append(f'<line class="axis" x1="{left}" y1="{y0}" x2="{left}" y2="{y0 + panel_h}"/>')
        for timestamp, message, category in markers:
            if not t_min <= timestamp <= t_max:
                continue
            x = left + (timestamp - t_min) / (t_max - t_min) * plot_w
            color = _SVG_MARKER_COLORS.get(category, "#f9a825")
            parts.append(f'<line x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{y0 + panel_h}" stroke="{color}" stroke-width="1.2" stroke-dasharray="5 4"/>')
            if panel_index == 0:
                label = html.escape(message[:16])
                parts.append(f'<text x="{x + 3:.1f}" y="{y0 + 13}" fill="{color}" transform="rotate(90 {x + 3:.1f} {y0 + 13})">{label}</text>')
        for series_index, (field, label, color) in enumerate(series):
            points = []
            for timestamp, row in zip(times, rows):
                value = _num(row.get(field))
                if value is None:
                    continue
                x = left + (timestamp - t_min) / (t_max - t_min) * plot_w
                y = y0 + (y_max - value) / (y_max - y_min) * panel_h
                points.append(f"{x:.1f},{y:.1f}")
            if points:
                parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.7" points="{" ".join(points)}"/>')
            legend_x = left + 690 + series_index * 120
            parts.append(f'<line x1="{legend_x}" y1="{y0 - 12}" x2="{legend_x + 22}" y2="{y0 - 12}" stroke="{color}" stroke-width="2"/>')
            parts.append(f'<text x="{legend_x + 27}" y="{y0 - 8}">{html.escape(label)}</text>')
    parts.append(f'<text x="{width / 2}" y="{height - 10}" text-anchor="middle">相对时间 {t_min:.2f}–{t_max:.2f} s</text>')
    parts.append("</svg>")
    return "".join(parts)


def _html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join("<tr>" + "".join(
        f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


_CONCLUSION_STATUS = {
    "pending": "待判断", "passed": "达到目的",
    "partial": "部分达到", "failed": "未达到/失败",
}
_SVG_MARKER_COLORS = {
    "target_changed": "#1e88e5", "load_applied": "#fb8c00",
    "load_removed": "#43a047", "parameter_changed": "#00897b",
    "oscillation": "#8e24aa",
    "abnormal_sound": "#d81b60", "protection": "#e53935",
    "custom": "#f9a825",
}


def _conclusion_markdown(session: ExperimentSession) -> list[str]:
    value = session.conclusion
    if not value:
        return [session.notes.strip() or
                "- 结论状态：待判断\n- 主要观察：待填写\n- 异常与原因：待填写\n- 下一步：待填写"]
    return [
        f"- 结论状态：{_CONCLUSION_STATUS.get(value.get('result_status'), '待判断')}",
        f"- 主要观察：{_md(value.get('observations') or '—')}",
        f"- 异常与可能原因：{_md(value.get('anomalies') or '—')}",
        f"- 改进建议：{_md(value.get('recommendations') or '—')}",
        f"- 下一次实验计划：{_md(value.get('next_plan') or '—')}",
        f"- 原始实验备注：{_md(session.notes or '—')}",
        f"- 更新时间：{_md(value.get('updated_at') or '—')}",
    ]


def _conclusion_html(session: ExperimentSession) -> str:
    value = session.conclusion
    if not value:
        return f"<p>{html.escape(session.notes.strip() or '结论待填写')}</p>"
    return _html_table(["项目", "内容"], [
        ["结论状态", _CONCLUSION_STATUS.get(value.get("result_status"), "待判断")],
        ["主要观察", value.get("observations") or "—"],
        ["异常与可能原因", value.get("anomalies") or "—"],
        ["改进建议", value.get("recommendations") or "—"],
        ["下一次实验计划", value.get("next_plan") or "—"],
        ["原始实验备注", session.notes or "—"],
        ["更新时间", value.get("updated_at") or "—"],
    ])


def _pre(value: Any) -> str:
    return "<pre>" + html.escape(json.dumps(value, ensure_ascii=False, indent=2)) + "</pre>"


def _detail_text(details: Any) -> str:
    if not details:
        return ""
    if isinstance(details, dict):
        return "；".join(f"{key}={value}" for key, value in details.items())
    return str(details)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")


def _fmt(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.6g}"


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _write_text(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        if not content.endswith("\n"):
            stream.write("\n")
    temp.replace(path)
