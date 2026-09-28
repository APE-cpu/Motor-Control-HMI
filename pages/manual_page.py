"""任务卡式使用说明书：左侧目录树 + 搜索，右侧卡片。

卡片来源：manual/cards/*.md（任务卡）与 使用说明书.md（按二级标题拆成参考卡）。
页面只负责展示与发出请求信号；"在界面中定位""带我做"由主窗口的引导层执行。
"""
from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QTextBrowser,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.control_locator import PAGE_BY_KEY
from core.manual_cards import (
    GROUP_ORDER, PRINCIPLE_SECTION, REFERENCE_GROUP, Card, load_cards, search_cards,
)
from runtime_paths import resource_path

_CARD_LINK = re.compile(r"\[\[([A-Za-z0-9_\-]+)\]\]")
_ANCHOR_COLOR = re.compile(r'(<a [^>]*>\s*<span style="[^"]*?)color:#[0-9a-fA-F]{6}')
_LINK_COLOR = "#4fc3f7"
_SECTION_ORDER = ("预期", "失败时")


def _set_markdown(browser: QTextBrowser, text: str) -> None:
    """Markdown 导入时链接颜色取自应用调色板（深色底上是暗蓝），这里改成强调色。"""
    browser.ensurePolished()
    document = QTextDocument()
    document.setDefaultFont(browser.font())    # 否则 toHtml 会写死临时文档的 9 pt 字号
    document.setMarkdown(text)
    html = _ANCHOR_COLOR.sub(rf"\1color:{_LINK_COLOR}", document.toHtml())
    browser.setHtml(html)


class ManualPage(QWidget):
    locateRequested = Signal(str)     # 控件 id
    guideRequested = Signal(str)      # 卡片 id；空字符串表示界面总览
    pageRequested = Signal(str)       # 页面键

    def __init__(self) -> None:
        super().__init__()
        self._cards_dir = resource_path("manual", "cards")
        self._legacy_path = resource_path("使用说明书.md")
        self.cards: list[Card] = []
        self._by_id: dict[str, Card] = {}
        self._current: Card | None = None

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("使用说明书")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)
        tour = QPushButton("界面总览引导")
        tour.setToolTip("逐步高亮导航、连接、运行控制与状态栏；只说明，不会替你操作")
        tour.clicked.connect(lambda: self.guideRequested.emit(""))
        refresh = QPushButton("重新载入")
        refresh.clicked.connect(self.reload)
        header.addWidget(tour)
        header.addWidget(refresh)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索：连接、零偏、辨识…（Ctrl+F）")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._rebuild_tree)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemClicked.connect(self._on_tree_clicked)
        self._tree.itemActivated.connect(self._on_tree_clicked)
        self._hits = QLabel()
        self._hits.setStyleSheet("color:#8fa3b8;")
        left_layout.addWidget(self._search)
        left_layout.addWidget(self._tree, 1)
        left_layout.addWidget(self._hits)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self._card_title = QLabel()
        self._card_title.setStyleSheet("font-size:20px; font-weight:bold;")
        self._card_meta = QLabel()
        self._card_meta.setTextFormat(Qt.RichText)
        actions = QHBoxLayout()
        self._btn_locate = QPushButton("在界面中定位")
        self._btn_locate.clicked.connect(self._locate_current)
        self._btn_guide = QPushButton("带我做")
        self._btn_guide.setToolTip("按卡片步骤逐个高亮控件；需要通电的步骤由你亲手操作")
        self._btn_guide.clicked.connect(self._guide_current)
        actions.addWidget(self._btn_locate)
        actions.addWidget(self._btn_guide)
        actions.addStretch(1)
        self._browser = QTextBrowser()
        self._browser.setOpenLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor)
        self._principle_toggle = QToolButton()
        self._principle_toggle.setCheckable(True)
        self._principle_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._principle_toggle.setArrowType(Qt.RightArrow)
        self._principle_toggle.toggled.connect(self._toggle_principle)
        self._principle = QTextBrowser()
        self._principle.setOpenLinks(False)
        self._principle.anchorClicked.connect(self._on_anchor)
        self._principle.hide()
        # 全局 QTextEdit 规则是等宽字体（给日志用）；说明书正文改用界面字体
        for browser in (self._browser, self._principle):
            browser.setStyleSheet(
                "QTextBrowser { font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif;"
                " font-size: 14px; }")
        right_layout.addWidget(self._card_title)
        right_layout.addWidget(self._card_meta)
        right_layout.addLayout(actions)
        right_layout.addWidget(self._browser, 3)
        right_layout.addWidget(self._principle_toggle)
        right_layout.addWidget(self._principle, 2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 900])
        root.addWidget(splitter, 1)

        find = QShortcut(QKeySequence.Find, self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self.focus_search)
        self.reload()

    # ------------------------------------------------------------ 数据
    def reload(self) -> None:
        try:
            self.cards = load_cards(self._cards_dir, self._legacy_path)
        except (OSError, ValueError) as exc:
            self.cards = []
            self._browser.setPlainText(f"使用说明书读取失败：{exc}")
        self._by_id = {card.id: card for card in self.cards}
        self._rebuild_tree()
        current = self._current.id if self._current else None
        if current in self._by_id:
            self.open_card(current)
        elif self.cards:
            self.open_card(self.cards[0].id)

    def card(self, card_id: str) -> Card | None:
        return self._by_id.get(card_id)

    def cards_for_page(self, page_key: str) -> list[Card]:
        return [card for card in self.cards
                if card.page == page_key and card.group != REFERENCE_GROUP]

    # ------------------------------------------------------------ 目录与搜索
    def focus_search(self, text: str | None = None) -> None:
        if text is not None:
            self._search.setText(text)
        self._search.setFocus()
        self._search.selectAll()

    def search(self, query: str) -> None:
        """设置搜索框并打开最佳匹配的卡片。"""
        self._search.setText(query)
        hits = search_cards(self.cards, query)
        if hits:
            self.open_card(hits[0].card.id)

    def _rebuild_tree(self) -> None:
        self._tree.clear()
        query = self._search.text().strip()
        if query:
            hits = search_cards(self.cards, query)
            group = QTreeWidgetItem([f"搜索结果（{len(hits)}）"])
            self._tree.addTopLevelItem(group)
            for hit in hits:
                item = QTreeWidgetItem([hit.card.title])
                item.setData(0, Qt.UserRole, hit.card.id)
                item.setToolTip(0, hit.snippet)
                group.addChild(item)
            group.setExpanded(True)
            self._hits.setText(f"命中 {len(hits)} 张卡片" if hits else "没有匹配的卡片")
            return
        self._hits.setText(f"共 {len(self.cards)} 张卡片")
        groups = {name: QTreeWidgetItem([name]) for name in GROUP_ORDER}
        for card in self.cards:
            parent = groups.setdefault(card.group, QTreeWidgetItem([card.group]))
            item = QTreeWidgetItem([card.title])
            item.setData(0, Qt.UserRole, card.id)
            tags = [tag for tag in (card.duration, card.risk) if tag]
            if tags:
                item.setToolTip(0, " · ".join(tags))
            parent.addChild(item)
        for name, parent in groups.items():
            if parent.childCount():
                self._tree.addTopLevelItem(parent)
                parent.setExpanded(name != REFERENCE_GROUP)

    def _on_tree_clicked(self, item: QTreeWidgetItem) -> None:
        card_id = item.data(0, Qt.UserRole)
        if card_id:
            self.open_card(card_id)

    # ------------------------------------------------------------ 卡片
    def open_card(self, card_id: str) -> bool:
        card = self._by_id.get(card_id)
        if card is None:
            return False
        self._current = card
        is_task = card.group != REFERENCE_GROUP
        self._card_title.setText(card.title)
        self._card_meta.setText(self._meta_html(card))
        self._btn_locate.setVisible(is_task)
        self._btn_guide.setVisible(is_task)
        self._btn_locate.setEnabled(bool(card.page or card.controls))
        self._btn_guide.setEnabled(bool(card.steps))
        _set_markdown(self._browser, self._link_cards(self._body_markdown(card)))
        principle = card.sections.get(PRINCIPLE_SECTION, "")
        self._principle_toggle.setVisible(bool(principle))
        self._principle_toggle.setChecked(False)
        self._toggle_principle(False)
        _set_markdown(self._principle, self._link_cards(principle))
        return True

    @property
    def current_card(self) -> Card | None:
        return self._current

    @staticmethod
    def _meta_html(card: Card) -> str:
        parts = []
        if card.page and card.page in PAGE_BY_KEY:
            parts.append(f"页面：{PAGE_BY_KEY[card.page].title}")
        if card.duration:
            parts.append(card.duration)
        if card.risk:
            parts.append(f"<span style='color:#ffb74d;'>{card.risk}</span>")
        parts.append(f"<span style='color:#8fa3b8;'>{card.group}</span>")
        return " · ".join(parts)

    @staticmethod
    def _body_markdown(card: Card) -> str:
        blocks = [card.summary] if card.summary else []
        if card.steps:
            lines = ["### 步骤", ""]
            for index, step in enumerate(card.steps, 1):
                prefix = "**【需你亲手操作】** " if step.dangerous else ""
                locate = (f" [〔定位〕](control:{step.control_id})"
                          if step.control_id else "")
                lines.append(f"{index}. {prefix}{step.text}{locate}")
            blocks.append("\n".join(lines))
        names = [n for n in _SECTION_ORDER if n in card.sections]
        names += [n for n in card.sections
                  if n not in _SECTION_ORDER and n != PRINCIPLE_SECTION]
        for name in names:
            blocks.append(f"### {name}\n\n{card.sections[name]}")
        return "\n\n".join(blocks)

    def _link_cards(self, text: str) -> str:
        def replace(match):
            target = self._by_id.get(match.group(1))
            title = target.title if target else match.group(1)
            return f"[{title}](card:{match.group(1)})"
        return _CARD_LINK.sub(replace, text)

    def _toggle_principle(self, expanded: bool) -> None:
        self._principle.setVisible(expanded)
        self._principle_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._principle_toggle.setText(
            f"{PRINCIPLE_SECTION}（{'点击收起' if expanded else '点击展开'}）")

    def _locate_current(self) -> None:
        card = self._current
        if card is None:
            return
        if card.controls:
            self.locateRequested.emit(card.controls[0])
        elif card.page:
            self.pageRequested.emit(card.page)

    def _guide_current(self) -> None:
        if self._current is not None:
            self.guideRequested.emit(self._current.id)

    def _on_anchor(self, url: QUrl) -> None:
        scheme = url.scheme()
        target = url.toString().split(":", 1)[-1]
        if scheme == "card":
            self.open_card(target)
        elif scheme == "control":
            self.locateRequested.emit(target)
        elif scheme == "page":
            self.pageRequested.emit(target)
        elif scheme in ("http", "https"):
            QDesktopServices.openUrl(url)
