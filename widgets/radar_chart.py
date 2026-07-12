"""多维雷达图控件（QPainter 自绘，零额外依赖）。

各物理量量级差异大（转速数千、电流数安、温度数十），按各自量程
归一化到 0~1 再绘制，才能在同一张图上比较——就像游戏里的能力六维图。
同时叠加“当前值”和“平均值”两层多边形。
"""
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


class RadarChart(QWidget):
    def __init__(self, axes: list, parent=None) -> None:
        """axes: [(名称, 满量程), ...]，满量程用于把原始值归一化到 0~1。"""
        super().__init__(parent)
        self._axes = axes
        self._cur = [0.0] * len(axes)     # 当前值（原始）
        self._avg = [0.0] * len(axes)     # 平均值（原始）
        self.setMinimumSize(280, 260)

    def set_values(self, current: list, average: list) -> None:
        self._cur = list(current)
        self._avg = list(average)
        self.update()

    def _norm(self, vals: list) -> list:
        out = []
        for v, (_name, full) in zip(vals, self._axes):
            out.append(max(0.0, min(1.0, abs(v) / full if full else 0.0)))
        return out

    def paintEvent(self, _ev) -> None:
        n = len(self._axes)
        if n < 3:
            return
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0 + 6
        r = min(w, h) / 2.0 - 42        # 留出标签空间

        # 轴角度：从正上方开始顺时针均分
        angs = [(-math.pi / 2.0) + 2.0 * math.pi * i / n for i in range(n)]

        # —— 背景网格：4 圈同心多边形 ——
        qp.setPen(QPen(QColor("#2c3442"), 1))
        for ring in range(1, 5):
            rr = r * ring / 4.0
            poly = QPolygonF([QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a))
                              for a in angs])
            qp.drawPolygon(poly)
        # 辐条 + 轴标签
        qp.setFont(QFont("", 8))
        for i, a in enumerate(angs):
            ex, ey = cx + r * math.cos(a), cy + r * math.sin(a)
            qp.setPen(QPen(QColor("#2c3442"), 1))
            qp.drawLine(QPointF(cx, cy), QPointF(ex, ey))
            # 标签：名称 + 当前值
            name, full = self._axes[i]
            lx, ly = cx + (r + 22) * math.cos(a), cy + (r + 22) * math.sin(a)
            qp.setPen(QPen(QColor("#b8c6d8")))
            flags = Qt.AlignCenter
            qp.drawText(int(lx - 40), int(ly - 14), 80, 14, flags, name)
            qp.setPen(QPen(QColor("#8fa3b8")))
            qp.drawText(int(lx - 40), int(ly), 80, 14, flags,
                        f"{self._cur[i]:.1f}")

        # —— 平均值多边形（浅色填充）——
        self._draw_poly(qp, cx, cy, r, angs, self._norm(self._avg),
                        QColor(120, 144, 156, 60), QColor("#90a4ae"))
        # —— 当前值多边形（蓝色实线）——
        self._draw_poly(qp, cx, cy, r, angs, self._norm(self._cur),
                        QColor(79, 195, 247, 90), QColor("#4fc3f7"))

        # 图例
        qp.setFont(QFont("", 8))
        qp.setPen(QPen(QColor("#4fc3f7")))
        qp.drawText(6, 12, "■ 当前值")
        qp.setPen(QPen(QColor("#90a4ae")))
        qp.drawText(70, 12, "■ 平均值")

    @staticmethod
    def _draw_poly(qp, cx, cy, r, angs, norm_vals, fill, line):
        poly = QPolygonF([
            QPointF(cx + r * v * math.cos(a), cy + r * v * math.sin(a))
            for v, a in zip(norm_vals, angs)])
        qp.setBrush(fill)
        qp.setPen(QPen(line, 2))
        qp.drawPolygon(poly)
