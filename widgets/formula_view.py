"""公式排版：LaTeX 公式预渲染为 SVG，界面里整行显示、放不下时等比缩小，从不折行。

公式源写成 LaTeX（`Eq`），说明文字与公式分开（`Txt` / `Notes` / 公式旁的短注）。
SVG 由 `python tools/render_formulas.py` 用 matplotlib mathtext 生成到
assets/formulas/<哈希>.svg，运行时只需 QtSvg，不依赖 matplotlib。
公式笔色在 SVG 里是占位色，加载时替换为当前主题的正文色，随主题切换。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from runtime_paths import resource_path

PLACEHOLDER = "#123456"          # 渲染脚本写入 SVG 的笔色，运行时替换
_TEXT = "#e8f1ff"                # 旧蓝主题正文色，经 theme_color 映射到当前主题
_SHORT_NOTE = 14                 # 不超过这么多字的注释放在公式右侧，否则放下一行
_REGISTRY: dict[str, None] = {}  # 已声明的全部 LaTeX，供渲染脚本收集


def formula_key(latex: str) -> str:
    return hashlib.sha1(latex.encode("utf-8")).hexdigest()[:16]


def formula_path(latex: str):
    return resource_path("assets", "formulas", f"{formula_key(latex)}.svg")


def registered_formulas() -> list[str]:
    return list(_REGISTRY)


# ─── 内容块 ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Sec:
    """小节标题。"""
    title: str


@dataclass(frozen=True)
class Eq:
    """一行公式（LaTeX，不含 $）与可选的简短中文注释。"""
    latex: str
    note: str = ""

    def __post_init__(self) -> None:
        _REGISTRY[self.latex] = None


@dataclass(frozen=True)
class Txt:
    """正文说明（富文本，可换行）。"""
    html: str


@dataclass(frozen=True)
class Warn:
    """警告说明（橙色）。"""
    html: str


class Notes:
    """参数说明列表。"""

    def __init__(self, *items: str) -> None:
        self.items = items


def plain_text(blocks) -> str:
    """把内容块拼成纯文本（测试与无障碍用）。"""
    parts = []
    for block in blocks:
        if isinstance(block, Sec):
            parts.append(block.title)
        elif isinstance(block, Eq):
            parts.append(block.latex + (f"  {block.note}" if block.note else ""))
        elif isinstance(block, (Txt, Warn)):
            parts.append(block.html)
        elif isinstance(block, Notes):
            parts.extend(block.items)
    return "\n".join(parts)


def _theme_color(source: str) -> QColor:
    try:
        from ui_theme import theme_color
        return theme_color(source)
    except Exception:  # noqa: BLE001 - 主题模块不可用时退回原色
        return QColor(source)


# ─── 单个公式 ─────────────────────────────────────────────────
class FormulaImage(QWidget):
    """显示一条预渲染公式；宽度不够时整体等比缩小，保持单行。"""

    MIN_SCALE = 0.55

    def __init__(self, latex: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.latex = latex
        self._svg = ""
        self._color = ""
        self._natural = QSize(0, 0)
        self._renderer = QSvgRenderer(self)
        path = formula_path(latex)
        if path.exists():
            self._svg = path.read_text(encoding="utf-8")
        self.setToolTip(latex)
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._recolor()

    @property
    def rendered(self) -> bool:
        return self._renderer.isValid()

    def _recolor(self) -> None:
        # 不连接全局主题信号（避免已销毁的控件收到通知）；绘制时发现主题色变了再重载
        self._color = _theme_color(_TEXT).name()
        if self._svg:
            self._renderer.load(self._svg.replace(PLACEHOLDER, self._color).encode("utf-8"))
        if self._renderer.isValid():
            box = self._renderer.viewBoxF()
            self._natural = QSize(round(box.width()), round(box.height()))
        else:                                   # 缺 SVG：退回等宽 LaTeX 源码
            metrics = self.fontMetrics()
            self._natural = QSize(metrics.horizontalAdvance(self.latex) + 4,
                                  metrics.height() + 4)
        self.updateGeometry()
        self.update()

    def _scale(self, width: int) -> float:
        if self._natural.width() <= 0:
            return 1.0
        return max(self.MIN_SCALE, min(1.0, width / self._natural.width()))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self._natural

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(round(self._natural.width() * self.MIN_SCALE),
                     round(self._natural.height() * self.MIN_SCALE))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return round(self._natural.height() * self._scale(width))

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        if _theme_color(_TEXT).name() != self._color:
            self._recolor()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        scale = self._scale(self.width())
        w = self._natural.width() * scale
        h = self._natural.height() * scale
        target = QRectF(0, (self.height() - h) / 2, w, h)
        if self._renderer.isValid():
            self._renderer.render(painter, target)
        else:
            painter.setPen(_theme_color(_TEXT))
            painter.drawText(target, Qt.AlignLeft | Qt.AlignVCenter, self.latex)


# ─── 公式说明页 ───────────────────────────────────────────────
class FormulaSheet(QWidget):
    """按内容块排版：标题、公式卡（连续公式合为一张深色卡片）、说明、列表。"""

    def __init__(self, blocks=(), parent: QWidget | None = None, *,
                 compact: bool = False) -> None:
        super().__init__(parent)
        self._compact = compact
        self._blocks: list = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4 if compact else 6)
        if blocks:
            self.set_blocks(blocks)

    def blocks(self) -> list:
        return list(self._blocks)

    def plain_text(self) -> str:
        return plain_text(self._blocks)

    def formula_images(self) -> list[FormulaImage]:
        return self.findChildren(FormulaImage)

    def set_blocks(self, blocks) -> None:
        self._blocks = list(blocks)
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先隐藏再交给 Qt 延迟删除，避免旧公式在删除前继续叠画
                widget.hide()
                widget.deleteLater()
        group: list[Eq] = []
        for block in self._blocks:
            if isinstance(block, Eq):
                group.append(block)
                continue
            if group:
                self._layout.addWidget(self._equation_card(group))
                group = []
            self._layout.addWidget(self._block_widget(block))
        if group:
            self._layout.addWidget(self._equation_card(group))
        self._layout.addStretch(1)

    # ---------------------------------------------------------- 各类块
    def _label(self, html: str, style: str) -> QLabel:
        label = QLabel(html)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet(style)
        return label

    def _block_widget(self, block) -> QWidget:
        if isinstance(block, Sec):
            return self._label(
                block.title,
                "QLabel { color:#4fc3f7; font-weight:bold; font-size:14px;"
                " margin-top:8px; }")
        if isinstance(block, Warn):
            return self._label(
                block.html, "QLabel { color:#ff8a65; font-size:13px; margin:4px 0; }")
        if isinstance(block, Notes):
            items = "".join(f"<li style='margin:4px 0;'>{item}</li>"
                            for item in block.items)
            return self._label(
                f"<ul style='margin:0; -qt-list-indent:1;'>{items}</ul>",
                "QLabel { color:#aebccb; font-size:13px; }")
        size = 12 if self._compact else 13
        return self._label(
            block.html, f"QLabel {{ color:#c7d3e0; font-size:{size}px; margin:2px 0; }}")

    def _equation_card(self, equations: list[Eq]) -> QFrame:
        card = QFrame()
        card.setObjectName("FormulaCard")
        card.setStyleSheet(
            "#FormulaCard { background-color:#10131a; border:1px solid #2c3442;"
            " border-radius:6px; }")
        layout = QVBoxLayout(card)
        pad = 8 if self._compact else 12
        layout.setContentsMargins(pad, pad - 2, pad, pad - 2)
        layout.setSpacing(6 if self._compact else 8)
        for eq in equations:
            image = FormulaImage(eq.latex)
            note = None
            if eq.note:
                note = QLabel(eq.note)
                note.setWordWrap(len(eq.note) > _SHORT_NOTE)
                note.setStyleSheet(
                    "QLabel { color:#8fa3b8; font-size:12px; background:transparent; }")
            if note is not None and len(eq.note) <= _SHORT_NOTE:
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(14)
                row.addWidget(image, 1)
                note.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                row.addWidget(note, 0)
                layout.addLayout(row)
            else:
                layout.addWidget(image)
                if note is not None:
                    layout.addWidget(note)
        return card
