"""实验日志的离屏图像：矢量合成动图、滑动频谱动图、桑基功率流动图与静态频谱图。

全部用 QPainter 画到 QImage，再用 PIL 存成 GIF/PNG，不依赖窗口显示。
桑基图借用 PowerSankey.render_frame，需要在 GUI 线程调用。
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

BG = QColor("#15181e")
GRID = QColor("#2c3442")
TEXT = QColor("#dfe6ee")
MUTED = QColor("#8fa3b8")
PALETTE = ("#ffb74d", "#4fc3f7", "#81c784", "#f48fb1", "#ce93d8", "#fff176",
           "#80cbc4", "#ff8a65", "#9fa8da", "#bcaaa4")


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


def _canvas(width: int, height: int) -> tuple[QImage, QPainter]:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(BG)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    font = QFont("Microsoft YaHei")
    font.setPixelSize(12)
    painter.setFont(font)
    return image, painter


# ─── 矢量合成 ────────────────────────────────────────────────
def phasor_frames(analysis, width: int = 480, height: int = 440, frames: int = 72,
                  max_components: int = 8) -> tuple[list[QImage], list[str]]:
    """取窗口中段的一个电周期，首尾相接画各分量；返回帧与被省略的分量说明。"""
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
    reach = max(sum(c.amp for c in shown), 1e-6)
    scale = 0.42 * min(width, height - 40) / reach
    cx, cy = width / 2, (height + 24) / 2
    colors = {c.key: QColor(PALETTE[i % len(PALETTE)]) for i, c in enumerate(analysis.components)}
    trail_times = np.linspace(t_mid - period, t_mid + period, 400)
    tip_path = sum(analysis.series(c, trail_times) for c in shown)
    measured = analysis.measured_between(t_mid - period, t_mid + period)

    def point(z: complex) -> QPointF:
        return QPointF(cx + z.real * scale, cy - z.imag * scale)

    images = []
    for index in range(frames):
        t = t_mid + index * step
        image, qp = _canvas(width, height)
        qp.setPen(QPen(GRID, 1))
        qp.drawLine(QPointF(0, cy), QPointF(width, cy))
        qp.drawLine(QPointF(cx, 24), QPointF(cx, height))
        qp.setPen(QPen(QColor(255, 255, 255, 50), 1))
        for a, b in zip(measured[:-1:2], measured[1::2]):
            qp.drawLine(point(a), point(b))
        path = QPainterPath(point(complex(tip_path[0])))
        for z in tip_path[1:]:
            path.lineTo(point(complex(z)))
        qp.setPen(QPen(QColor("#e0f7fa"), 1.4))
        qp.drawPath(path)
        start = 0j
        for comp in shown:
            vector = complex(analysis.series(comp, np.array([t]))[0])
            color = colors[comp.key]
            orbit = QColor(color)
            orbit.setAlpha(90)
            qp.setPen(QPen(orbit, 1, Qt.DotLine))
            radius = abs(vector) * scale
            qp.drawEllipse(point(start), radius, radius)
            _arrow(qp, point(start), point(start + vector), color)
            start += vector
        qp.setPen(TEXT)
        qp.drawText(QRectF(8, 4, width - 16, 20), Qt.AlignLeft,
                    f"电流矢量合成  t = {t:.4f} s  ·  θe 一周 {frames} 帧")
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

    qp.setPen(QPen(QColor(GRID.lighter(130)), 1, Qt.DotLine))
    for tick in np.linspace(lo, hi, 6):
        p = xy(tick, 0)
        qp.drawLine(p, QPointF(p.x(), rect.top()))
        qp.setPen(MUTED)
        qp.drawText(QRectF(p.x() - 40, rect.bottom() + 2, 80, 16), Qt.AlignHCenter,
                    f"{tick:.0f}")
        qp.setPen(QPen(QColor(GRID.lighter(130)), 1, Qt.DotLine))
    path = QPainterPath(xy(f[0], a_db[0])) if f.size else QPainterPath()
    for freq, level in zip(f[1:], a_db[1:]):
        path.lineTo(xy(freq, level))
    qp.setPen(QPen(QColor("#4fc3f7"), 1.2))
    qp.drawPath(path)
    for freq, label in markers:
        if lo <= freq <= hi:
            p = xy(freq, span)
            qp.setPen(QPen(QColor("#ffb74d"), 1, Qt.DashLine))
            qp.drawLine(p, QPointF(p.x(), rect.bottom()))
            qp.setPen(QColor("#ffb74d"))
            qp.drawText(QRectF(p.x() + 3, rect.top() + 2, 90, 16), Qt.AlignLeft, label)
    qp.setPen(TEXT)
    qp.drawText(QRectF(rect.left(), rect.top() - 20, rect.width(), 18), Qt.AlignLeft,
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
        qp.setPen(QPen(GRID.lighter(130), 1, Qt.DotLine))
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
        qp.setPen(QPen(color, 1.2))
        qp.drawPath(path)
        qp.drawLine(QPointF(rect.right() - 230, rect.top() + 14 + 16 * index),
                    QPointF(rect.right() - 206, rect.top() + 14 + 16 * index))
        qp.setPen(TEXT)
        qp.drawText(QRectF(rect.right() - 200, rect.top() + 6 + 16 * index, 196, 16),
                    Qt.AlignLeft, label)
    qp.setPen(TEXT)
    qp.drawText(QRectF(rect.left(), 8, rect.width(), 20), Qt.AlignLeft,
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
        qp.setPen(QPen(GRID.lighter(130), 1, Qt.DotLine))
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
        qp.drawLine(QPointF(rect.right() - 230, rect.top() + 14 + 16 * index),
                    QPointF(rect.right() - 206, rect.top() + 14 + 16 * index))
        qp.setPen(TEXT)
        qp.drawText(QRectF(rect.right() - 200, rect.top() + 6 + 16 * index, 196, 16),
                    Qt.AlignLeft, label)
    qp.setPen(TEXT)
    qp.drawText(QRectF(rect.left(), 8, rect.width(), 20), Qt.AlignLeft, f"{title}（{unit}）")
    qp.setPen(MUTED)
    qp.drawText(QRectF(rect.left(), rect.bottom() + 4, rect.width(), 16), Qt.AlignHCenter,
                f"各自记录的相对时间 0–{t_hi:.1f} s")
    qp.end()
    return image


# ─── 功率流 ──────────────────────────────────────────────────
def sankey_frames(series: dict, vbus: float, frames: int = 48, width: int = 640,
                  height: int = 360) -> list[QImage]:
    """整段功率沿时间回放的桑基图（需在 GUI 线程调用）。"""
    from core.power_estimate import powers_at
    from widgets.power_sankey import PowerSankey
    sankey = PowerSankey()
    times = np.linspace(series["time"][0], series["time"][-1], frames)
    images = []
    for index, t in enumerate(times):
        sankey.set_data(powers_at(series, float(t)), vbus, "normal")
        sankey._phase = index * 0.04 * 3
        image = sankey.render_frame(width, height, "#15181e")
        qp = QPainter(image)
        qp.setPen(TEXT)
        font = QFont("Microsoft YaHei")
        font.setPixelSize(12)
        qp.setFont(font)
        qp.drawText(QRectF(8, 4, width - 16, 18), Qt.AlignLeft, f"功率流  t = {t:.2f} s")
        qp.end()
        images.append(image)
    sankey.deleteLater()
    return images
