"""按任务卡步骤录制说明书动图：manual/media/<卡片id>.gif。

运行：python tools/generate_manual_gifs.py [卡片id ...]
在后台（offscreen）启动主窗口，用新手引导逐步高亮控件并截图，
第 0 帧是目标页面全貌，之后每一步放大到"高亮控件 + 说明气泡"附近。
界面改版后重跑即可更新；不连接任何设备，也不会点击任何控件。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
os.environ["HMI_NO_TOUR"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

WINDOW = (1600, 940)          # 录制时的主窗口尺寸
FRAME = (880, 520)            # 动图画幅
OVERVIEW_MS = 1800
STEP_MS = 2800
MEDIA_DIR = ROOT / "manual" / "media"


def pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def to_pil(image: QImage) -> Image.Image:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    from io import BytesIO
    return Image.open(BytesIO(bytes(data))).convert("RGB")


def fit(image: Image.Image) -> Image.Image:
    """等比缩放进固定画幅，四周补深色底。"""
    canvas = Image.new("RGB", FRAME, (16, 19, 26))
    scale = min(FRAME[0] / image.width, FRAME[1] / image.height)
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    resized = image.resize(size, Image.LANCZOS)
    canvas.paste(resized, ((FRAME[0] - size[0]) // 2, (FRAME[1] - size[1]) // 2))
    return canvas


def focus_crop(shot: QImage, guide) -> QImage:
    """裁出"高亮控件 + 说明气泡"周围的区域，保持画幅比例。"""
    bubble = QRect(guide._bubble.geometry())
    area = QRect(bubble)
    if not guide._hole.isNull():
        hole = QRect(guide._hole)
        if hole.width() > 700:            # 状态栏这类横贯整窗的控件，只取气泡附近一段
            center = min(max(bubble.center().x(), hole.left() + 350), hole.right() - 350)
            hole = QRect(center - 350, hole.top(), 700, hole.height())
        area = area.united(hole)
    area = area.adjusted(-150, -110, 150, 110)
    ratio = FRAME[0] / FRAME[1]
    if area.width() / area.height() < ratio:
        grow = int(area.height() * ratio) - area.width()
        area.adjust(-grow // 2, 0, grow - grow // 2, 0)
    else:
        grow = int(area.width() / ratio) - area.height()
        area.adjust(0, -grow // 2, 0, grow - grow // 2)
    bounds = shot.rect()
    area.moveLeft(min(max(area.left(), 0), max(0, bounds.width() - area.width())))
    area.moveTop(min(max(area.top(), 0), max(0, bounds.height() - area.height())))
    return shot.copy(area.intersected(bounds))


def banner(shot: QImage, text: str) -> QImage:
    image = shot.copy()
    painter = QPainter(image)
    painter.fillRect(QRect(0, 0, image.width(), 64), QColor(16, 19, 26, 225))
    font = QFont("Microsoft YaHei")
    font.setPixelSize(28)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#4fc3f7"))
    painter.drawText(QRect(24, 0, image.width() - 48, 64),
                     Qt.AlignLeft | Qt.AlignVCenter, text)
    painter.end()
    return image


def record(app, window, card) -> list[tuple[Image.Image, int]]:
    assist = window.assist
    frames = []
    assist.start_guide(card.id)
    guide = assist.guide
    pump(app, 0.6)
    guide.hide()                           # 第 0 帧：卡片所在页面的全貌，不带遮罩
    if card.page:
        assist.show_page(card.page)
    pump(app, 0.5)
    overview = banner(window.grab().toImage(),
                      f"{card.title} · 共 {len(card.steps)} 步")
    frames.append((fit(to_pil(overview)), OVERVIEW_MS))
    guide.show()
    guide.raise_()
    guide._enter_step()                    # 回到第 1 步所在页面
    for _index in range(len(card.steps)):
        pump(app, 0.55)                    # 等页面切换和滚动完成
        guide._update_geometry()
        pump(app, 0.1)
        shot = window.grab().toImage()
        frames.append((fit(to_pil(focus_crop(shot, guide))), STEP_MS))
        guide.next_step()
    if guide.active:
        guide.stop()
    return frames


def save_gif(frames, path: Path) -> None:
    images = [image.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
              for image, _ms in frames]
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=[ms for _image, ms in frames], loop=0, optimize=True)


def main(argv: list[str]) -> int:
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Microsoft YaHei", 9))
    from main_window import MainWindow

    window = MainWindow(enable_training=False)
    # 录制时关闭右侧诊断栏（不写入用户设置），给页面留出完整宽度
    toggle = window.appearance_bar.diagnostic_toggle
    toggle.blockSignals(True)
    toggle.setChecked(False)
    toggle.blockSignals(False)
    window._sync_diagnostic_sidebar()
    window.resize(*WINDOW)
    window.show()
    pump(app, 0.8)

    wanted = set(argv)
    cards = [card for card in window.manual_page.cards
             if card.steps and card.group != "参考" and (not wanted or card.id in wanted)]
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for card in cards:
        frames = record(app, window, card)
        path = MEDIA_DIR / f"{card.id}.gif"
        save_gif(frames, path)
        print(f"{path.relative_to(ROOT)}  {len(frames)} 帧  {path.stat().st_size / 1024:.0f} KB")
    window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
