"""实验日志的离屏图像：矢量合成动图、滑动频谱动图、桑基功率流动图与各类静态图。

全部用 QPainter 画到 QImage，再用 PIL 存成 GIF/PNG，不依赖窗口显示，可在后台线程调用。
配色与实验日志版式一致：暖白纸面、深灰绿文字、青绿为主数据线、铜色为强调与参考线。
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

BG = QColor("#fffefa")          # 纸面
GRID = QColor("#d5dcd4")        # 边框、坐标轴
GRID_DOT = QColor("#e6ebe3")    # 网格细线
TEXT = QColor("#24312f")        # 正文
MUTED = QColor("#74817b")       # 次要文字
LINE = QColor("#39786b")        # 主数据线（青绿）
ACCENT = QColor("#af6948")      # 强调、参考线（铜色）
SECOND = QColor("#4f6d8a")      # 第二数据色（蓝灰）
OLIVE = QColor("#6b8f3c")
PLUM = QColor("#8a5a7a")
FAINT = QColor(36, 49, 47, 45)  # 原始/实测底图
PALETTE = ("#39786b", "#af6948", "#4f6d8a", "#8a5a7a", "#b8902f", "#6b8f3c",
           "#a4553a", "#5d7f8f", "#7d6b5a", "#c07f9a")


def _to_pil(image: QImage) -> Image.Image:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return Image.open(io.BytesIO(bytes(data))).convert("RGB")


def save_gif(images: list[QImage], path: Path, frame_ms: int = 70) -> Path:
    frames = [_to_pil(image).quantize(colors=96, method=Image.MEDIANCUT, dither=Image.NONE)
              for image in images]
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=frame_ms,
                   loop=0, optimize=True)
    return path


def save_png(image: QImage, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path), "PNG")
    return path


def _font(size: int = 12, bold: bool = False) -> QFont:
    font = QFont("Microsoft YaHei")
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def _canvas(width: int, height: int) -> tuple[QImage, QPainter]:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(BG)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setFont(_font(12))
    return image, painter


def _title(qp: QPainter, rect: QRectF, text: str) -> None:
    qp.setPen(TEXT)
    qp.setFont(_font(12, True))
    qp.drawText(rect, Qt.AlignLeft, text)
    qp.setFont(_font(12))


# ─── 矢量合成 ────────────────────────────────────────────────
def phasor_frames(analysis, width: int = 480, height: int = 440, frames: int = 72,
                  max_components: int = 8, follow: bool = False) -> tuple[list[QImage], list[str]]:
    """取窗口中段的一个电周期，各分量首尾相接旋转；返回帧与被省略的分量说明。

    follow=True：以基波箭头末端为中心放大其余分量（小分量只有基波的百分之几时才看得清）。
    """
    fe = abs(analysis.fe_hz) if abs(analysis.fe_hz) >= 1.0 else 50.0
    period = 1.0 / fe
    t_mid = float(analysis.time[analysis.time.size // 2])
    step = period / frames
    # 每帧转角超过 90° 的分量在动图里会混叠，省略并在说明里写明
    limit = 0.25 / step
    shown, omitted = [], []
    for comp in analysis.components:
        if abs(comp.freq_hz) > limit:
            omitted.append(f"{comp.label} {comp.freq_hz:+.0f} Hz")
        elif len(shown) < max_components:
            shown.append(comp)
    following = follow and len(shown) > 1 and shown[0].order == 1
    reach = max(sum(c.amp for c in (shown[1:] if following else shown)), 1e-6)
    scale = 0.40 * min(width, height - 40) / reach
    cx, cy = width / 2, (height + 24) / 2
    colors = {c.key: QColor(PALETTE[i % len(PALETTE)]) for i, c in enumerate(analysis.components)}
    trail_times = np.linspace(t_mid - period, t_mid + period, 400)
    tip_path = sum(analysis.series(c, trail_times) for c in shown)
    measured = analysis.measured_between(t_mid - period, t_mid + period)

    images = []
    for index in range(frames):
        t = t_mid + index * step
        vectors = [complex(analysis.series(c, np.array([t]))[0]) for c in shown]
        focus = vectors[0] if following else 0j

        def point(z: complex) -> QPointF:
            z = z - focus
            return QPointF(cx + z.real * scale, cy - z.imag * scale)

        image, qp = _canvas(width, height)
        qp.setPen(QPen(GRID, 1))
        origin = point(0j)
        qp.drawLine(QPointF(0, origin.y()), QPointF(width, origin.y()))
        qp.drawLine(QPointF(origin.x(), 24), QPointF(origin.x(), height))
        qp.setPen(QPen(FAINT, 1))
        for a, b in zip(measured[:-1:2], measured[1::2]):
            qp.drawLine(point(a), point(b))
        path = QPainterPath(point(complex(tip_path[0])))
        for z in tip_path[1:]:
            path.lineTo(point(complex(z)))
        qp.setPen(QPen(TEXT, 1.3))
        qp.drawPath(path)
        start = 0j
        for comp, vector in zip(shown, vectors):
            color = colors[comp.key]
            orbit = QColor(color)
            orbit.setAlpha(90)
            qp.setPen(QPen(orbit, 1, Qt.DotLine))
            radius = abs(vector) * scale
            if radius < 4 * width:
                qp.drawEllipse(point(start), radius, radius)
            _arrow(qp, point(start), point(start + vector), color)
            start += vector
        title = "跟随基波末端（放大小分量）" if following else "全局视图"
        _title(qp, QRectF(8, 4, width - 16, 20), f"电流矢量合成 · {title} · t = {t:.4f} s")
        qp.setFont(_font(11))
        for row, comp in enumerate(shown):
            y = 28 + 15 * row
            qp.fillRect(QRectF(width - 170, y + 4, 10, 6), colors[comp.key])
            qp.setPen(TEXT)
            qp.drawText(QRectF(width - 156, y, 152, 14), Qt.AlignLeft,
                        f"{comp.label[:8]} {comp.amp:.3g} A {comp.freq_hz:+.0f} Hz")
        qp.end()
        images.append(image)
    return images, omitted


def _arrow(qp: QPainter, a: QPointF, b: QPointF, color: QColor) -> None:
    qp.setPen(QPen(color, 2.2))
    qp.drawLine(a, b)
    dx, dy = b.x() - a.x(), b.y() - a.y()
    length = math.hypot(dx, dy)
    if length < 4:
        return
    ux, uy = dx / length, dy / length
    size = min(9.0, 0.35 * length)
    for sign in (1, -1):
        angle = sign * 0.45
        wx = -(ux * math.cos(angle) - uy * math.sin(angle)) * size
        wy = -(ux * math.sin(angle) + uy * math.cos(angle)) * size
        qp.drawLine(b, QPointF(b.x() + wx, b.y() + wy))


# ─── 频谱 ────────────────────────────────────────────────────
def _spectrum_panel(qp: QPainter, rect: QRectF, freqs, amps, title: str,
                    f_range: tuple[float, float], unit: str, markers=()) -> None:
    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    lo, hi = f_range
    mask = (freqs >= lo) & (freqs <= hi)
    f, a = freqs[mask], amps[mask]
    top = max(float(a.max()) if a.size else 1.0, 1e-9) * 1.1
    # 对数纵轴显示小峰：以最大值下 60 dB 为底
    floor = top * 1e-3
    a_db = 20 * np.log10(np.maximum(a, floor) / floor)
    span = 20 * np.log10(top / floor)

    def xy(freq, level):
        x = rect.left() + (freq - lo) / (hi - lo) * rect.width()
        y = rect.bottom() - level / span * rect.height()
        return QPointF(x, y)

    for tick in np.linspace(lo, hi, 6):
        p = xy(tick, 0)
        qp.setPen(QPen(GRID_DOT, 1))
        qp.drawLine(p, QPointF(p.x(), rect.top()))
        qp.setPen(MUTED)
        qp.drawText(QRectF(p.x() - 40, rect.bottom() + 2, 80, 16), Qt.AlignHCenter,
                    f"{tick:.0f}")
    path = QPainterPath(xy(f[0], a_db[0])) if f.size else QPainterPath()
    for freq, level in zip(f[1:], a_db[1:]):
        path.lineTo(xy(freq, level))
    qp.setPen(QPen(LINE, 1.1))
    qp.drawPath(path)
    for freq, label in markers:
        if lo <= freq <= hi:
            p = xy(freq, span)
            qp.setPen(QPen(ACCENT, 1, Qt.DashLine))
            qp.drawLine(p, QPointF(p.x(), rect.bottom()))
            qp.setPen(ACCENT)
            qp.drawText(QRectF(p.x() + 3, rect.top() + 2, 90, 16), Qt.AlignLeft, label)
    _title(qp, QRectF(rect.left(), rect.top() - 20, rect.width(), 18),
           f"{title}（对数幅值，满格 {top:.3g} {unit}，底部 −60 dB）")
    qp.setPen(MUTED)
    qp.drawText(QRectF(rect.left(), rect.bottom() + 16, rect.width(), 16), Qt.AlignHCenter,
                "频率 / Hz")


def iq_spectrum(values, rate: float, remove_dc: bool = True):
    from pages.fourier_page import compute_spectrum
    spec = compute_spectrum(values, rate, window_name="hann", remove_dc=remove_dc)
    return np.asarray(spec["frequencies"]), np.asarray(spec["amplitudes"])


def complex_spectrum(ia, ib, rate: float):
    alpha = np.asarray(ia, float)
    beta = (alpha + 2.0 * np.asarray(ib, float)) / math.sqrt(3.0)
    z = alpha + 1j * beta
    hann = np.hanning(z.size)
    spectrum = np.fft.fftshift(np.fft.fft(z * hann)) / hann.sum()
    return np.fft.fftshift(np.fft.fftfreq(z.size, 1.0 / rate)), np.abs(spectrum)


def spectrum_image(window, fe_hz: float | None, width: int = 900, height: int = 520) -> QImage:
    """静态频谱：上 iq 单边谱，下电流复数双边谱（正负频率区分正负序）。"""
    image, qp = _canvas(width, height)
    fe = abs(fe_hz) if fe_hz else 0.0
    top_span = max(1000.0, 14 * fe)
    markers = [(k * fe, f"{k}fe") for k in (1, 2, 6) if fe] if fe else []
    if "iq_a" in window.extra:
        f, a = iq_spectrum(window.extra["iq_a"], window.rate_hz)
        _spectrum_panel(qp, QRectF(56, 34, width - 80, height / 2 - 70), f, a,
                        "iq 频谱（单边）", (0.0, top_span), "A", markers)
    f, a = complex_spectrum(window.ia, window.ib, window.rate_hz)
    cmarkers = [(k * fe, f"{k:+d}fe") for k in (1, -1, -5, 7)] if fe else []
    _spectrum_panel(qp, QRectF(56, height / 2 + 30, width - 80, height / 2 - 70), f, a,
                    "电流复数频谱 iα+jiβ（双边：正频率=正序，负频率=负序）",
                    (-top_span, top_span), "A", cmarkers)
    qp.end()
    return image


def sliding_spectrum_frames(window, fe_hz: float | None, frames: int = 40,
                            block_s: float = 0.5, width: int = 640,
                            height: int = 300) -> list[QImage]:
    """iq 频谱随时间的变化：0.5 s 窗口从头滑到尾。"""
    if "iq_a" not in window.extra:
        return []
    t, iq = window.time, window.extra["iq_a"]
    rate = window.rate_hz
    size = int(block_s * rate)
    if t.size < size * 2:
        size = t.size // 2
    starts = np.linspace(0, t.size - size, frames).astype(int)
    fe = abs(fe_hz) if fe_hz else 0.0
    span = max(1000.0, 14 * fe)
    markers = [(k * fe, f"{k}fe") for k in (1, 2, 6)] if fe else []
    images = []
    for start in starts:
        f, a = iq_spectrum(iq[start:start + size], rate)
        image, qp = _canvas(width, height)
        _spectrum_panel(qp, QRectF(56, 34, width - 80, height - 80), f, a,
                        f"iq 滑动频谱  {t[start]:.2f}–{t[start + size - 1]:.2f} s",
                        (0.0, span), "A", markers)
        qp.end()
        images.append(image)
    return images


# ─── 对比实验叠加图 ──────────────────────────────────────────
def _legend(qp: QPainter, rect: QRectF, index: int, color: QColor, label: str) -> None:
    y = rect.top() + 14 + 16 * index
    qp.setPen(QPen(color, 2))
    qp.drawLine(QPointF(rect.right() - 230, y), QPointF(rect.right() - 206, y))
    qp.setPen(TEXT)
    qp.drawText(QRectF(rect.right() - 200, y - 8, 196, 16), Qt.AlignLeft, label)


def overlay_spectrum_image(entries: list[tuple[str, np.ndarray, np.ndarray]],
                           title: str, f_range: tuple[float, float],
                           width: int = 900, height: int = 340) -> QImage:
    """多个实验的频谱叠在一张图上（对数幅值，统一纵轴）。"""
    image, qp = _canvas(width, height)
    rect = QRectF(56, 34, width - 80, height - 84)
    lo, hi = f_range
    top = max((float(a[(f >= lo) & (f <= hi)].max()) for _l, f, a in entries
               if a[(f >= lo) & (f <= hi)].size), default=1.0) * 1.1
    floor = top * 1e-3
    span = 20 * math.log10(top / floor)
    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    for tick in np.linspace(lo, hi, 6):
        x = rect.left() + (tick - lo) / (hi - lo) * rect.width()
        qp.setPen(QPen(GRID_DOT, 1))
        qp.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        qp.setPen(MUTED)
        qp.drawText(QRectF(x - 40, rect.bottom() + 2, 80, 16), Qt.AlignHCenter, f"{tick:.0f}")
    for index, (label, f, a) in enumerate(entries):
        color = QColor(PALETTE[index % len(PALETTE)])
        mask = (f >= lo) & (f <= hi)
        level = 20 * np.log10(np.maximum(a[mask], floor) / floor)
        xs = rect.left() + (f[mask] - lo) / (hi - lo) * rect.width()
        ys = rect.bottom() - level / span * rect.height()
        path = QPainterPath()
        for i, (x, y) in enumerate(zip(xs, ys)):
            (path.moveTo if i == 0 else path.lineTo)(QPointF(float(x), float(y)))
        qp.setPen(QPen(color, 1.1))
        qp.drawPath(path)
        _legend(qp, rect, index, color, label)
    _title(qp, QRectF(rect.left(), 8, rect.width(), 20),
           f"{title}（对数幅值，满格 {top:.3g}，底部 −60 dB）")
    qp.setPen(MUTED)
    qp.drawText(QRectF(rect.left(), rect.bottom() + 18, rect.width(), 16), Qt.AlignHCenter,
                "频率 / Hz")
    qp.end()
    return image


def overlay_series_image(entries: list[tuple[str, np.ndarray, np.ndarray]], title: str,
                         unit: str, width: int = 900, height: int = 300) -> QImage:
    """多个实验的时间曲线叠加（横轴为各自记录的相对时间）。"""
    image, qp = _canvas(width, height)
    rect = QRectF(64, 34, width - 88, height - 80)
    all_t = np.concatenate([t - t[0] for _l, t, _v in entries]) if entries else np.array([0, 1])
    all_v = np.concatenate([v for _l, _t, v in entries]) if entries else np.array([0, 1])
    t_hi = float(all_t.max()) or 1.0
    v_lo, v_hi = float(all_v.min()), float(all_v.max())
    if v_hi - v_lo < 1e-9:
        v_lo, v_hi = v_lo - 1.0, v_hi + 1.0
    pad = 0.06 * (v_hi - v_lo)
    v_lo, v_hi = v_lo - pad, v_hi + pad
    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    for k in range(5):
        value = v_lo + k * (v_hi - v_lo) / 4
        y = rect.bottom() - k * rect.height() / 4
        qp.setPen(QPen(GRID_DOT, 1))
        qp.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        qp.setPen(MUTED)
        qp.drawText(QRectF(4, y - 8, 56, 16), Qt.AlignRight, f"{value:.4g}")
    for index, (label, t, v) in enumerate(entries):
        color = QColor(PALETTE[index % len(PALETTE)])
        step = max(1, t.size // 1500)
        xs = rect.left() + (t[::step] - t[0]) / t_hi * rect.width()
        ys = rect.bottom() - (v[::step] - v_lo) / (v_hi - v_lo) * rect.height()
        path = QPainterPath()
        for i, (x, y) in enumerate(zip(xs, ys)):
            (path.moveTo if i == 0 else path.lineTo)(QPointF(float(x), float(y)))
        qp.setPen(QPen(color, 1.2))
        qp.drawPath(path)
        _legend(qp, rect, index, color, label)
    _title(qp, QRectF(rect.left(), 8, rect.width(), 20), f"{title}（{unit}）")
    qp.setPen(MUTED)
    qp.drawText(QRectF(rect.left(), rect.bottom() + 4, rect.width(), 16), Qt.AlignHCenter,
                f"各自记录的相对时间 0–{t_hi:.1f} s")
    qp.end()
    return image


# ─── 功率流 ──────────────────────────────────────────────────
def sankey_frames(series: dict, vbus: float, frames: int = 48, width: int = 640,
                  height: int = 360, titles: dict | None = None) -> list[QImage]:
    """整段功率沿时间回放的桑基图（纯绘制，可在后台线程调用）。"""
    from core.power_estimate import powers_at
    from widgets.power_sankey import SankeyPainter
    sankey = SankeyPainter("paper", titles)
    times = np.linspace(series["time"][0], series["time"][-1], frames)
    images = []
    for index, t in enumerate(times):
        sankey.set_data(powers_at(series, float(t)), vbus, "normal")
        sankey._phase = index * 0.04 * 3
        image = sankey.render_frame(width, height, BG.name())
        qp = QPainter(image)
        qp.setRenderHint(QPainter.Antialiasing, True)
        _title(qp, QRectF(8, 4, width - 16, 18), f"功率流  t = {t:.2f} s")
        qp.end()
        images.append(image)
    return images


def sankey_mean_image(mean_powers: dict, vbus: float, subtitle: str, width: int = 640,
                      height: int = 360, titles: dict | None = None) -> QImage:
    """按统计窗平均功率画的静态桑基图（打印版用，与平均功率表一致）。"""
    from widgets.power_sankey import SankeyPainter
    sankey = SankeyPainter("paper", titles)
    sankey.set_data(dict(mean_powers), vbus, "normal")
    image = sankey.render_frame(width, height, BG.name())
    qp = QPainter(image)
    qp.setRenderHint(QPainter.Antialiasing, True)
    _title(qp, QRectF(8, 4, width - 16, 18), f"功率流 · {subtitle}")
    qp.end()
    return image


# ─── 热图、电流圆、阶跃响应、分量柱状图 ────────────────────────
_VIRIDIS = ((0.0, (68, 1, 84)), (0.25, (59, 82, 139)), (0.5, (33, 145, 140)),
            (0.75, (94, 201, 98)), (1.0, (253, 231, 37)))


def _viridis(level: float) -> QColor:
    level = min(max(level, 0.0), 1.0)
    for (a, ca), (b, cb) in zip(_VIRIDIS[:-1], _VIRIDIS[1:]):
        if level <= b:
            k = (level - a) / (b - a)
            return QColor(*(int(x + (y - x) * k) for x, y in zip(ca, cb)))
    return QColor(*_VIRIDIS[-1][1])


def heatmap_image(time, axis, power, title: str, ylabel: str, width: int = 900,
                  height: int = 340, dynamic_db: float = 60.0) -> QImage:
    """时频热图（STFT / 阶次图）：纵轴 axis，横轴 time，颜色为相对峰值的 dB。"""
    image, qp = _canvas(width, height)
    rect = QRectF(64, 30, width - 130, height - 76)
    power = np.asarray(power, float)
    finite = power[np.isfinite(power)]
    peak = float(finite.max()) if finite.size else 1.0
    db = 10 * np.log10(np.maximum(power, peak * 10 ** (-dynamic_db / 10)) / max(peak, 1e-30))
    rows, cols = db.shape
    lut = np.array([_viridis(i / 255).getRgb()[:3] for i in range(256)], dtype=np.uint8)
    level = np.clip(np.nan_to_num(1 + db / dynamic_db, nan=-1.0), -1.0, 1.0)
    index = np.clip((level * 255).astype(int), 0, 255)
    rgb = lut[index[::-1]]                                   # 纵轴向上
    rgb[level[::-1] < 0] = (BG.red(), BG.green(), BG.blue())  # 无效区（NaN）留底色
    rgba = np.ascontiguousarray(np.dstack([rgb, np.full((rows, cols), 255, np.uint8)]))
    cell = QImage(rgba.data, cols, rows, cols * 4, QImage.Format_RGBA8888).copy()
    qp.drawImage(rect, cell)
    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    qp.setPen(MUTED)
    t0, t1 = float(time[0]), float(time[-1])
    a0, a1 = float(axis[0]), float(axis[-1])
    for k in range(6):
        x = rect.left() + k / 5 * rect.width()
        qp.drawText(QRectF(x - 40, rect.bottom() + 2, 80, 16), Qt.AlignHCenter,
                    f"{t0 + k / 5 * (t1 - t0):.1f}")
        y = rect.bottom() - k / 5 * rect.height()
        qp.drawText(QRectF(2, y - 8, 58, 16), Qt.AlignRight, f"{a0 + k / 5 * (a1 - a0):.4g}")
    qp.drawText(QRectF(rect.left(), rect.bottom() + 18, rect.width(), 16), Qt.AlignHCenter,
                "时间 / s")
    qp.save()
    qp.translate(12, rect.center().y())
    qp.rotate(-90)
    qp.drawText(QRectF(-80, -8, 160, 16), Qt.AlignCenter, ylabel)
    qp.restore()
    bar = QRectF(rect.right() + 14, rect.top(), 14, rect.height())
    for i in range(int(bar.height())):
        qp.fillRect(QRectF(bar.left(), bar.bottom() - i - 1, bar.width(), 1),
                    _viridis(i / bar.height()))
    qp.drawText(QRectF(bar.right() + 2, bar.top() - 2, 40, 14), Qt.AlignLeft, "0 dB")
    qp.drawText(QRectF(bar.right() + 2, bar.bottom() - 12, 44, 14), Qt.AlignLeft,
                f"−{dynamic_db:.0f}")
    _title(qp, QRectF(rect.left(), 6, rect.width(), 20), f"{title}（颜色为相对本图峰值的 dB）")
    qp.end()
    return image


def current_circle_image(alpha, beta, shape=None, width: int = 460, height: int = 460,
                         max_points: int = 20000) -> QImage:
    """αβ 电流圆：稳态段点云 + 平均半径参考圆 + 圆度分解数字。"""
    image, qp = _canvas(width, height)
    alpha, beta = np.asarray(alpha, float), np.asarray(beta, float)
    step = max(1, alpha.size // max_points)
    a, b = alpha[::step], beta[::step]
    reach = max(float(np.max(np.hypot(a, b))) if a.size else 1.0, 1e-6) * 1.12
    cx, cy = width / 2, (height + 20) / 2
    scale = 0.46 * min(width, height - 30) / reach
    qp.setPen(QPen(GRID, 1))
    qp.drawLine(QPointF(0, cy), QPointF(width, cy))
    qp.drawLine(QPointF(cx, 24), QPointF(cx, height))
    dot = QColor(LINE)
    dot.setAlpha(30)
    qp.setPen(QPen(dot, 1.6))
    for x, y in zip(a, b):
        qp.drawPoint(QPointF(cx + x * scale, cy - y * scale))
    if shape is not None:
        radius = shape.mean_radius * scale
        qp.setPen(QPen(ACCENT, 1.2, Qt.DashLine))
        qp.drawEllipse(QPointF(cx, cy), radius, radius)
    _title(qp, QRectF(8, 4, width - 16, 20), f"αβ 电流圆（稳态段 {alpha.size} 点，虚线为平均半径）")
    if shape is not None:
        qp.setFont(_font(11))
        qp.setPen(TEXT)
        lines = (f"平均半径 {shape.mean_radius:.3f} A",
                 f"偏心 {shape.eccentric_pct:.1f}%  椭圆 {shape.ellipse_pct:.1f}%",
                 f"三角 {shape.triangle_pct:.1f}%  六边形 {shape.hexagon_pct:.1f}%")
        for row, line in enumerate(lines):
            qp.drawText(QRectF(10, height - 56 + 16 * row, width - 20, 16), Qt.AlignLeft, line)
    qp.end()
    return image


def response_image(result: dict, title: str, unit: str, width: int = 900,
                   height: int = 320) -> QImage:
    """阶跃响应：原始与平滑曲线、事件线、10%/90% 与调节带。"""
    image, qp = _canvas(width, height)
    rect = QRectF(64, 30, width - 90, height - 76)
    t = np.asarray(result["time"], float)
    y = np.asarray(result["values"], float)
    f = np.asarray(result["filtered"], float)
    lo, hi = float(np.min(y)), float(np.max(y))
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = 0.08 * (hi - lo)
    lo, hi = lo - pad, hi + pad

    def xy(tt, vv):
        return QPointF(rect.left() + (tt - t[0]) / max(t[-1] - t[0], 1e-9) * rect.width(),
                       rect.bottom() - (vv - lo) / (hi - lo) * rect.height())

    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    metrics = result["metrics"]
    if result.get("step_valid"):
        band = abs(metrics["target"] - metrics["baseline"]) * result["band"]
        for level in (metrics["target"] - band, metrics["target"] + band):
            qp.setPen(QPen(MUTED, 1, Qt.DotLine))
            qp.drawLine(xy(t[0], level), xy(t[-1], level))
    step = max(1, t.size // 2000)
    for values, color, width_px in ((y, FAINT, 1.0), (f, LINE, 1.6)):
        path = QPainterPath(xy(t[0], values[0]))
        for tt, vv in zip(t[::step], values[::step]):
            path.lineTo(xy(tt, vv))
        qp.setPen(QPen(color, width_px))
        qp.drawPath(path)
    for mark, color in ((result.get("event"), ACCENT), (result.get("t10"), OLIVE),
                        (result.get("t90"), OLIVE)):
        if mark is not None:
            qp.setPen(QPen(color, 1, Qt.DashLine))
            qp.drawLine(xy(mark, lo), xy(mark, hi))
    qp.setPen(MUTED)
    for k in range(5):
        value = lo + k * (hi - lo) / 4
        point = xy(t[0], value)
        qp.drawText(QRectF(2, point.y() - 8, 58, 16), Qt.AlignRight, f"{value:.4g}")
    qp.drawText(QRectF(rect.left(), rect.bottom() + 4, rect.width(), 16), Qt.AlignHCenter,
                f"时间 {t[0]:.2f}–{t[-1]:.2f} s（铜色：事件，绿：10%/90%，点线：调节带）")
    _title(qp, QRectF(rect.left(), 6, rect.width(), 20), f"{title}（{unit}）")
    qp.end()
    return image


def component_bars_image(components, width: int = 900, height: int = 260) -> QImage:
    """各分量占基波百分比的柱状图，按静止系频率排列，颜色区分正序/负序/静止。"""
    image, qp = _canvas(width, height)
    rect = QRectF(64, 30, width - 90, height - 80)
    items = sorted((c for c in components if c.order != 1), key=lambda c: c.freq_hz)
    fundamental = next((c.amp for c in components if c.order == 1), 1.0) or 1.0
    top = max([100 * c.amp / fundamental for c in items] + [1.0]) * 1.15
    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    if items:
        slot = rect.width() / len(items)
        for index, comp in enumerate(items):
            pct = 100 * comp.amp / fundamental
            color = QColor("#a9b3ad") if abs(comp.freq_hz) < 1e-6 else \
                (ACCENT if comp.freq_hz > 0 else LINE)
            bar_h = pct / top * rect.height()
            bar = QRectF(rect.left() + index * slot + slot * 0.2, rect.bottom() - bar_h,
                         slot * 0.6, bar_h)
            qp.fillRect(bar, color)
            qp.setPen(TEXT)
            qp.drawText(QRectF(bar.left() - 20, bar.top() - 16, bar.width() + 40, 14),
                        Qt.AlignHCenter, f"{pct:.1f}%")
            qp.setPen(MUTED)
            label = comp.key.replace("k", "k=") if comp.order is not None else f"{comp.freq_hz:+.0f}Hz"
            qp.drawText(QRectF(rect.left() + index * slot, rect.bottom() + 2, slot, 28),
                        Qt.AlignHCenter | Qt.TextWordWrap, label)
    _title(qp, QRectF(rect.left(), 6, rect.width(), 20),
           "各分量幅值（占基波 %）· 铜色=正序 青绿=负序 灰=静止矢量")
    qp.end()
    return image


def torque_speed_image(model_rpm, model_torque, model_current, blocks: dict, steady_mask,
                       title: str = "对拖负载：转矩-转速", width: int = 900,
                       height: int = 380) -> QImage:
    """短路制动模型曲线（转矩、负载相电流峰值）与实测块平均点（稳速 / 变速）。"""
    image, qp = _canvas(width, height)
    rect = QRectF(64, 34, width - 140, height - 84)
    rpm_model = np.asarray(model_rpm, float)
    torque_model = np.asarray(model_torque, float)
    current_model = np.asarray(model_current, float)
    valid = np.asarray(blocks["valid"], bool)
    rpm_meas = np.asarray(blocks["omega"], float)[valid] * 60 / (2 * math.pi)
    torque_meas = np.asarray(blocks["torque"], float)[valid]
    steady = np.asarray(steady_mask, bool)[valid]
    x_max = float(rpm_model[-1])
    t_max = max(float(torque_model.max()), float(torque_meas.max(initial=0.0))) * 1.12 or 1.0
    t_min = min(0.0, float(torque_meas.min(initial=0.0)) * 1.12)
    i_max = float(current_model.max()) * 1.12 or 1.0

    def xy(rpm, value, lo, hi):
        return QPointF(rect.left() + rpm / x_max * rect.width(),
                       rect.bottom() - (value - lo) / (hi - lo) * rect.height())

    qp.setPen(QPen(GRID, 1))
    qp.drawRect(rect)
    qp.setPen(QPen(GRID_DOT, 1))
    for k in range(1, 5):
        y = rect.top() + k * rect.height() / 5
        qp.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
    for values, lo, hi, color, style in (
            (current_model, 0.0, i_max, PLUM, Qt.DashLine),
            (torque_model, t_min, t_max, ACCENT, Qt.SolidLine)):
        path = QPainterPath(xy(rpm_model[0], values[0], lo, hi))
        for rpm, value in zip(rpm_model[1:], values[1:]):
            path.lineTo(xy(rpm, value, lo, hi))
        qp.setPen(QPen(color, 2, style))
        qp.drawPath(path)
    transient = QColor(SECOND)
    transient.setAlpha(90)
    for mask, color in ((~steady, transient), (steady, LINE)):
        qp.setPen(Qt.NoPen)
        qp.setBrush(color)
        for rpm, value in zip(rpm_meas[mask], torque_meas[mask]):
            qp.drawEllipse(xy(rpm, value, t_min, t_max), 2.6, 2.6)
    qp.setBrush(Qt.NoBrush)
    qp.setPen(MUTED)
    for k in range(6):
        value = t_min + k * (t_max - t_min) / 5
        point = xy(0, value, t_min, t_max)
        qp.drawText(QRectF(2, point.y() - 8, 58, 16), Qt.AlignRight, f"{value:.3f}")
        current = k * i_max / 5
        qp.drawText(QRectF(rect.right() + 6, point.y() - 8, 60, 16), Qt.AlignLeft, f"{current:.1f} A")
    for k in range(6):
        rpm = k * x_max / 5
        point = xy(rpm, t_min, t_min, t_max)
        qp.drawText(QRectF(point.x() - 30, rect.bottom() + 4, 60, 16), Qt.AlignHCenter, f"{rpm:.0f}")
    qp.drawText(QRectF(rect.left(), rect.bottom() + 22, rect.width(), 16), Qt.AlignHCenter,
                "转速 rpm　　铜色线：短路制动转矩（左轴 N·m）　紫虚线：负载相电流峰值（右轴）　"
                "青绿点：实测稳速　蓝灰点：实测变速")
    _title(qp, QRectF(rect.left(), 8, rect.width(), 20), title)
    qp.end()
    return image
