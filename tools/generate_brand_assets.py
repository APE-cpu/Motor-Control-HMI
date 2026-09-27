"""生成上位机品牌资源：应用图标（.ico/.png）与启动画面底图。

运行：python tools/generate_brand_assets.py
输出：assets/app_icon.ico、assets/app_icon.png、assets/splash.png

全部用 QPainter 矢量绘制，改配色或造型后重新运行即可再生成；
版本号、加载状态和进度条由 widgets/splash_screen.py 在运行时叠加绘制。
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

# 与 config/style.qss 深色科技风保持一致
CYAN = QColor("#4fc3f7")
CYAN_LIGHT = QColor("#8ee3ff")
BLUE = QColor("#1976d2")
TEXT = QColor("#ffffff")
TEXT_DIM = QColor("#8fa3b8")

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
SPLASH_SIZE = (720, 420)   # 逻辑像素
SPLASH_SCALE = 2           # 按 2 倍像素渲染，高分屏不发虚


# ---------------------------------------------------------------- 图标
def paint_icon(p: QPainter, s: float) -> None:
    """在 s×s 区域内绘制图标：定子齿圈 + 正弦电流波形。"""
    p.setRenderHint(QPainter.Antialiasing, True)
    small = s < 40

    # 圆角方形底板
    inset = 0 if small else s * 0.03
    body = QRectF(inset, inset, s - 2 * inset, s - 2 * inset)
    radius = body.width() * 0.22
    bg = QLinearGradient(body.topLeft(), body.bottomLeft())
    bg.setColorAt(0.0, QColor("#26324a"))
    bg.setColorAt(1.0, QColor("#0d1117"))
    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)
    p.fillPath(path, bg)
    if not small:
        glow = QRadialGradient(QPointF(s * 0.5, s * 0.42), s * 0.55)
        glow.setColorAt(0.0, QColor(79, 195, 247, 46))
        glow.setColorAt(1.0, QColor(79, 195, 247, 0))
        p.fillPath(path, glow)
        p.setPen(QPen(QColor(79, 195, 247, 110), max(1.0, s / 96)))
        p.drawPath(path)

    c = QPointF(s / 2, s / 2)
    ring_r = s * (0.31 if small else 0.30)
    ring_w = s * (0.10 if small else 0.068)
    ring_grad = QLinearGradient(QPointF(s * 0.2, s * 0.15), QPointF(s * 0.8, s * 0.85))
    ring_grad.setColorAt(0.0, CYAN_LIGHT)
    ring_grad.setColorAt(1.0, BLUE)

    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QBrush(ring_grad), ring_w, Qt.SolidLine, Qt.RoundCap))
    p.drawEllipse(c, ring_r, ring_r)

    # 正弦电流波形，穿过转子中心
    span = ring_r * (0.72 if small else 0.66)
    amp = ring_r * (0.42 if small else 0.40)
    wave = QPainterPath()
    steps = 64
    for i in range(steps + 1):
        x = -span + 2 * span * i / steps
        y = -amp * math.sin(math.pi * x / span)
        pt = QPointF(c.x() + x, c.y() + y)
        if i == 0:
            wave.moveTo(pt)
        else:
            wave.lineTo(pt)
    wave_w = s * (0.10 if small else 0.062)
    if not small:
        p.setPen(QPen(QColor(79, 195, 247, 38), wave_w * 2.0,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(wave)
    wave_grad = QLinearGradient(QPointF(c.x() - span, 0), QPointF(c.x() + span, 0))
    wave_grad.setColorAt(0.0, CYAN)
    wave_grad.setColorAt(0.5, TEXT)
    wave_grad.setColorAt(1.0, CYAN)
    p.setPen(QPen(QBrush(wave_grad), wave_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(wave)


def render_icon(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    paint_icon(p, float(size))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: Path, images: list[QImage]) -> None:
    """多尺寸 ICO（每个条目内嵌 PNG，Windows Vista 起支持）。"""
    blobs = [png_bytes(img) for img in images]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for img, blob in zip(images, blobs):
        w = img.width()
        entries += struct.pack("<BBBBHHII", w % 256, w % 256, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
    path.write_bytes(header + entries + b"".join(blobs))


# ---------------------------------------------------------------- 启动画面
def _three_phase(p: QPainter, rect: QRectF, amp: float, periods: float,
                 width: float) -> None:
    colors = (QColor(79, 195, 247, 80), QColor(25, 118, 210, 70),
              QColor(38, 198, 218, 60))
    for k, color in enumerate(colors):
        path = QPainterPath()
        steps = 240
        for i in range(steps + 1):
            x = rect.left() + rect.width() * i / steps
            ph = 2 * math.pi * periods * i / steps - k * 2 * math.pi / 3
            y = rect.center().y() - amp * math.sin(ph)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)


def _annular_sector(c: QPointF, r_in: float, r_out: float,
                    start_deg: float, span_deg: float) -> QPainterPath:
    outer = QRectF(c.x() - r_out, c.y() - r_out, 2 * r_out, 2 * r_out)
    inner = QRectF(c.x() - r_in, c.y() - r_in, 2 * r_in, 2 * r_in)
    path = QPainterPath()
    path.arcMoveTo(outer, start_deg)
    path.arcTo(outer, start_deg, span_deg)
    path.arcTo(inner, start_deg + span_deg, -span_deg)
    path.closeSubpath()
    return path


def _motor_section(p: QPainter, c: QPointF) -> None:
    line = QColor(79, 195, 247, 70)
    fill = QColor(79, 195, 247, 22)
    r_yoke_out, r_yoke_in, r_tip = 152.0, 132.0, 96.0

    # 定子：轭部圆环与 12 个带齿靴的齿合成一个轮廓
    stator = QPainterPath()
    stator.addEllipse(c, r_yoke_out, r_yoke_out)
    stator = stator.subtracted(_circle(c, r_yoke_in))
    for k in range(12):
        ang = k * 30.0
        stator = stator.united(_annular_sector(c, r_tip + 8, r_yoke_in + 1, ang - 6, 12))
        stator = stator.united(_annular_sector(c, r_tip, r_tip + 9, ang - 12, 24))
    p.setPen(QPen(line, 1.2))
    p.setBrush(fill)
    p.drawPath(stator)

    # 转子：8 块磁钢交替 N/S
    r_mag_out, r_mag_in = 88.0, 74.0
    for k in range(8):
        n_pole = k % 2 == 0
        p.setBrush(QColor(79, 195, 247, 60 if n_pole else 26))
        p.setPen(QPen(QColor(79, 195, 247, 80), 1))
        p.drawPath(_annular_sector(c, r_mag_in, r_mag_out, k * 45 + 3, 39))
    p.setBrush(QColor(79, 195, 247, 14))
    p.setPen(QPen(line, 1))
    p.drawEllipse(c, r_mag_in, r_mag_in)
    p.setBrush(QColor(13, 16, 22, 200))
    p.drawEllipse(c, 16, 16)

    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(79, 195, 247, 28), 1))
    p.drawEllipse(c, 176, 176)


def _circle(c: QPointF, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(c, r, r)
    return path


def render_splash() -> QImage:
    w, h = SPLASH_SIZE
    img = QImage(w * SPLASH_SCALE, h * SPLASH_SCALE, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(SPLASH_SCALE)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    frame = QRectF(0, 0, w, h)
    card = QPainterPath()
    card.addRoundedRect(frame.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
    p.setClipPath(card)

    bg = QLinearGradient(frame.topLeft(), frame.bottomRight())
    bg.setColorAt(0.0, QColor("#1a2130"))
    bg.setColorAt(1.0, QColor("#0d1016"))
    p.fillRect(frame, bg)

    # 细网格（示波器底纹）
    p.setPen(QPen(QColor(79, 195, 247, 12), 1))
    for x in range(0, w + 1, 24):
        p.drawLine(x, 0, x, h)
    for y in range(0, h + 1, 24):
        p.drawLine(0, y, w, y)

    # 右侧电机截面装饰：12 槽定子 + 8 极表贴磁钢转子
    deco_c = QPointF(w * 0.80, h * 0.38)
    halo = QRadialGradient(deco_c, 200)
    halo.setColorAt(0.0, QColor(79, 195, 247, 36))
    halo.setColorAt(1.0, QColor(79, 195, 247, 0))
    p.fillRect(frame, halo)
    _motor_section(p, deco_c)

    # 三相正弦电流
    _three_phase(p, QRectF(0, h * 0.60, w, 60), 26, 2.5, 2.0)

    # 底部渐隐，为运行时进度条留出干净区域
    fade = QLinearGradient(QPointF(0, h * 0.62), QPointF(0, h))
    fade.setColorAt(0.0, QColor(13, 16, 22, 0))
    fade.setColorAt(1.0, QColor(13, 16, 22, 235))
    p.fillRect(frame, fade)

    # 左侧图标 + 标题
    icon_size = 84
    p.save()
    p.translate(44, 64)
    paint_icon(p, icon_size)
    p.restore()

    title_font = QFont("Microsoft YaHei", 1)
    title_font.setPixelSize(34)
    title_font.setBold(True)
    p.setFont(title_font)
    p.setPen(TEXT)
    p.drawText(QRectF(148, 66, 420, 46), Qt.AlignLeft | Qt.AlignVCenter, "电机控制上位机")

    sub_font = QFont("Segoe UI", 1)
    sub_font.setPixelSize(15)
    sub_font.setLetterSpacing(QFont.AbsoluteSpacing, 2.2)
    p.setFont(sub_font)
    p.setPen(CYAN)
    p.drawText(QRectF(150, 112, 420, 24), Qt.AlignLeft | Qt.AlignVCenter,
               "MOTOR CONTROL HMI")

    tag_font = QFont("Microsoft YaHei", 1)
    tag_font.setPixelSize(13)
    p.setFont(tag_font)
    p.setPen(TEXT_DIM)
    p.drawText(QRectF(46, 172, 460, 22), Qt.AlignLeft | Qt.AlignVCenter,
               "PMSM 矢量控制 · 实时监控 · 参数辨识 · 数字孪生 · 智能诊断")

    # 标题下方强调线
    accent = QLinearGradient(QPointF(46, 0), QPointF(346, 0))
    accent.setColorAt(0.0, CYAN)
    accent.setColorAt(1.0, QColor(79, 195, 247, 0))
    p.fillRect(QRectF(46, 160, 300, 2), accent)

    p.setClipping(False)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(79, 195, 247, 70), 1))
    p.drawPath(card)
    p.end()
    return img


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    ASSETS.mkdir(exist_ok=True)
    icons = [render_icon(s) for s in ICON_SIZES]
    write_ico(ASSETS / "app_icon.ico", icons)
    render_icon(512).save(str(ASSETS / "app_icon.png"))
    render_splash().save(str(ASSETS / "splash.png"))
    for name in ("app_icon.ico", "app_icon.png", "splash.png"):
        print(f"{ASSETS / name}  {(ASSETS / name).stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
