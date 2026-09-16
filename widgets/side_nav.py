"""侧边导航栏控件:基于 QListWidget,支持分组标题 + 图标项。

每个分组是一张圆角方框卡片:左侧标题,右上角 7x4 渐变点阵。
- 鼠标悬停在该分区(标题或组内任意条目)时,点阵出现"消散坑"扫过:
  坑心的点彻底消失,坑沿渐变过渡,坑外完全点亮;
- 切换页面时,当前分组的点阵触发一次"波纹消散 → 短暂空白 → 汇聚"。
"""
import math
import random
from typing import List, Optional, Tuple

from PySide6.QtCore import (
    Qt, Signal, QTimer, QSize, QEvent, QPoint, QRect,
    QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QPainter, QColor, QCursor, QPen, QLinearGradient,
)
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QWidget, QHBoxLayout, QLabel,
)


class _DotMatrix(QWidget):
    """7x4 密排渐变点阵(分区卡片右上,贴满上/右/下三边)。

    静态:自带对角消散渐变——右上(贴边侧)完整、中间渐隐、
    左下(靠标题侧)彻底消失。
    动态:idle / wave(悬停行波)/ settle(悬停结束回稳)/
    dissipate / hold / reconverge(切页消散循环)。60fps 刷新。
    """

    COLS, ROWS = 7, 4
    TICK_MS = 16
    # 消散循环
    DISSIPATE_MS = 520
    HOLD_MS = 90
    RECONVERGE_MS = 420
    SPREAD = 0.45       # 错峰幅度(占总时长的比例)
    DRIFT_PX = 3.5      # 消散时外漂距离
    SHRINK = 0.45       # 消散末端缩放比例
    # 行波(悬停):一个高斯"消散坑"沿对角扫过——坑心彻底消失,
    # 坑沿渐变过渡,坑外完全点亮,三区共存
    WAVE_SPEED = 0.0055  # 角速度 rad/ms
    WAVE_SIGMA = 0.55    # 消散坑宽度(rad,越小越局部)
    WAVE_PC = 0.55       # 列向相位步进
    WAVE_PR = 0.50       # 行向相位步进
    # 钢蓝 → 亮青渐变,不透明度调高
    _C0 = (94, 150, 246)
    _C1 = (110, 205, 250)
    BASE_A = 0.85        # 常态不透明度
    DOT_D = 5.0          # 点径 px
    DOT_W = 68           # 点阵区宽度(px,高度=卡片全高)
    BLEED = 0.35         # 上/右/下缘截断比例(点径的倍数)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        n = self.ROWS * self.COLS
        self._alpha = [1.0] * n
        self._scale = [1.0] * n
        self._dx = [0.0] * n
        self._dy = [0.0] * n
        # 静态消散渐变(与动画无关):右上(贴边侧)完整 → 对角渐隐 →
        # 左下(靠标题侧)彻底消失,加少量固定种子的抖动让边缘更自然
        rng = random.Random(7)
        self._grad = []
        tmax = (self.COLS - 1) + (self.ROWS - 1) * 0.4
        for r in range(self.ROWS):
            for c in range(self.COLS):
                t = ((self.COLS - 1 - c) + (self.ROWS - 1 - r) * 0.4) / tmax
                g = 1.25 - 1.6 * t                # t≥0.78 处彻底消失
                g += rng.uniform(-0.06, 0.06)
                self._grad.append(max(0.0, min(1.0, g)))
        # 每列一个渐变颜色
        self._colors = [self._lerp_color(c / (self.COLS - 1)) for c in range(self.COLS)]
        self._origin = (0.0, 0.0)     # 当前波源(消散用)
        self._max_dist = 1.0
        self._wave_on = False         # 悬停标志(跨相位记忆)
        # 尺寸由卡片 resizeEvent 指定:贴满卡片上/右/下三边
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  #  hover 由卡片/列表接管
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._phase = "idle"
        self._t = 0

    # ---------- 公共接口 ----------

    def setWave(self, on: bool) -> None:
        """悬停开始/结束。开始:进入行波;结束:平滑回稳。"""
        self._wave_on = on
        if on and self._phase == "idle":
            self._phase = "wave"
            self._t = 0
            self._timer.start(self.TICK_MS)
        elif not on and self._phase == "wave":
            self._phase = "settle"

    def startCycle(self) -> None:
        """触发一次:波纹消散 → 短暂空白 → 汇聚。"""
        self._pick_origin()
        self._phase = "dissipate"
        self._t = 0
        self._timer.start(self.TICK_MS)

    # ---------- 内部 ----------

    @classmethod
    def _lerp_color(cls, t: float) -> QColor:
        r = cls._C0[0] + (cls._C1[0] - cls._C0[0]) * t
        g = cls._C0[1] + (cls._C1[1] - cls._C0[1]) * t
        b = cls._C0[2] + (cls._C1[2] - cls._C0[2]) * t
        return QColor(int(r), int(g), int(b))

    @staticmethod
    def _ease_in_out_cubic(x: float) -> float:
        return 4 * x * x * x if x < 0.5 else 1.0 - (-2 * x + 2) ** 3 / 2

    def _pick_origin(self) -> None:
        """随机选一个角作为波源,并预计算各点距离/方向。"""
        self._origin = (
            random.choice((0.0, float(self.COLS - 1))),
            random.choice((0.0, float(self.ROWS - 1))),
        )
        self._max_dist = (self.COLS - 1) ** 2 + (self.ROWS - 1) ** 2

    def _stagger(self, c: int, r: int, progress: float) -> float:
        """按到波源的距离错峰:返回该点自己的局部进度 0..1。"""
        d2 = (c - self._origin[0]) ** 2 + (r - self._origin[1]) ** 2
        delay = (d2 / self._max_dist) * self.SPREAD
        # 最近的点在 progress=1-SPREAD 时完成,最远的正好在末尾完成,
        # 波纹在整个时长内均匀扫过
        local = (progress - delay) / (1.0 - self.SPREAD)
        return max(0.0, min(1.0, local))

    def _settle_target(self) -> bool:
        """settle 相位:所有点平滑回到 (alpha=1, scale=1)。返回是否完成。"""
        done = True
        for i in range(self.ROWS * self.COLS):
            self._alpha[i] += (1.0 - self._alpha[i]) * 0.16
            self._scale[i] += (1.0 - self._scale[i]) * 0.16
            self._dx[i] *= 0.8
            self._dy[i] *= 0.8
            if abs(1.0 - self._alpha[i]) > 0.02:
                done = False
        return done

    def _finish_to_idle(self) -> None:
        n = self.ROWS * self.COLS
        self._alpha = [1.0] * n
        self._scale = [1.0] * n
        self._dx = [0.0] * n
        self._dy = [0.0] * n
        if self._wave_on:
            self._phase = "wave"   # 鼠标还悬停着,回到行波
            self._t = 0
        else:
            self._phase = "idle"
            self._timer.stop()

    def _tick(self) -> None:
        self._t += self.TICK_MS
        if self._phase == "wave":
            two_pi = 2.0 * math.pi
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    i = r * self.COLS + c
                    p = (self._t * self.WAVE_SPEED
                         - (c * self.WAVE_PC + r * self.WAVE_PR)) % two_pi
                    d = abs(p - math.pi)          # 到空洞中心的角距离 0..π
                    a = 1.0 - math.exp(-((d / self.WAVE_SIGMA) ** 2))
                    self._alpha[i] = a
                    self._scale[i] = 0.55 + 0.45 * a
        elif self._phase == "settle":
            if self._settle_target():
                self._finish_to_idle()
        elif self._phase == "dissipate":
            progress = min(1.0, self._t / self.DISSIPATE_MS)
            ox, oy = self._origin
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    i = r * self.COLS + c
                    e = self._ease_in_out_cubic(self._stagger(c, r, progress))
                    self._alpha[i] = 1.0 - e
                    self._scale[i] = 1.0 - self.SHRINK * e
                    # 沿"远离波源"方向外漂;波源点自身不动
                    vx, vy = c - ox, r - oy
                    norm = math.hypot(vx, vy)
                    if norm > 1e-6:
                        self._dx[i] = vx / norm * self.DRIFT_PX * e
                        self._dy[i] = vy / norm * self.DRIFT_PX * e
            if progress >= 1.0:
                self._alpha = [0.0] * (self.ROWS * self.COLS)
                self._phase = "hold"
                self._t = 0
        elif self._phase == "hold":
            if self._t >= self.HOLD_MS:
                # 汇聚从消散波源的对角开始,形成往返感
                self._origin = (
                    float(self.COLS - 1) - self._origin[0],
                    float(self.ROWS - 1) - self._origin[1],
                )
                self._phase = "reconverge"
                self._t = 0
        elif self._phase == "reconverge":
            progress = min(1.0, self._t / self.RECONVERGE_MS)
            ox, oy = self._origin
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    i = r * self.COLS + c
                    e = self._ease_in_out_cubic(self._stagger(c, r, progress))
                    self._alpha[i] = e
                    self._scale[i] = (1.0 - self.SHRINK) + self.SHRINK * e
                    vx, vy = c - ox, r - oy
                    norm = math.hypot(vx, vy)
                    if norm > 1e-6:
                        self._dx[i] = vx / norm * self.DRIFT_PX * (1.0 - e)
                        self._dy[i] = vy / norm * self.DRIFT_PX * (1.0 - e)
                    else:
                        self._dx[i] = self._dy[i] = 0.0
            if progress >= 1.0:
                self._finish_to_idle()
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        dot = self.DOT_D
        bleed = dot * self.BLEED
        # 点阵贴满上/右/下三边:边缘列/行的点略微越界被裁断;
        # 仅左侧留空(面对标题一侧)
        x0 = dot * 0.6
        x1 = w - dot / 2 + bleed
        y0 = dot / 2 - bleed
        y1 = h - dot / 2 + bleed
        sx = (x1 - x0) / (self.COLS - 1)
        sy = (y1 - y0) / (self.ROWS - 1)
        for r in range(self.ROWS):
            for c in range(self.COLS):
                i = r * self.COLS + c
                a = self._alpha[i] * self._grad[i] * self.BASE_A
                if a <= 0.02:
                    continue
                d = dot * self._scale[i]
                cx = x0 + c * sx + self._dx[i]
                cy = y0 + r * sy + self._dy[i]
                core = QColor(self._colors[c])
                core.setAlphaF(a)
                p.setBrush(core)
                p.drawEllipse(int(cx - d / 2), int(cy - d / 2), int(d), int(d))
        p.end()


class _HeaderRow(QWidget):
    """分组标题卡片:圆角方框,左侧标题文字,右上角 7x4 点阵。

    悬停时方框底色/描边提亮,点阵进入行波律动。
    """

    def __init__(self, title: str, on_enter=None, parent=None) -> None:
        super().__init__(parent)
        self._hover = False
        self._on_enter = on_enter
        layout = QHBoxLayout(self)
        # 右侧预留点阵 overlay 的宽度,标题不会压到点阵
        layout.setContentsMargins(12, 4, 12 + _DotMatrix.DOT_W, 4)
        layout.setSpacing(8)
        self._label = QLabel(title)
        self._label.setObjectName("NavHeaderLabel")
        self._dot = _DotMatrix(self)   # overlay:不占布局,resizeEvent 里贴右上角
        layout.addWidget(self._label, 1)
        self.setMinimumHeight(28)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        # 点阵贴满卡片上/右/下三边(边缘点略微截断),仅左侧留空
        self._dot.setGeometry(self.width() - _DotMatrix.DOT_W, 0,
                              _DotMatrix.DOT_W, self.height())
        super().resizeEvent(ev)

    def dot(self) -> _DotMatrix:
        return self._dot

    def setHover(self, on: bool) -> None:
        if self._hover == on:
            return
        self._hover = on
        self._dot.setWave(on)
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        if self._on_enter is not None:
            self._on_enter()
        super().enterEvent(ev)

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 左右顶满:只留上下 1px 缝隙,避免与相邻元素粘连
        rect = self.rect().adjusted(0, 1, 0, -1)
        if self._hover:
            p.setBrush(QColor("#19212e"))
            p.setPen(QPen(QColor(120, 152, 186, 90), 1))
        else:
            p.setBrush(QColor("#151b27"))
            p.setPen(QPen(QColor("#232b38"), 1))
        p.drawRoundedRect(rect, 4, 4)
        p.end()
        super().paintEvent(ev)


class _Indicator(QWidget):
    """选中项左侧的滑动高亮指示条。

    叠加在 viewport 上,跟随选中条目上下滑动(与页面过渡同节奏);
    钢蓝→亮青竖向渐变,无发光,与点阵配色一致。
    """

    W = 3            # 条宽 px
    V_INSET = 7      # 上下内缩,两端圆角

    def __init__(self, viewport: QWidget) -> None:
        super().__init__(viewport)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def target_rect(self, item_rect: QRect) -> QRect:
        return QRect(
            item_rect.left() + 3,
            item_rect.top() + self.V_INSET,
            self.W,
            max(6, item_rect.height() - 2 * self.V_INSET),
        )

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt signature
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(*_DotMatrix._C0))
        g.setColorAt(1.0, QColor(*_DotMatrix._C1))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(self.rect(), self.W / 2, self.W / 2)
        p.end()


class SideNav(QListWidget):
    """分组导航。sections 结构:[(组标题, [(条目文本, 页面索引), ...]), ...]

    组标题不可选中;条目通过 UserRole 携带页面索引,
    因此导航顺序可以与 QStackedWidget 的页面顺序解耦。
    """

    currentIndexChanged = Signal(int)

    def __init__(self, sections: List[Tuple[str, List[Tuple[str, int]]]]) -> None:
        super().__init__()
        self.setObjectName("SideNav")
        self.setMinimumWidth(140)
        self.setMaximumWidth(180)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._headers: List[_HeaderRow] = []
        self._header_rows: List[int] = []
        self._row_to_header: dict[int, int] = {}
        self._first_select = True  # 启动时不消散,避免看到残缺点阵
        self._hover_sec: Optional[int] = None

        cur_row = 0
        for sec_idx, (title, entries) in enumerate(sections):
            header_widget = _HeaderRow(
                title, on_enter=lambda i=sec_idx: self._set_hover(i))
            header_item = QListWidgetItem()
            header_item.setFlags(Qt.NoItemFlags)          # 不可选中
            header_item.setData(Qt.UserRole, -1)
            # 方框卡片比原纯文本 header 高;4 个 header 总计 +32px,
            # 远小于侧边栏空白(800 - 内容高度),不撑高总高度
            header_item.setSizeHint(QSize(180, 34))
            self.addItem(header_item)
            self.setItemWidget(header_item, header_widget)
            self._headers.append(header_widget)
            self._header_rows.append(cur_row)
            self._row_to_header[cur_row] = sec_idx
            cur_row += 1
            for text, page_idx in entries:
                item = QListWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                item.setData(Qt.UserRole, page_idx)
                self.addItem(item)
                self._row_to_header[cur_row] = sec_idx
                cur_row += 1

        self.currentRowChanged.connect(self._on_row_changed)
        # 悬停追踪:条目区走 viewport 事件,标题卡片走自身 enterEvent
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)
        # 选中指示条(viewport 叠加层,跟随选中项滑动)
        self._indicator = _Indicator(self.viewport())
        self._ind_anim = QPropertyAnimation(self._indicator, b"geometry", self)
        self._ind_anim.setDuration(190)                  # 与页面过渡同节奏
        self._ind_anim.setEasingCurve(QEasingCurve.OutCubic)
        # QListView 完成首轮布局时会调用 updateGeometries；不额外排队绑定
        # 回调，避免窗口快速销毁后访问已经释放的 C++ 控件。

    # ---------- 卡片几何:强制占满整行 ----------

    def updateGeometries(self) -> None:  # noqa: N802 - Qt signature
        super().updateGeometries()
        # QListView 默认把 item widget 摆进"文本子矩形"(左右各让 ~25px),
        # 导致卡片无法顶满;每次布局后强制卡片覆盖整条 item rect
        if not hasattr(self, "_header_rows"):
            return
        for row, header in zip(self._header_rows, self._headers):
            rect = self.visualRect(self.model().index(row, 0))
            if rect.isValid() and header.geometry() != rect:
                header.setGeometry(rect)
        # 布局变化(窗口缩放/滚动)时,指示条无动画地贴回当前选中项
        if (hasattr(self, "_indicator") and
                self._ind_anim.state() != QPropertyAnimation.Running):
            rect = self.visualRect(self.model().index(self.currentRow(), 0))
            if rect.isValid():
                target = self._indicator.target_rect(rect)
                if self._indicator.geometry() != target:
                    self._indicator.setGeometry(target)
                    self._indicator.show()

    # ---------- 选中指示条 ----------

    def _move_indicator(self, row: int, animate: bool) -> None:
        rect = self.visualRect(self.model().index(row, 0))
        if not rect.isValid():
            # 布局尚未完成(如启动初期),推迟到下一轮事件循环
            QTimer.singleShot(0, lambda: self._move_indicator(row, animate))
            return
        target = self._indicator.target_rect(rect)
        self._ind_anim.stop()
        if animate and self._indicator.isVisible():
            self._ind_anim.setStartValue(self._indicator.geometry())
            self._ind_anim.setEndValue(target)
            self._ind_anim.start()
        else:
            self._indicator.setGeometry(target)
            self._indicator.show()
        self._indicator.raise_()

    def stop_animations(self) -> None:
        """窗口关闭前停止所有导航动画和刷新定时器。"""
        self._ind_anim.stop()
        for header in self._headers:
            dot = header.dot()
            dot._timer.stop()
            dot._phase = "idle"

    # ---------- 悬停 → 行波 ----------

    def _set_hover(self, sec_idx: Optional[int]) -> None:
        if sec_idx == self._hover_sec:
            return
        self._hover_sec = sec_idx
        for i, header in enumerate(self._headers):
            header.setHover(i == sec_idx)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt signature
        if obj is self.viewport():
            t = ev.type()
            if t == QEvent.MouseMove:
                item = self.itemAt(ev.pos())
                if item is not None:
                    # item 为空(条目间隙)时保持原分区,避免缝隙闪烁
                    self._set_hover(self._row_to_header.get(self.row(item)))
            elif t == QEvent.Leave:
                # 移入标题卡片时 viewport 也会收到 Leave,
                # 仅当鼠标真的离开列表区域才清除悬停
                pos = self.viewport().mapFromGlobal(QCursor.pos())
                if not self.viewport().rect().contains(pos):
                    self._set_hover(None)
        return super().eventFilter(obj, ev)

    # ---------- 选中 → 切页 + 消散 ----------

    def _on_row_changed(self, row: int) -> None:
        item = self.item(row)
        if item is None:
            return
        page_idx = item.data(Qt.UserRole)
        if page_idx is None or page_idx < 0:
            return
        self.currentIndexChanged.emit(page_idx)
        # 首次 select_page(0) 不触发动画,避免启动时点阵"残缺"
        if self._first_select:
            self._first_select = False
            self._move_indicator(row, animate=False)
            return
        # 指示条滑向新选中项(与页面过渡同节奏)
        self._move_indicator(row, animate=True)
        # 触发当前分组 header 的右上角点阵消散
        sec_idx = self._row_to_header.get(row)
        if sec_idx is not None and 0 <= sec_idx < len(self._headers):
            self._headers[sec_idx].dot().startCycle()

    def select_page(self, page_idx: int) -> None:
        """按页面索引选中对应条目(跳过组标题)。"""
        for row in range(self.count()):
            if self.item(row).data(Qt.UserRole) == page_idx:
                self.setCurrentRow(row)
                return
