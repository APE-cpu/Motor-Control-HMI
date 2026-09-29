"""功率流桑基图：能量带宽度 ∝ 功率，损耗支路向下分流，粒子沿能量方向流动。

拓扑（驱动方向为正）：
  电源 ─┬─► 直流母线 ─┬─► 逆变器/电机电气 ─┬─► 电磁功率 ─┬─► 负载与摩擦
        └─► 内阻损耗  └─► 制动电阻         └─► 定子铜损  └─► 转子动能
回馈制动时 inv/em 为负：能量带变蓝，粒子反向（从转轴流回母线再进制动电阻）。
接口与旧图一致：set_active(bool)、set_data(powers, vdc, bus_state)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import QWidget

_FWD = QColor("#ffb74d")        # 驱动方向能量
_REV = QColor("#4fc3f7")        # 回馈能量
_LOSS = QColor("#ef5350")       # 损耗
_STORE = QColor("#b39ddb")      # 动能储存/释放
_IDLE = QColor("#546e7a")
_TEXT = QColor("#dfe6ee")
_MUTED = QColor("#90a4ae")
_NODE = QColor("#37474f")

# 配色：dark 为上位机界面；paper 为实验日志（暖白纸面，青绿为主、铜色为损耗）
PALETTES = {
    "dark": {"fwd": _FWD, "rev": _REV, "loss": _LOSS, "store": _STORE, "idle": _IDLE,
             "text": _TEXT, "muted": _MUTED, "node": _NODE.lighter(170), "dot_lighter": 170},
    "paper": {"fwd": QColor("#39786b"), "rev": QColor("#4f6d8a"), "loss": QColor("#af6948"),
              "store": QColor("#8a5a7a"), "idle": QColor("#b7c0b8"), "text": QColor("#24312f"),
              "muted": QColor("#74817b"), "node": QColor("#5d6b66"), "dot_lighter": 125},
}

# 待机时的示意数值（只用来画灰色骨架，不显示数值）
_IDLE_POWERS = {"supply": 100.0, "loss_src": 4.0, "inv": 90.0, "brake": 0.0,
                "cu": 12.0, "em": 78.0, "fric": 70.0, "kinetic": 8.0}


@dataclass
class _Node:
    key: str
    title: str
    x: float            # 相对宽度 0~1
    y: float            # 节点顶部，相对高度 0~1
    sink: bool = False
    top: float = 0.0    # 像素（布局后填）
    height: float = 0.0
    out_used: float = 0.0
    in_used: float = 0.0


@dataclass
class _Link:
    source: str
    target: str
    value: float        # 带符号功率；负值表示能量逆着箭头方向流
    kind: str           # main / loss / store


class SankeyPainter:
    """桑基图的数据与绘制，不依赖 QWidget：实验日志可在后台线程画成 QImage。"""
    _BAR_W = 14.0

    def __init__(self, palette: str = "dark", titles: dict | None = None) -> None:
        self._powers: dict = {}
        self._vdc = 0.0
        self._bus_state = "normal"
        self._phase = 0.0
        self._colors = PALETTES[palette]
        self._titles = dict(titles or {})     # 节点标题覆盖，如对拖时 load → 负载电机铜损

    def set_data(self, powers: dict, vdc: float, bus_state: str) -> None:
        self._powers = dict(powers or {})
        self._vdc = float(vdc or 0.0)
        self._bus_state = bus_state
        self.update()

    def update(self) -> None:           # 纯绘制对象无需刷新窗口
        pass

    @property
    def idle(self) -> bool:
        return not self._powers

    def _tick(self) -> None:
        self._phase = (self._phase + 0.04) % 1000.0
        if self._powers and self.isVisible():
            self.update()

    # ------------------------------------------------------------ 拓扑
    @staticmethod
    def _graph(p: dict) -> tuple[list[_Node], list[_Link]]:
        g = lambda key: float(p.get(key, 0.0))  # noqa: E731
        nodes = [
            _Node("supply", "电源", 0.03, 0.24),
            _Node("bus", "直流母线", 0.27, 0.24),
            _Node("inv", "逆变器·电机电气", 0.51, 0.24),
            _Node("em", "电磁功率", 0.74, 0.24),
            _Node("load", "负载与摩擦", 0.955, 0.24, sink=True),
            _Node("kin", "转子动能", 0.955, 0.62, sink=True),
            _Node("loss_src", "电源内阻", 0.17, 0.80, sink=True),
            _Node("brake", "制动电阻", 0.41, 0.80, sink=True),
            _Node("cu", "定子铜损", 0.64, 0.80, sink=True),
        ]
        links = [
            _Link("supply", "bus", g("supply") - g("loss_src"), "main"),
            _Link("supply", "loss_src", g("loss_src"), "loss"),
            _Link("bus", "inv", g("inv"), "main"),
            _Link("bus", "brake", g("brake"), "loss"),
            _Link("inv", "em", g("em"), "main"),
            _Link("inv", "cu", g("cu"), "loss"),
            _Link("em", "load", g("fric"), "main"),
            _Link("em", "kin", g("kinetic"), "store"),
        ]
        if abs(g("load_cu")) > 1e-6:
            # 对拖且负载驱动器上电未启动：负载电机短路铜损单独成支路，其余才是摩擦与负载
            nodes[4].title = "摩擦与其余负载"
            nodes.append(_Node("load_cu", "负载电机铜损", 0.86, 0.80, sink=True))
            links.append(_Link("em", "load_cu", g("load_cu"), "loss"))
        return nodes, links

    # ------------------------------------------------------------ 绘制
    def render_frame(self, width: int, height: int, background: str = "#1b1c1c"):
        """离屏画一帧（实验日志动图用），不需要显示窗口。"""
        from PySide6.QtGui import QImage
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(QColor(background))
        qp = QPainter(image)
        self.paint(qp, float(width), float(height))
        qp.end()
        return image

    def paint(self, qp: QPainter, w: float, h: float) -> None:
        qp.setRenderHint(QPainter.Antialiasing, True)
        idle = self.idle
        powers = _IDLE_POWERS if idle else self._powers
        nodes, links = self._graph(powers)
        by_key = {node.key: node for node in nodes}

        # 节点高度 = 流经功率；统一比例尺，最大节点占可用高度约 30%
        through = {}
        for node in nodes:
            outgoing = sum(abs(link.value) for link in links if link.source == node.key)
            incoming = sum(abs(link.value) for link in links if link.target == node.key)
            through[node.key] = max(outgoing, incoming)
        peak = max(max(through.values()), 1e-6)
        scale = 0.30 * h / peak
        for node in nodes:
            node.height = max(2.0, through[node.key] * scale)
            node.top = node.y * h
            if node.sink and node.y > 0.5:
                # 底部的损耗/动能汇点向上收，给标签和底部效率说明留出位置
                node.top = min(node.top, h - 78.0 - node.height)

        for link in links:
            self._draw_link(qp, link, by_key, scale, w, idle)
        for node in nodes:
            self._draw_node(qp, node, w, powers, idle)
        self._draw_header(qp, w, h, powers, idle)

    def _link_color(self, link: _Link, idle: bool) -> QColor:
        c = self._colors
        if idle:
            return QColor(c["idle"])
        if link.kind == "loss":
            color = QColor(c["loss"])
            if link.target == "brake" and abs(link.value) > 1.0:   # 泄放中：呼吸闪烁
                color.setAlphaF(0.55 + 0.45 * math.sin(self._phase * 6.0))
            return color
        if link.kind == "store":
            return QColor(c["store"])
        return QColor(c["fwd"] if link.value >= 0 else c["rev"])

    def _draw_link(self, qp: QPainter, link: _Link, nodes: dict, scale: float,
                   w: float, idle: bool) -> None:
        src, dst = nodes[link.source], nodes[link.target]
        thickness = max(1.5, abs(link.value) * scale)
        x1 = src.x * w + self._BAR_W / 2
        x2 = dst.x * w - self._BAR_W / 2
        y1 = src.top + src.out_used
        y2 = dst.top + dst.in_used
        src.out_used += thickness
        dst.in_used += thickness
        if abs(link.value) < 1e-9 and not idle:
            return
        color = self._link_color(link, idle)
        dx = (x2 - x1) * 0.5
        path = QPainterPath(QPointF(x1, y1))
        path.cubicTo(QPointF(x1 + dx, y1), QPointF(x2 - dx, y2), QPointF(x2, y2))
        path.lineTo(QPointF(x2, y2 + thickness))
        path.cubicTo(QPointF(x2 - dx, y2 + thickness), QPointF(x1 + dx, y1 + thickness),
                     QPointF(x1, y1 + thickness))
        path.closeSubpath()
        gradient = QLinearGradient(QPointF(x1, 0), QPointF(x2, 0))
        start, end = QColor(color), QColor(color)
        start.setAlpha(105 if not idle else 55)
        end.setAlpha(55 if not idle else 35)
        gradient.setColorAt(0.0, start)
        gradient.setColorAt(1.0, end)
        qp.setPen(Qt.NoPen)
        qp.setBrush(QBrush(gradient))
        qp.drawPath(path)
        # 上下亮边，让能量带轮廓清楚
        edge = QColor(color)
        edge.setAlpha(210 if not idle else 90)
        qp.setPen(QPen(edge, 1.2))
        qp.setBrush(Qt.NoBrush)
        for offset in (0.0, thickness):
            rim = QPainterPath(QPointF(x1, y1 + offset))
            rim.cubicTo(QPointF(x1 + dx, y1 + offset), QPointF(x2 - dx, y2 + offset),
                        QPointF(x2, y2 + offset))
            qp.drawPath(rim)
        if idle:
            return
        # 能量粒子：分布在带宽内的多条流道上，速度 ∝ 功率，负功率反向
        speed = 0.12 + 0.45 * min(abs(link.value), 300.0) / 300.0
        count = max(3, min(28, int(thickness / 3.5) + 3))
        dot = QColor(color).lighter(self._colors["dot_lighter"])
        radius = min(3.2, 1.3 + thickness / 30.0)
        qp.setPen(Qt.NoPen)
        for i in range(count):
            lane = 0.5 + (((i * 0.618034) % 1.0) - 0.5) * 0.8
            y_a, y_b = y1 + thickness * lane, y2 + thickness * lane
            track = QPainterPath(QPointF(x1, y_a))
            track.cubicTo(QPointF(x1 + dx, y_a), QPointF(x2 - dx, y_b), QPointF(x2, y_b))
            t = (self._phase * speed + i / count) % 1.0
            if link.value < 0:
                t = 1.0 - t
            fade = QColor(dot)
            fade.setAlphaF(0.25 + 0.75 * math.sin(math.pi * t))
            qp.setBrush(fade)
            qp.drawEllipse(track.pointAtPercent(t), radius, radius)

    def _draw_node(self, qp: QPainter, node: _Node, w: float, p: dict, idle: bool) -> None:
        x = node.x * w - self._BAR_W / 2
        rect = QRectF(x, node.top, self._BAR_W, node.height)
        c = self._colors
        color = QColor(c["idle"] if idle else (c["loss"] if node.key in ("loss_src", "brake", "cu", "load_cu")
                                               else c["store"] if node.key == "kin" else c["node"]))
        qp.setPen(Qt.NoPen)
        qp.setBrush(color)
        qp.drawRoundedRect(rect, 3, 3)
        value = self._node_value(node.key, p)
        title = self._titles.get(node.key, node.title)
        if node.key == "bus" and not idle:
            title = f"{title} {self._vdc:.1f} V"
        font = QFont("Microsoft YaHei")
        font.setPixelSize(13 if not node.sink else 12)
        font.setBold(not node.sink)
        qp.setFont(font)
        text_w = 150.0
        if node.sink and node.key in ("load", "kin"):
            box = QRectF(x - text_w + self._BAR_W - 4, node.top - 36, text_w, 34)
            align = Qt.AlignRight | Qt.AlignBottom
        elif node.sink:
            box = QRectF(x - text_w / 2 + self._BAR_W / 2, node.top + node.height + 4, text_w, 36)
            align = Qt.AlignHCenter | Qt.AlignTop
        else:
            box = QRectF(x - text_w / 2 + self._BAR_W / 2, node.top - 38, text_w, 36)
            align = Qt.AlignHCenter | Qt.AlignBottom
        if box.left() < 2.0:                     # 最左侧节点的标签不要画出画布
            box.moveLeft(2.0)
            align = (align & ~Qt.AlignHCenter) | Qt.AlignLeft
        qp.setPen(c["text"] if not idle else c["muted"])
        text = title if idle else f"{title}\n{value:+.1f} W"
        if node.key == "kin" and not idle and "stored_j" in p:
            # 动能功率只有加减速时才有；转轴储能 ½Jω² 更能说明转子“存了多少能量”
            text += f" · 储能 {float(p['stored_j']):.3g} J"
        qp.drawText(box, align, text)

    @staticmethod
    def _node_value(key: str, p: dict) -> float:
        if key == "bus":
            inv = float(p.get("inv", 0.0))
            if inv < 0.0:   # 回馈：母线的能量来自逆变器
                return inv
            # 驱动：进入母线的功率 = 电源输出 − 电源内阻损耗
            return float(p.get("supply", 0.0)) - float(p.get("loss_src", 0.0))
        mapping = {"load": "fric", "kin": "kinetic"}
        return float(p.get(mapping.get(key, key), 0.0))

    def _draw_header(self, qp: QPainter, w: float, h: float, p: dict, idle: bool) -> None:
        qp.setPen(self._colors["muted"])
        if idle:
            qp.drawText(QRectF(0, h - 30, w, 26), Qt.AlignCenter,
                        "待机示意：勾选“启用功率流”并运行仿真或连接真机后显示实时能量流")
            return
        supply, inv, em = (float(p.get(k, 0.0)) for k in ("supply", "inv", "em"))
        parts = []
        if inv < -0.5:
            parts.append("回馈制动：能量由转轴经逆变器回到母线")
        else:
            if supply > 1.0 and inv > 0.0:
                parts.append(f"母线→逆变器 {100.0 * inv / supply:.0f}%")
            if inv > 1.0 and em > 0.0:
                parts.append(f"电机电磁效率 {100.0 * em / inv:.0f}%")
            if supply > 1.0 and em > 0.0:
                parts.append(f"总效率 {100.0 * em / supply:.0f}%")
        qp.drawText(QRectF(0, h - 30, w, 26), Qt.AlignCenter,
                    "   ·   ".join(parts) if parts else "轻载，效率不具参考意义")


class PowerSankey(QWidget, SankeyPainter):
    """功率流页上的桑基图控件：SankeyPainter 的绘制 + 粒子动画定时器。"""

    def __init__(self) -> None:
        QWidget.__init__(self)
        SankeyPainter.__init__(self)
        self.setMinimumHeight(260)
        self._anim = QTimer(self)
        self._anim.setInterval(40)
        self._anim.timeout.connect(self._tick)

    def update(self) -> None:  # noqa: D401 - QWidget.update
        QWidget.update(self)

    def set_active(self, active: bool) -> None:
        if active:
            self._anim.start()
        else:
            self._anim.stop()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        qp = QPainter(self)
        self.paint(qp, float(self.width()), float(self.height()))
