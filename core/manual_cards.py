"""任务卡式说明书：卡片解析与检索。

卡片文件（manual/cards/*.md）格式：

    # 标题 {#card-id}
    > 分组：快速上手 · 页面：communication · 耗时：约 2 分钟 · 风险：会连接控制板
    > 关键词：连接, 握手, 以太网

    可选的一段简介。

    ## 步骤
    1. 步骤文字 {控件: communication.connect}
    2. 会让电机通电的步骤 {控件: monitor.start, 危险}

    ## 预期
    ## 失败时
    ## 原理与公式        ← 页面上默认折叠

另外把旧版 使用说明书.md 的每个二级标题拆成一张"参考"卡，内容不丢、可被搜索。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

GROUP_ORDER = ("快速上手", "常用任务", "排障", "参考")
REFERENCE_GROUP = "参考"
PRINCIPLE_SECTION = "原理与公式"

_TITLE = re.compile(r"^#\s+(.+?)\s*\{#([A-Za-z0-9_\-]+)\}\s*$")
_STEP = re.compile(r"^\s*\d+[.、]\s*(.+?)\s*$")
_MARK = re.compile(r"\{控件[:：]\s*([^}]*)\}")
_META_SPLIT = re.compile(r"\s*[·|]\s*")


@dataclass(frozen=True)
class Step:
    text: str
    control_id: str | None = None
    dangerous: bool = False


@dataclass
class Card:
    id: str
    title: str
    group: str
    page: str | None = None
    duration: str = ""
    risk: str = ""
    keywords: tuple[str, ...] = ()
    summary: str = ""
    steps: list[Step] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    order: int = 0

    @property
    def controls(self) -> list[str]:
        return [step.control_id for step in self.steps if step.control_id]

    def search_text(self) -> str:
        parts = [self.title, " ".join(self.keywords), self.summary]
        parts += [step.text for step in self.steps]
        parts += list(self.sections.values())
        return "\n".join(parts)


def _parse_step(text: str) -> Step:
    match = _MARK.search(text)
    control_id, dangerous = None, False
    if match:
        for token in (t.strip() for t in re.split(r"[,，]", match.group(1))):
            if token in ("危险", "注意"):
                dangerous = True
            elif token:
                control_id = token
        text = _MARK.sub("", text).strip()
    return Step(text, control_id, dangerous)


def _parse_meta(line: str, card: Card) -> None:
    body = line.lstrip(">").strip()
    if body.startswith(("关键词：", "关键词:")):
        words = body.split("：", 1)[-1] if "：" in body else body.split(":", 1)[-1]
        card.keywords = tuple(w.strip() for w in re.split(r"[,，、]", words) if w.strip())
        return
    for item in _META_SPLIT.split(body):
        key, _, value = item.partition("：")
        if not value:
            key, _, value = item.partition(":")
        key, value = key.strip(), value.strip()
        if key == "分组":
            card.group = value
        elif key == "页面":
            card.page = value or None
        elif key == "耗时":
            card.duration = value
        elif key == "风险":
            card.risk = value


def parse_card(text: str, order: int = 0) -> Card:
    lines = text.splitlines()
    card = None
    section = None
    summary, buffers = [], {}
    for line in lines:
        if card is None:
            match = _TITLE.match(line)
            if match:
                card = Card(id=match.group(2), title=match.group(1).strip(),
                            group="常用任务", order=order)
            continue
        if (line.startswith(">") and section is None and
                not any(s.strip() for s in summary)):
            _parse_meta(line, card)
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            buffers.setdefault(section, [])
            continue
        if section is None:
            summary.append(line)
        elif section == "步骤" and _STEP.match(line):
            card.steps.append(_parse_step(_STEP.match(line).group(1)))
        else:
            buffers[section].append(line)
    if card is None:
        raise ValueError("卡片缺少形如 `# 标题 {#id}` 的首行")
    card.summary = "\n".join(summary).strip()
    card.sections = {name: "\n".join(body).strip()
                     for name, body in buffers.items()
                     if name != "步骤" and "\n".join(body).strip()}
    return card


def reference_cards(text: str, source_name: str = "使用说明书") -> list[Card]:
    """把旧版说明书按二级标题拆成参考卡。"""
    cards, title, body = [], None, []

    def flush():
        if title is not None:
            index = len(cards) + 1
            cards.append(Card(
                id=f"ref-{index:02d}", title=title, group=REFERENCE_GROUP,
                summary="\n".join(body).strip(), order=1000 + index,
                keywords=(source_name,)))

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            title, body = line[3:].strip(), []
        elif title is not None:
            body.append(line)
    flush()
    return cards


def load_cards(cards_dir: Path, legacy_manual: Path | None = None) -> list[Card]:
    cards = []
    if cards_dir.is_dir():
        for order, path in enumerate(sorted(cards_dir.glob("*.md"))):
            cards.append(parse_card(path.read_text(encoding="utf-8"), order))
    if legacy_manual is not None and legacy_manual.is_file():
        cards += reference_cards(legacy_manual.read_text(encoding="utf-8"))
    ids = [card.id for card in cards]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ValueError(f"卡片 id 重复：{', '.join(duplicated)}")
    group_rank = {name: i for i, name in enumerate(GROUP_ORDER)}
    cards.sort(key=lambda c: (group_rank.get(c.group, len(GROUP_ORDER)), c.order))
    return cards


@dataclass(frozen=True)
class SearchHit:
    card: Card
    score: int
    snippet: str


def search_cards(cards: list[Card], query: str, limit: int = 20) -> list[SearchHit]:
    """所有关键字都命中才算匹配；标题 > 关键词 > 正文。"""
    tokens = [t.lower() for t in query.split() if t.strip()]
    if not tokens:
        return []
    hits = []
    for card in cards:
        title = card.title.lower()
        keywords = " ".join(card.keywords).lower()
        body = card.search_text().lower()
        if not all(t in body or t in title or t in keywords for t in tokens):
            continue
        score = sum(10 if t in title else 5 if t in keywords else 1 for t in tokens)
        if card.group != REFERENCE_GROUP:
            score += 2
        snippet = ""
        for line in card.search_text().splitlines():
            if tokens[0] in line.lower():
                snippet = line.strip()
                break
        hits.append(SearchHit(card, score, snippet[:80]))
    hits.sort(key=lambda h: (-h.score, h.card.order))
    return hits[:limit]
