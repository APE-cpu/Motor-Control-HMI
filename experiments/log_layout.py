"""实验日志版式：纸面样式、章节外壳、图号、表格与折叠原始记录。

参照 GPT 的版式样稿 R1（报告排版样稿/EXP-20260928-005）：暖白纸面、铜色章节号、
青绿数据线、只保留横向细分隔的表格、图号与来源随图走、原始字段折叠查看。

与样稿不同的地方：样稿每章是固定高度的 A4 页（overflow:hidden，只对那一份记录逐页核对过），
通用生成器改为内容自然流动——每章从新页开始，长表格自动跨页并重复表头，页眉、页码
（第 n / N 页）由 CSS @page 页边框生成，不会裁掉更长的参数表或更多批次的数据。
"""
from __future__ import annotations

import html
import json
import math
from typing import Any, Iterable

import numpy as np

INK, MUTED, LINE, TEAL, COPPER = "#24312f", "#74817b", "#dce2dc", "#39786b", "#af6948"


def esc(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value))


def num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}g}"


class Raw:
    """直接输出的 HTML 片段（表格单元格用）。"""

    def __init__(self, text: str) -> None:
        self.text = text


def _cell(value: Any) -> str:
    return value.text if isinstance(value, Raw) else esc(value)


def _numeric(value: Any) -> bool:
    if isinstance(value, Raw):
        return False
    try:
        float(str(value).replace("%", "").replace("+", "").strip())
        return True
    except ValueError:
        return False


def table(headers: list[str], rows: list[list[Any]], *, cls: str = "",
          numeric_from: int = 1) -> str:
    """只保留横向细线的表格；数值列右对齐；长表跨页时重复表头。"""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            right = index >= numeric_from and _numeric(value)
            cells.append(f'<td{" class=num" if right else ""}>{_cell(value)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<table class="{cls}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table>")


def kv(rows: Iterable[tuple[str, Any]], cls: str = "") -> str:
    """两列“项目 / 值”表（项目列为次要色）。"""
    body = "".join(f"<tr><td>{esc(k)}</td><td>{_cell(v)}</td></tr>" for k, v in rows)
    return f'<table class="kv {cls}"><tbody>{body}</tbody></table>' if body else ""


def tag(text: str, kind: str = "") -> str:
    return f'<span class="tag {kind}">{esc(text)}</span>'


def note(inner_html: str, kind: str = "") -> str:
    return f'<div class="note {kind}">{inner_html}</div>'


def quiet(text: str) -> str:
    return f'<p class="quiet">{text}</p>'


def source(text: str) -> str:
    return f'<p class="source">来源 / {esc(text)}</p>'


def details(summary: str, inner_html: str, hint: str = "") -> str:
    hint_html = f"<span>{esc(hint)}</span>" if hint else ""
    return (f'<details class="screen-only"><summary>{esc(summary)}{hint_html}</summary>'
            f"{inner_html}</details>")


def pre_json(value: Any) -> str:
    return "<pre>" + html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str)) + "</pre>"


def facts(items: list[tuple[str, str, str, str]]) -> str:
    """关键数字行：(数值, 单位, 名称, 说明)。"""
    blocks = "".join(
        f'<div class="fact"><div class="number">{esc(value)}<em>{html.escape(unit)}</em></div>'
        f'<p class="fact-label">{esc(label)}</p><small>{esc(small)}</small></div>'
        for value, unit, label, small in items)
    return f'<div class="facts">{blocks}</div>'


def summary_rows(rows: list[tuple[str, str]]) -> str:
    return '<div class="summary-list">' + "".join(
        f'<div class="summary-row"><strong>{esc(k)}</strong><span>{v}</span></div>'
        for k, v in rows) + "</div>"


def cols(*blocks: str, wide_left: bool = False) -> str:
    return f'<div class="cols{" wide-left" if wide_left else ""}">' + "".join(
        f"<div>{b}</div>" for b in blocks) + "</div>"


# 每章背景的粉笔插图（R2 粉笔交互版）：按章节顺序循环；插图只是装饰，不代表本次设备的精确结构
CHALK_IMAGES = ("motor", "inverter", "dq", "bench")
CHALK_NAMES = {"motor": "电机剖面", "inverter": "三相逆变器", "dq": "dq 坐标系", "bench": "同轴对拖台架"}
_CHAPTER_ART = ("motor", "bench", "inverter", "dq", "bench", "inverter", "motor", "bench", "dq", "motor")


class Doc:
    """一份日志：章节编号、图号、目录、页面外壳，以及屏幕阅读功能（粉笔背景、阅读设置、
    收藏、本机批注、图表索引、时间探索器）。阅读功能只在屏幕上出现，打印与 PDF 自动隐藏。"""

    def __init__(self, title: str, exp_label: str, footer: str, *, reader_key: str = "",
                 chalk_prefix: str = "log_assets/chalk/") -> None:
        self.title = title
        self.exp_label = exp_label
        self.footer = footer
        self.reader_key = reader_key or exp_label
        self.chalk_prefix = chalk_prefix
        self.chapters: list[tuple[str, str, str]] = []    # (编号, id, 标题)
        self.reader_data: dict = {}

    # ---------------------------------------------------------------- 组件
    def figure(self, src: str, caption: str, *, print_src: str | None = None,
               origin: str = "", kind: str = "") -> str:
        """统一宽度（占满正文栏）的图；点击放大；GIF 可另给打印用静态图。
        图号先写占位符，render() 按出现顺序统一编号（章节可以不按顺序生成）。"""
        label = _FIG_TOKEN
        alt = esc(caption)
        screen_cls = "screen-only" if print_src else ""
        img = (f'<button class="zoom {screen_cls}" type="button" data-image="{esc(src)}">'
               f'<img src="{esc(src)}" alt="{alt}" loading="lazy"></button>')
        if print_src:
            img += f'<img class="print-only" src="{esc(print_src)}" alt="{alt}">'
        origin_html = f"<br><span class=src>{esc(origin)}</span>" if origin else ""
        return (f'<figure class="{kind}">{img}<figcaption><b>{label}</b> {esc(caption)}'
                f"{origin_html}</figcaption></figure>")

    def svg_figure(self, svg: str, caption: str, origin: str = "") -> str:
        label = _FIG_TOKEN
        origin_html = f"<br><span class=src>{esc(origin)}</span>" if origin else ""
        return (f'<figure class="svg">{svg}<figcaption><b>{label}</b> {esc(caption)}'
                f"{origin_html}</figcaption></figure>")

    def chapter(self, sid: str, title: str, deck: str, body: str, status: str = "") -> str:
        index = len(self.chapters) + 1
        number = f"{index:02d}"
        self.chapters.append((number, sid, title))
        art = _CHAPTER_ART[(index - 1) % len(_CHAPTER_ART)]
        notes = (f'<details class="screen-only chapter-notes"><summary>阅读批注 '
                 f'<span id="note-count-{sid}">仅本机</span></summary><div class="note-editor">'
                 f'<label for="note-{sid}">第 {number} 章的个人记录</label>'
                 f'<textarea id="note-{sid}" data-note="{sid}" maxlength="10000" rows="3" '
                 'placeholder="记录疑问、复核线索或下一次实验想法…"></textarea>'
                 f'<small data-note-status="{sid}">批注只保存在本机浏览器，不会改写实验档案与日志结论。</small>'
                 "</div></details>")
        return (f'<section class="page" id="{sid}" data-art-image="{art}" '
                f'style="--chapter-art:url({self.chalk_prefix}{art}.jpg)">'
                f'<div class="running"><span>驭衡智控<i>/</i>实验日志</span>'
                f"<span>{esc(self.exp_label)}</span></div>"
                f'<div class="section-title"><div class="section-no">{number}</div><div>'
                f"<h2>{esc(title)} {status}</h2><p class=deck>{esc(deck)}</p></div>"
                f'<button class="chapter-mark screen-only" type="button" data-bookmark="{sid}" '
                f'aria-pressed="false" title="收藏这一章">☆</button></div>'
                f"{body}{notes}</section>")

    def toc(self) -> str:
        return '<div class="toc">' + "".join(
            f'<a href="#{sid}"><span>{esc(title)}</span><b>{no}</b></a>'
            for no, sid, title in self.chapters) + "</div>"

    def time_explorer(self, rows: list[dict], moments: list[tuple[float, str]],
                      video: dict | None = None) -> str:
        """可拖动读数的慢遥测时间线（只读最近的真实采样点，不插值），可跳到对应录像位置。"""
        samples = [[float(r["monotonic_s"])] + [float(r.get(k) or 0.0) for k in
                                                ("speed_actual", "speed_target", "current_actual", "vdc")]
                   for r in rows if r.get("monotonic_s") is not None]
        if len(samples) < 2 or samples[-1][0] <= samples[0][0]:
            return ""
        t_min, t_max = samples[0][0], samples[-1][0]
        v_max = max(max(abs(s[1]), abs(s[2])) for s in samples) * 1.1 or 1.0
        x0, width, y0, height = 42.0, 616.0, 194.0, 145.0

        def poly(col: int) -> str:
            step = max(1, len(samples) // 900)
            return " ".join(f"{x0 + (s[0] - t_min) / (t_max - t_min) * width:.1f},"
                            f"{y0 - s[col] / v_max * height:.1f}" for s in samples[::step])

        ticks = "".join(f'<text x="{x0 + k / 4 * width - 6:.0f}" y="220">{t_min + k / 4 * (t_max - t_min):.0f}</text>'
                        for k in range(5))
        self.reader_data["explorer"] = {
            "samples": [[round(v, 4) for v in s] for s in samples], "tMin": t_min, "tMax": t_max,
            "vMax": v_max, "x0": x0, "w": width, "y0": y0, "h": height,
            "moments": [[round(t, 3), label] for t, label in moments],
            "videoOffset": (video or {}).get("offset"), "videoEnd": (video or {}).get("end")}
        buttons = "".join(f'<button type="button" data-time="{when:.3f}">{esc(label)}</button>'
                          for when, label in moments[:6])
        seek = ('<button type="button" id="seek-video">查看对应录像 ↗</button>'
                if video and video.get("offset") is not None else "")
        return (
            '<details class="screen-only explorer" id="time-explorer"><summary>探索采集时间线 '
            '<span>拖动读数 · 定位录像</span></summary><div class="explorer-body">'
            '<p class="caption">拖动时间滑块，读取最近的一个慢遥测采样点（不插值）；时刻从实验开始计算。</p>'
            '<svg viewBox="0 0 700 240" class="time-chart" role="img" aria-label="可定位的转速时间线">'
            '<g stroke="#dce2dc"><path d="M42 49H658M42 121H658M42 194H658"/></g>'
            '<g fill="#74817b" font-size="11"><text x="0" y="27">rpm</text>'
            f'<text x="2" y="53">{v_max:.0f}</text><text x="2" y="125">{v_max / 2:.0f}</text>'
            f'<text x="20" y="198">0</text>{ticks}<text x="452" y="28">灰虚线：给定　青绿：实际</text></g>'
            f'<polyline points="{poly(2)}" fill="none" stroke="#9ca49d" stroke-width="1.5" stroke-dasharray="5 4"/>'
            f'<polyline points="{poly(1)}" fill="none" stroke="#39786b" stroke-width="1.7"/>'
            '<line id="time-cursor" x1="42" x2="42" y1="37" y2="200" stroke="#af6948" stroke-width="1.5"/>'
            '<circle id="time-dot" cx="42" cy="194" r="4" fill="#af6948"/></svg>'
            '<label class="range-label" for="time-range">采样时刻 <output id="time-label">—</output></label>'
            f'<input id="time-range" type="range" min="0" max="{len(samples) - 1}" value="0" step="1" '
            'aria-label="选择慢遥测采样时刻">'
            '<div class="sample-values"><div><small>实际转速</small><b id="read-speed"></b></div>'
            '<div><small>给定转速</small><b id="read-target"></b></div><div><small>实际电流</small>'
            '<b id="read-current"></b></div><div><small>母线电压</small><b id="read-voltage"></b></div></div>'
            f'<div class="stage-buttons">{buttons}{seek}</div><p class="caption" id="time-stage"></p>'
            '<p class="caption">仅用于浏览已保存的实验数据。录像定位按录像记录的起点偏移换算，'
            "精度受记录时间戳影响。</p></div></details>")

    # ---------------------------------------------------------------- 外壳
    def render(self, sections: list[str]) -> str:
        body = "".join(sections)
        parts = body.split(_FIG_TOKEN)
        body = parts[0] + "".join(f"图 {index:02d}{part}" for index, part in enumerate(parts[1:], 1))
        rail = "".join(f'<a href="#{sid}"><b>{no}</b>{esc(title)}</a>'
                       for no, sid, title in self.chapters)
        page_css = (
            "@page{size:A4;margin:15mm 12.7mm 15mm;"
            f"@top-left{{content:\"驭衡智控 / 实验日志\";{_MARGIN_FONT}}}"
            f"@top-right{{content:\"{_css_string(self.exp_label)}\";{_MARGIN_FONT}}}"
            f"@bottom-left{{content:\"{_css_string(self.footer)}\";{_MARGIN_FONT}}}"
            f"@bottom-right{{content:counter(page) \" / \" counter(pages);{_MARGIN_FONT}}}}}")
        first_art = _CHAPTER_ART[0]
        data = dict(self.reader_data, key=f"yuheng:{self.reader_key}:reader",
                    exp=self.exp_label, chalk=self.chalk_prefix, artNames=CHALK_NAMES)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(self.title)}</title><style>{CSS}{READER_CSS}{page_css}</style></head>"
            '<body data-atmosphere="paper" data-art="soft" data-size="normal">'
            f'<div class="chalk-wall screen-only" aria-hidden="true"><img id="wall-image" '
            f'src="{self.chalk_prefix}{first_art}.jpg" alt=""></div>'
            '<header class="appbar screen-only"><div class="brand">驭衡智控 · 实验日志'
            f'<small>{esc(self.exp_label)}</small></div><div class="header-tools">'
            '<button type="button" data-open="contents-panel">目录</button>'
            '<button type="button" data-open="figures-panel">图表</button>'
            '<button type="button" data-open="reading-panel">阅读设置</button>'
            '<button type="button" data-print>打印 / PDF</button></div>'
            '<div class="reading-progress" aria-hidden="true"><i id="reading-progress"></i></div></header>'
            f'<nav class="rail screen-only" aria-label="章节"><p>目录</p>{rail}</nav>'
            '<aside class="margin-status screen-only"><span id="reading-count">01</span>'
            '<strong id="reading-title"></strong><small id="background-title"></small>'
            '<button type="button" id="back-top">↑ 回到顶部</button></aside>'
            f'<main class="document" id="top-of-report">{body}</main>{_PANELS}'
            f'<script id="report-data" type="application/json">{payload}</script>'
            f"<script>{JS}</script></body></html>")


_PANELS = """
<dialog id="reading-panel" class="tool-panel screen-only"><div class="panel-heading"><div><small>READING ROOM</small>
<h2>阅读设置</h2></div><button type="button" data-close aria-label="关闭">×</button></div>
<h3>背景氛围</h3><div class="choice-row"><button type="button" data-atmosphere-choice="paper" aria-pressed="true">纸页 · 淡线稿</button>
<button type="button" data-atmosphere-choice="board" aria-pressed="false">黑板 · 粉笔画</button></div>
<h3>背景浓度</h3><div class="choice-row"><button type="button" data-art-choice="off" aria-pressed="false">关闭</button>
<button type="button" data-art-choice="soft" aria-pressed="true">轻淡</button>
<button type="button" data-art-choice="visible" aria-pressed="false">清晰</button></div>
<h3>正文字号</h3><div class="choice-row"><button type="button" data-size-choice="normal" aria-pressed="true">标准</button>
<button type="button" data-size-choice="large" aria-pressed="false">舒适</button></div>
<p class="caption" id="storage-status">设置与批注保存在本机浏览器中。</p>
<div class="panel-footer"><button type="button" id="export-notes">导出本机批注</button></div>
<p class="caption">背景为装饰性粉笔插图，不表示本次实验设备的精确结构。打印时自动隐藏粉笔背景与阅读工具。</p></dialog>
<dialog id="contents-panel" class="tool-panel screen-only"><div class="panel-heading"><div><small>CHAPTERS</small>
<h2>章节目录</h2></div><button type="button" data-close aria-label="关闭">×</button></div><div id="mobile-contents"></div>
<h3>我的收藏</h3><div id="bookmarked-chapters"></div></dialog>
<dialog id="figures-panel" class="tool-panel figures-panel screen-only"><div class="panel-heading"><div><small>FIGURE INDEX</small>
<h2>图表索引</h2></div><button type="button" data-close aria-label="关闭">×</button></div><div id="figure-index"></div>
<p class="caption">选择图表进入大图查看，可放大、拖动，左右方向键切换。</p></dialog>
<dialog class="image-viewer screen-only" id="image-viewer"><div class="viewer-bar"><span id="viewer-title">图表</span>
<button type="button" data-zoom="out" aria-label="缩小">−</button><output id="zoom-level">100%</output>
<button type="button" data-zoom="in" aria-label="放大">＋</button><button type="button" data-zoom="fit">适应</button>
<a id="download-figure" download>下载原图</a><button type="button" class="close" aria-label="关闭大图">×</button></div>
<div class="viewer-canvas"><img id="viewer-image" alt="" draggable="false"></div>
<div class="viewer-bottom"><button type="button" id="previous-figure">← 上一图</button><span id="viewer-caption"></span>
<button type="button" id="next-figure">下一图 →</button></div></dialog>
<div class="toast screen-only" role="status" aria-live="polite" id="toast"></div>
"""


_MARGIN_FONT = 'font:8.5pt "Microsoft YaHei",sans-serif;color:#74817b'
_FIG_TOKEN = "\x00FIG\x00"


def _css_string(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


# ─── 慢遥测曲线（SVG，与纸面同色系，矢量不失真） ──────────────────
def _downsample(rows: list[dict], limit: int = 1200) -> list[dict]:
    if len(rows) <= limit:
        return rows
    last = len(rows) - 1
    return [rows[round(i * last / (limit - 1))] for i in range(limit)]


def telemetry_svg(rows: list[dict], panels: list[tuple[str, list[tuple[str, str, str]]]],
                  markers: list[tuple[float, str]] = (), width: int = 700,
                  panel_h: int = 118) -> str:
    """多面板时间曲线。panels: [(面板名, [(字段, 图例, 样式 solid/dash/copper)])]。"""
    rows = _downsample([r for r in rows if r.get("monotonic_s") is not None])
    if not rows:
        return ""
    t = np.array([float(r["monotonic_s"]) for r in rows])
    t0, t1 = float(t[0]), float(t[-1]) if t[-1] > t[0] else float(t[0]) + 1.0
    left, right, top_pad = 50, 12, 10
    plot_w = width - left - right
    height = len(panels) * (panel_h + 16) + 44
    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'role="img" class="chart">']

    def x_of(value: float) -> float:
        return left + (value - t0) / (t1 - t0) * plot_w

    for index, (name, series) in enumerate(panels):
        y0 = top_pad + index * (panel_h + 16)
        values = [np.array([np.nan if r.get(k) is None else float(r[k]) for r in rows])
                  for k, _l, _s in series]
        finite = np.concatenate([v[np.isfinite(v)] for v in values]) if values else np.array([])
        if finite.size == 0:
            continue
        lo, hi = float(finite.min()), float(finite.max())
        if hi - lo < 1e-9:
            lo, hi = lo - 1.0, hi + 1.0
        pad = 0.08 * (hi - lo)
        lo, hi = lo - pad, hi + pad

        def y_of(value: float) -> float:
            return y0 + panel_h - (value - lo) / (hi - lo) * panel_h

        out.append(f'<line x1="{left}" y1="{y0 + panel_h}" x2="{left + plot_w}" '
                   f'y2="{y0 + panel_h}" stroke="{LINE}"/>')
        for k in range(3):
            value = lo + (k + 0.5) * (hi - lo) / 3
            y = y_of(value)
            out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                       f'stroke="#edf0e8"/>')
            out.append(f'<text x="{left - 6}" y="{y + 3:.1f}" text-anchor="end" '
                       f'font-size="9" fill="{MUTED}">{value:.3g}</text>')
        out.append(f'<text x="{left}" y="{y0 + 9}" font-size="10" fill="{MUTED}">{esc(name)}</text>')
        legend_x = left + plot_w
        for (key, label, style), v in reversed(list(zip(series, values))):
            color = {"dash": "#9aa59f", "copper": COPPER}.get(style, TEAL)
            dash = ' stroke-dasharray="4 3"' if style == "dash" else ""
            points = " ".join(f"{x_of(tt):.1f},{y_of(vv):.1f}"
                              for tt, vv in zip(t, v) if math.isfinite(vv))
            if points:
                out.append(f'<polyline points="{points}" fill="none" stroke="{color}" '
                           f'stroke-width="1.3"{dash}/>')
            text_w = 12 * len(label) + 26
            legend_x -= text_w
            out.append(f'<line x1="{legend_x}" y1="{y0 + 6}" x2="{legend_x + 16}" y2="{y0 + 6}" '
                       f'stroke="{color}" stroke-width="1.6"{dash}/>')
            out.append(f'<text x="{legend_x + 20}" y="{y0 + 9}" font-size="10" '
                       f'fill="{INK}">{esc(label)}</text>')
    bottom = height - 30                 # 刻度数字在 bottom+10，横轴标题在最底一行
    for tick in np.linspace(t0, t1, 7):
        out.append(f'<text x="{x_of(tick):.1f}" y="{bottom + 10}" text-anchor="middle" '
                   f'font-size="9" fill="{MUTED}">{tick:.0f}</text>')
    row_end: list[float] = []            # 每一行标签已占到的横坐标，挤不下就换一行
    for when, label in markers:
        if t0 <= when <= t1:
            x = x_of(when)
            out.append(f'<line x1="{x:.1f}" y1="{top_pad}" x2="{x:.1f}" y2="{bottom - 4}" '
                       f'stroke="{COPPER}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0.7"/>')
            width_px = 9.5 * len(label) + 6
            row = next((i for i, end in enumerate(row_end) if x >= end), len(row_end))
            if row == len(row_end):
                row_end.append(0.0)
            row_end[row] = x + width_px
            out.append(f'<text x="{x + 3:.1f}" y="{bottom - 6 - 11 * row}" font-size="9" '
                       f'fill="{COPPER}">{esc(label)}</text>')
    out.append(f'<text x="{left + plot_w / 2}" y="{height - 1}" text-anchor="middle" '
               f'font-size="9" fill="{MUTED}">实验相对时间 / s</text></svg>')
    return "".join(out)


MOTIF_SVG = (
    '<svg class="motif" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg" '
    'fill="none" stroke="currentColor" stroke-width="1">'
    '<circle cx="120" cy="120" r="108"/><circle cx="120" cy="120" r="92"/>'
    '<circle cx="120" cy="120" r="54"/><circle cx="120" cy="120" r="14"/>'
    + "".join(
        f'<line x1="{120 + 92 * math.cos(a):.1f}" y1="{120 + 92 * math.sin(a):.1f}" '
        f'x2="{120 + 62 * math.cos(a):.1f}" y2="{120 + 62 * math.sin(a):.1f}"/>'
        for a in (k * math.pi / 6 for k in range(12)))
    + "".join(
        f'<path d="M{120 + 54 * math.cos(a - 0.3):.1f},{120 + 54 * math.sin(a - 0.3):.1f} '
        f'A54,54 0 0 1 {120 + 54 * math.cos(a + 0.3):.1f},{120 + 54 * math.sin(a + 0.3):.1f}" '
        f'stroke-width="4" opacity=".5"/>'
        for a in (k * math.pi / 4 for k in range(8)))
    + "</svg>")


CSS = """
:root{--ink:#24312f;--muted:#74817b;--line:#dce2dc;--paper:#fffefa;--teal:#39786b;--copper:#af6948;--soft:#f3f4ee}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:#e7e9e3;color:var(--ink);font:14px/1.65 "Microsoft YaHei",Arial,sans-serif}
a{color:var(--teal);text-underline-offset:4px}button{font:inherit;cursor:pointer}
.appbar{height:56px;position:sticky;top:0;z-index:20;background:#253430;color:#edf1e8;display:flex;
 align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid #52625a}
.brand{font-size:15px;letter-spacing:2px}.brand small{font-size:10px;letter-spacing:2px;color:#b6c5b8;margin-left:18px}
.appbar button{border:1px solid #74857a;background:transparent;color:inherit;padding:5px 14px;border-radius:3px}
.appbar a{color:#dce7dc;margin-right:18px;font-size:12px}
.rail{position:fixed;left:max(16px,calc(50% - 660px));top:96px;width:150px}
.rail p{color:var(--muted);font-size:10px;letter-spacing:2px;margin:0 0 12px}
.rail a{display:block;text-decoration:none;font-size:12px;padding:7px 0 7px 12px;color:#707c73;border-left:2px solid transparent}
.rail a:hover,.rail a.active{color:var(--ink);border-color:var(--copper);background:#dfe3d9}
.rail b{font-weight:400;color:var(--copper);margin-right:10px}
.document{padding:28px 0 56px}
.page{position:relative;background:var(--paper);width:820px;margin:0 auto 24px;padding:40px 52px 48px;
 box-shadow:0 4px 24px #263c3010;scroll-margin-top:76px}
.page:before{content:"";position:absolute;left:0;top:40px;width:4px;height:42px;background:var(--copper)}
.running{font-size:10px;letter-spacing:1px;color:var(--muted);display:flex;justify-content:space-between;
 border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:26px}
.running i{font-style:normal;color:var(--copper);margin:0 6px}
.section-title{display:flex;gap:18px;align-items:flex-start;margin-bottom:22px}
.section-no{font:40px/1.1 Georgia,serif;color:#c19a7e;min-width:48px}
h1,h2,h3,p,figure{margin:0}
h1{font-size:40px;font-weight:500;line-height:1.2;letter-spacing:1px}
h2{font-size:25px;font-weight:500;line-height:1.35}
h3{font-size:14px;font-weight:600;margin:20px 0 8px;break-after:avoid}
p{margin:6px 0}.deck{font-size:11px;color:var(--muted);margin-top:6px}
.eyebrow{font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:var(--copper);margin-bottom:12px}
.hero{position:relative;padding:22px 0 26px;overflow:hidden}.hero h1{position:relative;z-index:1}
.hero .cn-title{font-size:18px;margin-top:10px}
.motif{position:absolute;right:-12px;top:-14px;width:220px;color:#a5b2a2;opacity:.4;z-index:0}
.meta-line{font-size:11px;color:var(--muted);margin:16px 0 0;position:relative;z-index:1}
.tags{margin-top:14px;position:relative;z-index:1}
.tag{display:inline-block;font-size:10px;letter-spacing:1px;padding:2px 8px;border:1px solid #c8d8cb;
 color:var(--teal);background:#edf3ea;border-radius:2px;margin-right:6px;vertical-align:middle;font-weight:400}
.tag.review{color:#9b603d;border-color:#e3cdb5;background:#f8f0e4}
.tag.ai{color:#7a5a8a;border-color:#dccbe3;background:#f5eff8}
.facts{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line);
 border-bottom:1px solid var(--line);margin:8px 0 20px;padding:16px 0}
.fact{padding-left:18px;border-left:1px solid var(--line)}.fact:first-child{padding-left:0;border:0}
.number{font:31px/1.15 "Bahnschrift",Arial,sans-serif;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.number em{font:11px "Microsoft YaHei",sans-serif;color:var(--muted);letter-spacing:0;font-style:normal;margin-left:4px}
.fact-label{margin:6px 0 2px;font-size:12px}.fact small{font-size:10px;color:var(--muted)}
figure{margin:12px 0 18px;break-inside:avoid}figure img,figure svg{display:block;width:100%;height:auto}
.zoom{display:block;width:100%;border:0;background:transparent;padding:0}
.zoom:focus-visible{outline:2px solid var(--copper)}
figcaption{font-size:10px;color:var(--muted);line-height:1.6;margin-top:6px}
figcaption b{font-weight:500;color:var(--copper);margin-right:4px}figcaption .src{color:#98a39b}
.summary-list{border-top:1px solid var(--line);margin-top:16px}
.summary-row{display:grid;grid-template-columns:96px 1fr;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}
.summary-row strong{font-weight:500}.summary-row span{color:#617168}
.toc{display:grid;grid-template-columns:1fr 1fr;gap:6px 34px;margin-top:22px;font-size:11px}
.toc a{display:flex;justify-content:space-between;text-decoration:none;border-bottom:1px solid var(--line);padding:4px 0;color:var(--ink)}
.toc b{font-weight:400;color:var(--copper)}
table{border-collapse:collapse;width:100%;font-size:10.5px;margin:8px 0 16px}
thead{display:table-header-group}tr{break-inside:avoid}
th{text-align:left;font-weight:500;color:#65756d;background:#edf0e8;border-top:1px solid #cad4c9;padding:7px 9px}
td{padding:7px 9px;border-bottom:1px solid #e3e8df;vertical-align:top;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
td.num,th.num{text-align:right}table.kv td:first-child{width:38%;color:var(--muted)}
table.compact td{padding:5px 8px}.review-cell{color:#a55c3a}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}.cols.wide-left{grid-template-columns:1.3fr 1fr}
.cols>div{min-width:0}
.note{border-left:2px solid var(--copper);padding:8px 14px;background:#f7f3e9;font-size:11px;line-height:1.7;margin:12px 0;break-inside:avoid}
.note.info{border-color:var(--teal);background:#eef3ec}
.quiet{font-size:11px;color:#68786e}.source{font-size:9px;color:#849083;overflow-wrap:anywhere;margin:6px 0 12px}
.timeline{display:flex;border-top:2px solid #9aae9b;padding:10px 0 12px;gap:8px;margin-top:14px}
.timeline div{flex:1;font-size:10px;color:var(--muted)}.timeline b{display:block;font-size:13px;color:var(--ink);font-weight:500}
.event-table th:nth-child(1){width:12%}.event-table th:nth-child(2){width:24%}.event-table td{font-size:10px;padding:6px 7px}
.ai-text{white-space:pre-wrap;font-size:12px;border-left:2px solid #b89bc4;padding:8px 14px;background:#faf7fb}
.conclusion{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:12px 0;margin:14px 0}
.conclusion .blank{height:30px;border-bottom:1px solid #e5e9e1}
.file-row>span{min-width:0;overflow-wrap:anywhere}
.file-row{display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:12px;border-bottom:1px solid var(--line);padding:9px 0;font-size:11px}
.file-icon{color:var(--copper);font-size:10px;letter-spacing:1px}.file-row small{display:block;color:var(--muted);font-size:10px}
.hash{font-family:Consolas,monospace;font-size:9.5px}
details{margin:8px 0}summary{cursor:pointer;font-size:11px;list-style:none;padding:10px 0;border-bottom:1px solid var(--line);color:var(--teal)}
summary span{float:right;color:var(--muted)}details[open]{padding-bottom:10px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:10px/1.7 Consolas,monospace;background:#f0f2ec;padding:12px;max-height:480px;overflow:auto}
video{display:block;width:100%;max-height:420px;background:#18201e;margin-top:8px}
.dialog{border:0;padding:12px;background:#fff;max-width:96vw;max-height:94vh;box-shadow:0 10px 80px #0005}
.dialog::backdrop{background:#17201bd9}.dialog img{display:block;max-width:90vw;max-height:80vh}
.dialog .close{display:block;margin-left:auto;border:0;background:#f0f1eb;padding:5px 16px}
.print-only{display:none}.chart text{font-family:"Microsoft YaHei",sans-serif}
@media screen and (max-width:1210px){.rail{display:none}}
@media screen and (max-width:860px){.document{padding-top:10px}.page{width:calc(100% - 16px);padding:24px 20px 32px}
 .running{font-size:8px}.appbar{padding:0 14px}.brand small{display:none}.motif{width:160px;right:-30px}
 .hero h1{font-size:32px}.number{font-size:24px}.fact{padding-left:10px}
 .cols,.cols.wide-left{grid-template-columns:1fr}h2{font-size:22px}.toc{gap:6px 16px}}
@media print{html,body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .page{background:#fff}
 .appbar,.rail,.screen-only,.dialog,details,video{display:none!important}.print-only{display:block}
 .document{padding:0}.page{width:auto;margin:0;padding:0;box-shadow:none;break-before:page}
 .page:first-child{break-before:auto}.page:before{display:none}.running{display:none}
 a{color:inherit;text-decoration:none}.zoom{padding:0}
 figure img{max-height:215mm;width:auto;max-width:100%;margin:0 auto;object-fit:contain}}
"""

READER_CSS = """
@media screen{
 html{scroll-padding-top:80px}
 body{--wall-opacity:.19;--page-art-opacity:.075;--wall-filter:invert(1) grayscale(1);background:#e6e8df}
 body[data-atmosphere="board"]{background:#1d2522;--wall-filter:none;--wall-opacity:.33}
 body[data-art="visible"]{--wall-opacity:.34;--page-art-opacity:.13}
 body[data-atmosphere="board"][data-art="visible"]{--wall-opacity:.55}
 body[data-art="off"]{--wall-opacity:0;--page-art-opacity:0}
 .chalk-wall{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
 .chalk-wall img{width:135vw;height:100vh;max-width:none;object-fit:cover;position:absolute;left:-15vw;
  filter:var(--wall-filter);opacity:var(--wall-opacity);transition:opacity .3s}
 .document{position:relative;z-index:1}
 .page{overflow:hidden;background:#fffefa;isolation:isolate}
 .page::after{content:"";position:absolute;z-index:-1;inset:0 0 auto;height:490px;pointer-events:none;
  background-image:var(--chapter-art);background-position:78% -30px;background-size:900px auto;background-repeat:no-repeat;
  filter:invert(1) grayscale(1);mix-blend-mode:multiply;opacity:var(--page-art-opacity);
  mask-image:linear-gradient(to bottom,#000 8%,#0009 43%,transparent 95%);
  -webkit-mask-image:linear-gradient(to bottom,#000 8%,#0009 43%,transparent 95%)}
 #s1::after,#c1::after{height:590px;background-position:150px 140px;background-size:790px auto;
  opacity:calc(var(--page-art-opacity) * 1.6);-webkit-mask-image:linear-gradient(to right,transparent 10%,black 65%);
  mask-image:linear-gradient(to right,transparent 10%,black 65%)}
 .motif{display:none}
 .appbar{background:#253430ed;backdrop-filter:blur(12px);height:60px}
 .header-tools{display:flex;align-items:center;gap:7px}
 .header-tools button{font-size:11px;padding:6px 12px;border-color:#708276;border-radius:3px}
 button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid #b67d58;outline-offset:3px}
 .reading-progress{height:3px;position:absolute;bottom:-1px;left:0;right:0;background:#63746533;overflow:hidden}
 .reading-progress i{display:block;width:100%;height:100%;background:#c7946e;transform:scaleX(0);transform-origin:left}
 .rail{z-index:3;background:#e6e8dfaa;padding:16px 12px;border:1px solid #d0d6c844;border-radius:4px;backdrop-filter:blur(7px)}
 body[data-atmosphere="board"] .rail{background:#1d2824ba;border-color:#58706244}
 body[data-atmosphere="board"] .rail p,body[data-atmosphere="board"] .rail a{color:#b9c5b9}
 body[data-atmosphere="board"] .rail a.active{background:#53635055;color:#f2efe2}
 .margin-status{position:fixed;right:max(18px,calc(50% - 660px));top:118px;width:150px;color:#788270;z-index:2;
  display:flex;flex-direction:column;gap:12px}
 .margin-status>span{font:38px/1 Georgia,serif;color:#b58b6d;letter-spacing:-2px}
 .margin-status strong{font-size:13px;font-weight:400}.margin-status small{font-size:10px;line-height:1.8}
 .margin-status button{width:110px;margin-top:14px;padding:8px 0;border:0;border-top:1px solid #aab4a477;
  background:transparent;color:inherit;font-size:11px;text-align:left}
 body[data-atmosphere="board"] .margin-status{color:#c4cbbb}
 .section-title{position:relative}.section-title>div:nth-child(2){padding-right:28px}
 .chapter-mark{position:absolute;right:0;top:2px;border:0;background:transparent;color:#a9b4a4;font-size:24px;padding:0 3px}
 .chapter-mark[aria-pressed="true"]{color:#af6948}
 .chapter-notes{margin-top:18px}.chapter-notes summary{color:#81907d;font-size:10px}
 .note-editor{padding:14px 0 8px}.note-editor label{display:block;color:#697c6b;font-size:11px;margin-bottom:8px}
 .note-editor textarea{display:block;resize:vertical;width:100%;min-height:95px;border:1px solid #c8d2c2;border-radius:3px;
  padding:12px;background:#f7f8f0;color:#304033;font:12px/1.8 "Microsoft YaHei",sans-serif}
 .note-editor small{display:block;margin-top:7px;color:#80907f;font-size:10px}
 .tool-panel{position:fixed;inset:0;margin:auto;border:1px solid #d4dccc;border-radius:8px;padding:24px;width:430px;
  max-width:calc(100vw - 24px);max-height:86vh;overflow:auto;background:#fffefa;color:var(--ink);box-shadow:0 22px 90px #0e1b164d}
 dialog::backdrop{background:#18241e96;backdrop-filter:blur(4px)}
 .panel-heading{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:14px}
 .panel-heading small{font-size:9px;letter-spacing:2px;color:var(--copper)}.panel-heading h2{font-size:22px;margin-top:6px}
 .panel-heading button{align-self:flex-start;border:0;background:transparent;font-size:25px;color:#6f7a6e;padding:0 5px}
 .tool-panel h3{font-size:12px;margin:18px 0 10px}.caption{font-size:10px;color:var(--muted);line-height:1.6}
 .choice-row{display:flex;gap:7px;flex-wrap:wrap}
 .choice-row button,.panel-footer button,.stage-buttons button,.viewer-bar button,.viewer-bottom button{border:1px solid #cad4c5;
  border-radius:3px;background:transparent;color:#3d5547;padding:7px 11px;font-size:11px}
 .choice-row button[aria-pressed="true"]{color:#8e5536;border-color:#c38f64;background:#f3eadd}
 .panel-footer{margin:18px 0 12px;display:flex;justify-content:space-between;align-items:center;gap:12px}
 #mobile-contents a,#bookmarked-chapters a{display:flex;justify-content:space-between;align-items:center;padding:10px 0;
  text-decoration:none;border-bottom:1px solid #e3e8dd;font-size:12px;color:var(--ink)}
 #mobile-contents b,#bookmarked-chapters b{font-weight:400;color:var(--copper)}#bookmarked-chapters p{font-size:11px;color:#7f897b}
 #figure-index{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 #figure-index button{min-width:0;text-align:left;border:1px solid #e2e6dc;background:#f9faf4;padding:10px;border-radius:4px}
 #figure-index img{width:100%;height:95px;object-fit:contain;background:white}
 #figure-index span{display:block;font-size:11px;margin-top:7px;color:#465e4b}.figures-panel{width:610px}
 .image-viewer{width:min(1260px,96vw);height:min(870px,94vh);padding:0;border:0;border-radius:6px;background:#fffefa;overflow:hidden;max-height:94vh}
 .image-viewer[open]{display:flex;flex-direction:column}
 .viewer-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line)}
 .viewer-bar span{margin-right:auto;font-size:13px}.viewer-bar output{font-size:11px;min-width:39px;text-align:center}
 .viewer-bar a{font-size:11px;padding:0 7px}.viewer-bar .close{padding:4px 10px;font-size:19px;border:0}
 .viewer-canvas{flex:1;overflow:auto;min-height:0;background:#fff;padding:20px;cursor:grab}
 .viewer-canvas img{max-width:none;display:block;width:100%;height:auto;margin:auto;user-select:none}
 .viewer-canvas.dragging{cursor:grabbing}
 .viewer-bottom{padding:10px 16px;border-top:1px solid var(--line);display:flex;align-items:center;gap:20px;justify-content:space-between}
 .viewer-bottom span{font-size:11px;color:#7f8b7c}
 .toast{position:fixed;bottom:25px;left:50%;transform:translate(-50%,12px);max-width:90vw;padding:10px 18px;background:#263a2ff2;
  color:#f1f4e9;border-radius:5px;z-index:1000;font-size:12px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s}
 .toast.show{opacity:1;transform:translate(-50%,0)}
 .explorer{border-top:1px solid #c9d5c4;margin:18px 0 8px}.explorer summary{font-size:12px}.explorer-body{padding-top:12px}
 .time-chart{width:100%;height:auto;display:block;margin-top:10px;background:#fbfcf7;cursor:crosshair}
 .range-label{display:flex;justify-content:space-between;font-size:11px;color:#7a8874;margin:8px 0}
 .range-label output{font:17px Bahnschrift,Arial,sans-serif;color:#986946}
 #time-range{width:100%;accent-color:#9d704e;cursor:ew-resize;margin:6px 0 12px}
 .sample-values{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
 .sample-values small{font-size:10px;color:#7b8a74;display:block}.sample-values b{display:block;font:18px Bahnschrift,Arial,sans-serif;margin-top:5px}
 .stage-buttons{display:flex;gap:7px;flex-wrap:wrap;margin:15px 0 10px}.stage-buttons button{font-size:10px;padding:5px 9px}
 .stage-buttons #seek-video{margin-left:auto;color:#986443}
 body[data-size="large"] .page table,body[data-size="large"] .page td{font-size:12px}
 body[data-size="large"] .page .quiet,body[data-size="large"] .page .summary-row{font-size:14px}
 body[data-size="large"] .page figcaption,body[data-size="large"] .page .note{font-size:12px}
 @keyframes locate{from{box-shadow:0 0 0 3px #bf916b99}to{box-shadow:0 0 0 3px transparent}}.located{animation:locate 1.4s ease-out}
}
@media screen and (max-width:1210px){.margin-status{display:none}.chalk-wall img{width:160vw;left:-30vw}}
@media screen and (max-width:860px){.header-tools{gap:4px}.header-tools button{font-size:10px;padding:5px 7px}
 .page::after{background-size:680px auto;background-position:35% 0}#s1::after,#c1::after{background-position:20px 140px;background-size:620px auto}
 .sample-values{grid-template-columns:1fr 1fr}.viewer-bar span{flex-basis:100%}.stage-buttons #seek-video{margin-left:0}}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto!important}*,*::before,*::after{animation:none!important;transition:none!important}}
@media print{.page::after,.chalk-wall,.margin-status,.chapter-mark,.chapter-notes,.explorer,.toast,.tool-panel,.image-viewer{display:none!important}
 .motif{display:block}}
"""

JS = r"""
'use strict';
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const chapters = $$('.page');
  const data = JSON.parse($('#report-data').textContent);
  let saved = {atmosphere:'paper', art:'soft', size:'normal', notes:{}, bookmarks:[]};
  let persistent = true;
  try { saved = {...saved, ...JSON.parse(localStorage.getItem(data.key) || '{}')}; } catch (_) { persistent = false; }
  if (!saved.notes || typeof saved.notes !== 'object') saved.notes = {};
  if (!Array.isArray(saved.bookmarks)) saved.bookmarks = [];
  let toastTimer;
  function toast(message) {
    const t = $('#toast'); t.textContent = message; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
  }
  function persist() {
    if (persistent) { try { localStorage.setItem(data.key, JSON.stringify(saved)); } catch (_) { persistent = false; } }
    $('#storage-status').textContent = persistent ? '设置与批注保存在本机浏览器中。' : '浏览器不允许本地保存；本次打开仍可使用，请导出批注。';
    return persistent;
  }
  function applySettings() {
    for (const field of ['atmosphere','art','size']) {
      const choices = $$(`[data-${field}-choice]`);
      if (!choices.some(b => b.dataset[`${field}Choice`] === saved[field])) saved[field] = choices[0].dataset[`${field}Choice`];
      document.body.dataset[field] = saved[field];
      choices.forEach(b => b.setAttribute('aria-pressed', String(b.dataset[`${field}Choice`] === saved[field])));
    }
  }
  applySettings(); persist();
  for (const field of ['atmosphere','art','size']) {
    $$(`[data-${field}-choice]`).forEach(b => b.addEventListener('click', () => { saved[field] = b.dataset[`${field}Choice`]; applySettings(); persist(); }));
  }
  $$('[data-open]').forEach(b => b.addEventListener('click', () => $('#'+b.dataset.open).showModal()));
  $$('[data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  $$('.tool-panel').forEach(d => d.addEventListener('click', e => { if (e.target === d) d.close(); }));
  $('[data-print]').addEventListener('click', () => window.print());
  $('#back-top').addEventListener('click', () => chapters[0].scrollIntoView());
  const title = p => $('h2', p).childNodes[0].textContent.trim();
  function jump(id) {
    $$('.tool-panel[open]').forEach(d => d.close());
    const target = $('#'+id); target.scrollIntoView({block:'start'});
    target.classList.remove('located'); requestAnimationFrame(() => target.classList.add('located'));
  }
  function chapterLink(p) {
    const a = document.createElement('a'); a.href = '#'+p.id;
    const label = document.createElement('span'); label.textContent = title(p);
    const n = document.createElement('b'); n.textContent = $('.section-no', p).textContent;
    a.append(label, n); a.addEventListener('click', e => { e.preventDefault(); jump(p.id); }); return a;
  }
  chapters.forEach(p => $('#mobile-contents').append(chapterLink(p)));
  function bookmarks() {
    const host = $('#bookmarked-chapters'); host.replaceChildren();
    chapters.filter(p => saved.bookmarks.includes(p.id)).forEach(p => host.append(chapterLink(p)));
    if (!host.children.length) { const p = document.createElement('p'); p.textContent = '点击章节标题旁的 ☆，把想复看的章节收在这里。'; host.append(p); }
    $$('[data-bookmark]').forEach(b => { const on = saved.bookmarks.includes(b.dataset.bookmark); b.textContent = on ? '★' : '☆'; b.setAttribute('aria-pressed', String(on)); });
  }
  $$('[data-bookmark]').forEach(b => b.addEventListener('click', () => {
    const id = b.dataset.bookmark, exists = saved.bookmarks.includes(id);
    saved.bookmarks = exists ? saved.bookmarks.filter(v => v !== id) : [...saved.bookmarks, id];
    bookmarks(); persist(); toast(exists ? '已取消收藏' : '已收藏，可在“目录”中查看');
  })); bookmarks();
  $$('[data-note]').forEach(input => {
    const n = input.dataset.note;
    input.value = typeof saved.notes[n] === 'string' ? saved.notes[n] : '';
    const count = () => { $('#note-count-'+n).textContent = input.value ? `${input.value.length} 字` : '仅本机'; };
    count();
    input.addEventListener('input', () => {
      saved.notes[n] = input.value; const ok = persist();
      $(`[data-note-status="${n}"]`).textContent = ok ? '已保存到本机浏览器；未改写实验档案。' : '本次打开有效，请导出批注保存。';
      count();
    });
  });
  $('#export-notes').addEventListener('click', () => {
    const lines = ['驭衡实验日志 · 本机阅读批注', data.exp, new Date().toLocaleString(), '个人阅读记录，不属于实验结论。', ''];
    chapters.forEach(p => { if (saved.notes[p.id]) lines.push(`${$('.section-no', p).textContent} ${title(p)}`, saved.notes[p.id], ''); });
    if (lines.length === 5) { toast('还没有批注，可在各章节末尾填写。'); return; }
    const url = URL.createObjectURL(new Blob(['﻿'+lines.join('\r\n')], {type:'text/plain;charset=utf-8'}));
    const a = document.createElement('a'); a.href = url; a.download = `${data.exp}_阅读批注.txt`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // 阅读位置：滚动时只排一次回调，不用常驻定时器
  let scrollPending = false, currentArt = null;
  function updatePosition() {
    scrollPending = false;
    const point = window.innerHeight * .32;
    let active = chapters[0];
    for (const p of chapters) { if (p.getBoundingClientRect().top <= point) active = p; }
    $$('.rail a').forEach(a => a.classList.toggle('active', a.hash === '#'+active.id));
    $('#reading-count').textContent = $('.section-no', active).textContent + ' / ' + String(chapters.length).padStart(2, '0');
    $('#reading-title').textContent = title(active);
    const art = active.dataset.artImage;
    if (art !== currentArt) { $('#wall-image').src = `${data.chalk}${art}.jpg`; currentArt = art; }
    $('#background-title').textContent = (data.artNames[art] || '') + ' · 粉笔插图';
    const max = document.documentElement.scrollHeight - innerHeight;
    $('#reading-progress').style.transform = `scaleX(${Math.max(0, Math.min(1, scrollY / Math.max(1, max)))})`;
  }
  function schedule() { if (!scrollPending) { scrollPending = true; requestAnimationFrame(updatePosition); } }
  window.addEventListener('scroll', schedule, {passive:true}); window.addEventListener('resize', schedule);
  $$('details').forEach(d => d.addEventListener('toggle', schedule)); updatePosition();

  // 图表索引与大图查看
  const figures = $$('[data-image]').map(b => ({button:b, src:b.dataset.image,
    caption:($('figcaption', b.closest('figure')) || {textContent:''}).textContent}));
  const viewer = $('#image-viewer'), image = $('#viewer-image'), canvas = $('.viewer-canvas');
  let figureIndex = 0, zoom = 1;
  function setZoom(z) { zoom = Math.max(.5, Math.min(4, z)); image.style.width = (zoom*100)+'%'; $('#zoom-level').value = Math.round(zoom*100)+'%'; }
  function showFigure(i) {
    figureIndex = (i + figures.length) % figures.length;
    const f = figures[figureIndex]; image.src = f.src; image.alt = f.caption;
    $('#viewer-title').textContent = f.caption.split('（')[0].split('：')[0].trim();
    $('#viewer-caption').textContent = `${figureIndex+1} / ${figures.length} · 可放大、拖动查看`;
    $('#download-figure').href = f.src; $('#download-figure').download = f.src.split('/').pop();
    $('#figures-panel').close(); setZoom(1);
    if (!viewer.open) viewer.showModal(); canvas.scrollTop = canvas.scrollLeft = 0;
  }
  figures.forEach((f, i) => {
    f.button.addEventListener('click', () => showFigure(i));
    const b = document.createElement('button'), img = document.createElement('img'), span = document.createElement('span');
    b.type = 'button'; img.src = f.src; img.alt = ''; img.loading = 'lazy'; span.textContent = f.caption.split('。')[0];
    b.append(img, span); b.addEventListener('click', () => showFigure(i)); $('#figure-index').append(b);
  });
  $('.close', viewer).addEventListener('click', () => viewer.close());
  $('#previous-figure').addEventListener('click', () => showFigure(figureIndex-1));
  $('#next-figure').addEventListener('click', () => showFigure(figureIndex+1));
  $$('[data-zoom]').forEach(b => b.addEventListener('click', () => setZoom(b.dataset.zoom === 'fit' ? 1 : zoom*(b.dataset.zoom === 'in' ? 1.25 : .8))));
  viewer.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight') { e.preventDefault(); showFigure(figureIndex+1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); showFigure(figureIndex-1); }
  });
  let dragging;
  canvas.addEventListener('pointerdown', e => { if (e.pointerType !== 'mouse' || zoom <= 1) return;
    dragging = {x:e.clientX, y:e.clientY, left:canvas.scrollLeft, top:canvas.scrollTop}; canvas.setPointerCapture(e.pointerId); canvas.classList.add('dragging'); });
  canvas.addEventListener('pointermove', e => { if (!dragging) return; canvas.scrollLeft = dragging.left + dragging.x - e.clientX; canvas.scrollTop = dragging.top + dragging.y - e.clientY; });
  const endDrag = () => { dragging = null; canvas.classList.remove('dragging'); };
  canvas.addEventListener('pointerup', endDrag); canvas.addEventListener('pointercancel', endDrag);

  // 录像：内置浏览器没有 H.264 解码器时直接给出提示
  $$('video').forEach(v => { const hint = v.parentElement.querySelector('.video-hint'); if (!hint) return;
    if (!v.canPlayType('video/mp4; codecs="avc1.42E01E"')) hint.hidden = false;
    v.addEventListener('error', () => { hint.hidden = false; }, true); });

  // 时间探索器：只读最近的真实采样点
  const ex = data.explorer, slider = $('#time-range');
  if (ex && slider) {
    const xOf = t => ex.x0 + (t - ex.tMin) / (ex.tMax - ex.tMin) * ex.w;
    const yOf = v => ex.y0 - v / ex.vMax * ex.h;
    const read = () => {
      const r = ex.samples[Number(slider.value)];
      $('#time-label').value = r[0].toFixed(3)+' s';
      [['#read-speed',1,'rpm',1],['#read-target',2,'rpm',1],['#read-current',3,'A',3],['#read-voltage',4,'V',1]]
        .forEach(([sel, col, unit, digits]) => { $(sel).textContent = r[col].toFixed(digits)+' '+unit; });
      const x = xOf(r[0]), y = yOf(r[1]);
      $('#time-cursor').setAttribute('x1', x); $('#time-cursor').setAttribute('x2', x);
      $('#time-dot').setAttribute('cx', x); $('#time-dot').setAttribute('cy', y);
      const past = ex.moments.filter(m => m[0] <= r[0]);
      $('#time-stage').textContent = '最近的关键事件：' + (past.length ? past[past.length-1][1] : '尚未开始') +
        ' · 真实采样点 ' + (Number(slider.value)+1) + ' / ' + ex.samples.length;
    };
    const selectTime = t => { let best = 0; ex.samples.forEach((r, i) => { if (Math.abs(r[0]-t) < Math.abs(ex.samples[best][0]-t)) best = i; }); slider.value = best; read(); };
    slider.addEventListener('input', read);
    $$('[data-time]').forEach(b => b.addEventListener('click', () => selectTime(Number(b.dataset.time))));
    $('.time-chart').addEventListener('click', e => { const box = e.currentTarget.getBoundingClientRect();
      selectTime(ex.tMin + ((e.clientX - box.left) / box.width * 700 - ex.x0) / ex.w * (ex.tMax - ex.tMin)); });
    read();
    const seek = $('#seek-video'), video = $('video');
    if (seek && video) seek.addEventListener('click', () => {
      const t = ex.samples[Number(slider.value)][0];
      const pos = Math.max(0, Math.min((ex.videoEnd || 1e9) - ex.videoOffset, t - ex.videoOffset));
      video.scrollIntoView({block:'center'});
      const go = () => { video.currentTime = pos; toast(`已定位录像 ${pos.toFixed(2)} s，点击播放即可。`); };
      if (video.readyState >= 1) go(); else { video.addEventListener('loadedmetadata', go, {once:true}); video.preload = 'metadata'; video.load(); }
      video.addEventListener('error', () => toast(`此浏览器无法解码录像；对应录像位置 ${pos.toFixed(2)} s。`), {once:true});
    });
  }
})();
"""
