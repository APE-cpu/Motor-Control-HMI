"""参数辨识算法说明对话框（参数辨识页帮助）。"""
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout,
)

_HTML = """
<h2>参数辨识算法原理</h2>
<p>目标：只用<b>转速与电流遥测</b>，辨识电机的三个机械参数——
粘滞摩擦系数 <b>B</b>、库仑摩擦力矩 <b>Tc</b>、转动惯量 <b>J</b>。
不需要额外的转矩传感器，以磁链 ψf 作为转矩锚点。</p>

<h3>转矩锚点：Kt = 1.5·p·ψf</h3>
<p>PMSM 的电磁转矩 T = Kt·iq，其中转矩常数 Kt 由极对数 p 和永磁磁链 ψf 算出。
ψf 来自铭牌或反电动势实验，是整个辨识的已知基准——所以电流 iq 能换算成转矩，
无需测功机。</p>

<h3>第 1 步：两点稳态解 B 和 Tc</h3>
<p>电机稳定运行（转速恒定、加速度为零）时，电磁转矩正好平衡摩擦转矩：</p>
<p style="font-family: Consolas, monospace; background-color:#10131a; padding:8px;">
Kt · iq = B · ω + Tc</p>
<p>这是一个关于 (B, Tc) 的一次方程。在<b>两个不同转速点</b>各测一次稳态电流，
得到两个方程，联立解出两个未知数：</p>
<ul>
<li><b>B</b> = Kt · (i₂ − i₁) / (ω₂ − ω₁)　——摩擦转矩随转速变化的斜率</li>
<li><b>Tc</b> = Kt · i₁ − B · ω₁　——转速外推到零的截距（干摩擦）</li>
</ul>
<p>（ω 为机械角速度 rad/s，由 rpm 换算：ω = rpm·π/30）。两个转速点拉得越开，
斜率估计越稳——所以配置里点 2 默认比点 1 高不少。</p>

<h3>第 2 步：滑行实验最小二乘拟合 J</h3>
<p>切断驱动让电机<b>自由滑行降速</b>，此时没有电磁转矩，只有摩擦在减速：</p>
<p style="font-family: Consolas, monospace; background-color:#10131a; padding:8px;">
J · (dω/dt) = −(B · ω + Tc)</p>
<p>B、Tc 已在第 1 步求出，滑行曲线上每个采样点都能算出左边的加速度 dω/dt
和右边的摩擦转矩。J 是唯一未知数，用<b>最小二乘</b>拟合所有采样点求最优解：</p>
<p style="font-family: Consolas, monospace; background-color:#10131a; padding:8px;">
J = Σ(T·dω/dt) / Σ(dω/dt)²</p>
<p>只取<b>明显降速段</b>（dω/dt &lt; −1 rad/s²）且<b>未停死</b>（ω &gt; 5 rad/s）的采样点——
接近停止时库仑摩擦的过零抖动会污染数据，要剔除。</p>

<h3>一句话总结</h3>
<p style="font-family: Consolas, monospace; background-color:#10131a; padding:8px;">
两点稳态电流 →解出→ B、Tc　→　滑行降速曲线 →最小二乘→ J</p>

<h3>为什么用这套方法</h3>
<ul>
<li><b>无需测功机</b>：靠 ψf 把电流换算成转矩，只用常规遥测量</li>
<li><b>分步解耦</b>：先稳态定摩擦、再滑行定惯量，每步只解少量未知数，数值稳健</li>
<li><b>可验证</b>：辨识出的 B/Tc/J 可「应用到数字孪生」，再对比孪生真值，
直观看到辨识精度</li>
</ul>

<h3>实验注意</h3>
<ul>
<li>两个稳态点都要真正<b>稳定</b>（转速不再变化）后再取电流，否则加速度未清零会污染方程</li>
<li>滑行段要有足够的降速采样点（默认至少 3 点），转速点 2 取高些能拉长滑行曲线</li>
<li>ψf / 极对数填错会等比例地缩放 Kt，进而系统性地偏移 B/Tc/J——务必用准确的铭牌值</li>
</ul>
"""


class IdentifyHelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("参数辨识 — 算法原理")
        self.resize(680, 640)
        v = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(_HTML)
        v.addWidget(browser, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)
