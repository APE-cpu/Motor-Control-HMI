"""位置传感器详情对话框：工作原理详解 + 在线自检。

自检基于遥测流做 3 秒采样，检查数据流、传感器匹配、信号质量、
角度原始量活动性、（无传感器法）观测器收敛度。
"""
import statistics

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QLabel, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout,
)

from logs.operation_logger import logger

_CHECK_SECONDS = 3.0

# 各传感器原理/参数/故障说明（HTML）
PRINCIPLES = {
    "霍尔传感器(Hall)": """
<h3>霍尔传感器（Hall）</h3>
<p><b>原理：</b>三个霍尔开关沿定子相隔 120° 电角度安装，转子磁极经过时输出
高/低电平，三路信号组合出 6 个状态，每 60° 电角度跳变一次。控制器据此判断
转子处于哪个扇区，进而决定换相时刻。</p>
<p><b>分辨率：</b>60° 电角度（每电周期 6 个离散位置）。扇区之间的角度靠转速
积分插值，稳速时插值准确，加减速时有滞后。</p>
<p><b>优点：</b>结构简单、成本低、抗油污振动；<b>局限：</b>分辨率低，
不适合高性能位置控制（FOC 低速时电流波动大）。</p>
<p><b>关键参数：</b>安装偏移角（霍尔零点与 A 相轴线夹角，装配误差需标定补偿）、
输出极性。</p>
<p><b>典型故障：</b>某一路恒高/恒低（接线断或器件坏）→ 状态序列缺码，
表现为特定扇区换相异常、转矩周期性抖动。</p>
""",
    "增量式编码器(QEP)": """
<h3>增量式编码器（QEP）</h3>
<p><b>原理：</b>码盘随轴旋转，光电/磁编码输出 A、B 两路相位差 90° 的正交
脉冲和每转一个的 Z 索引脉冲。计数器对 AB 边沿 4 倍频计数得到相对角度，
AB 相序判方向，Z 脉冲用于每圈校准绝对零位。</p>
<p><b>分辨率：</b>线数 × 4（如 2500 线 → 每转 10000 计数，0.036°/计数）。</p>
<p><b>优点：</b>分辨率高、响应快，是伺服的主流选择；<b>局限：</b>上电后
只有相对位置，需先找 Z 脉冲或做磁极对齐（预定位/HFI）才能做 FOC；
计数丢失后误差累积。</p>
<p><b>关键参数：</b>线数（PPR）、Z 脉冲偏移角、计数方向。</p>
<p><b>典型故障：</b>A/B 断线（只能单向计数或不计数）、信号受干扰丢计数
（角度漂移，Z 校准时跳变）、码盘污染（局部丢脉冲）。</p>
""",
    "旋转变压器(Resolver)": """
<h3>旋转变压器（Resolver）</h3>
<p><b>原理：</b>转子上的励磁绕组注入高频正弦励磁（典型 10 kHz），定子两组
相互垂直的输出绕组感应出被 sin θ / cos θ 调制的信号，专用解码芯片（RDC）
解调并反正切求出绝对角度。</p>
<p><b>分辨率：</b>取决于 RDC 位数（常见 12~16 bit）。上电即为绝对位置
（单圈）。</p>
<p><b>优点：</b>纯电磁结构，耐高温、强振动、油污，可靠性最高，车规电驱
首选；<b>局限：</b>成本高，需要励磁与解调电路，动态跟踪有带宽限制。</p>
<p><b>关键参数：</b>极对数（旋变极对数 ≠ 电机极对数时要换算）、励磁频率、
零位偏移角。</p>
<p><b>典型故障：</b>励磁断线（输出全无）、某相输出断线（角度锁死或跳变）、
零位漂移（换相角错，表现为同转速下电流偏大、效率下降）。</p>
""",
    "无位置传感器-滑模观测器(SMO)": """
<h3>滑模观测器（SMO）</h3>
<p><b>原理：</b>基于电机电压方程构造电流观测器，用滑模切换项迫使观测电流
跟踪实测电流；切换项的等效值就是反电动势估计，反电动势的相位即转子位置
（EMF ∝ ω·ψf，方向垂直于磁链）。</p>
<p><b>特点：</b>结构简单、鲁棒性强（对参数失配不敏感），但切换带来高频
抖振，需要低通滤波，滤波又引入相位滞后（需按转速补偿）。</p>
<p><b>适用区间：</b>中高速。反电动势与转速成正比，<b>低速/零速时信噪比
不足，位置估计失效</b>——监控页的「低速警告」即提示进入不可用区。</p>
<p><b>关键参数：</b>滑模增益（大→收敛快但抖振大）、截止频率、启动切换
转速（低速用开环强拖，超过阈值切入 SMO）。</p>
""",
    "无位置传感器-扩展卡尔曼(EKF)": """
<h3>扩展卡尔曼滤波（EKF）</h3>
<p><b>原理：</b>把转子位置/转速作为状态量，将电机非线性模型在工作点线性化，
用卡尔曼滤波在「模型预测」与「电流量测」之间按噪声协方差最优加权，
递推估计位置与转速。</p>
<p><b>特点：</b>对量测噪声有最优滤波效果，估计平滑，还能同时估计负载转矩
等扩展状态；代价是矩阵运算量大（DSP 上需要优化），且 Q/R 协方差整定
依赖经验。</p>
<p><b>适用区间：</b>中高速；极低速时模型可观测性变差，同样会失效。</p>
<p><b>关键参数：</b>过程噪声 Q（信模型还是信量测）、量测噪声 R、初始协方差。
Q/R 比值决定动态响应与平滑度的折中。</p>
""",
    "无位置传感器-模型参考自适应(MRAS)": """
<h3>模型参考自适应（MRAS）</h3>
<p><b>原理：</b>用不含转速的「参考模型」（如定子电压方程算磁链）和含转速的
「可调模型」（电流方程算磁链）并行计算同一物理量，两者输出之差经 PI 自适应
律调节可调模型中的转速估计，误差收敛时转速估计即真实转速，积分得角度。</p>
<p><b>特点：</b>结构清晰、计算量适中、转速估计平滑；对电机参数（尤其 Rs、
ψf 随温度漂移）较敏感。</p>
<p><b>适用区间：</b>中高速；低速时参考模型中纯积分/低通近似误差放大而失效。</p>
<p><b>关键参数：</b>自适应 PI 增益（决定收敛速度与稳定裕度）、磁链计算的
积分器截止频率。</p>
""",
    "无位置传感器-高频注入(HFI)": """
<h3>高频注入（HFI）</h3>
<p><b>原理：</b>在估计的 d 轴上叠加高频电压小信号（典型 0.5~2 kHz），利用
转子<b>凸极性</b>（Ld ≠ Lq）：若估计角有偏差，高频电流会在 q 轴出现与偏差角
成正比的分量，解调该分量并用锁相环把误差压到零，即可跟踪真实转子位置。</p>
<p><b>特点：</b>不依赖反电动势，<b>唯一能在零速/极低速可靠工作</b>的无传感器
方法，也可用于静止状态磁极初始定位；代价是注入信号带来附加损耗与可听噪声，
且要求电机有足够凸极率（表贴式 PMSM 的 Ld≈Lq 时效果差）。</p>
<p><b>适用区间：</b>零速~低速；中高速通常切换到 SMO/EKF 等反电动势法
（混合方案）。</p>
<p><b>关键参数：</b>注入频率与幅值（高→信噪比好但噪声损耗大）、解调滤波器
带宽、锁相环增益。</p>
""",
}


class SensorDetailDialog(QDialog):
    def __init__(self, sensor_name: str, comm, parent=None) -> None:
        super().__init__(parent)
        self._sensor_name = sensor_name
        self._comm = comm
        self._frames: list = []
        self._checking = False

        self.setWindowTitle(f"传感器详情 — {sensor_name}")
        self.resize(720, 640)
        root = QVBoxLayout(self)

        browser = QTextBrowser()
        browser.setHtml(PRINCIPLES.get(sensor_name, f"<p>{sensor_name}</p>"))
        root.addWidget(browser, 1)

        check_box = QGroupBox("在线自检")
        cv = QVBoxLayout(check_box)
        hint = QLabel("检查数据流、传感器匹配、信号质量与角度活动性。"
                      "建议在电机运行（仿真或真机）状态下执行，约 3 秒。")
        hint.setWordWrap(True)
        cv.addWidget(hint)
        self._btn_check = QPushButton("开始自检")
        self._btn_check.setObjectName("PrimaryButton")
        self._btn_check.clicked.connect(self._on_check)
        cv.addWidget(self._btn_check)
        self._result = QPlainTextEdit()
        self._result.setReadOnly(True)
        self._result.setMinimumHeight(150)
        self._result.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        cv.addWidget(self._result)
        root.addWidget(check_box)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        root.addWidget(btns)

    # ─── 自检 ───────────────────────────────────────────────
    def _on_check(self) -> None:
        if self._checking:
            return
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            self._result.setPlainText("❌ 无数据源：请先启动仿真（虚拟电机）或连接真机。")
            return
        self._checking = True
        self._frames = []
        self._btn_check.setEnabled(False)
        self._btn_check.setText("采样中…")
        self._result.setPlainText(f"正在采样 {_CHECK_SECONDS:.0f} 秒遥测数据…")
        self._comm.telemetryReceived.connect(self._collect)
        QTimer.singleShot(int(_CHECK_SECONDS * 1000), self._finish_check)

    def _collect(self, frame) -> None:
        self._frames.append(frame)

    def _finish_check(self) -> None:
        try:
            self._comm.telemetryReceived.disconnect(self._collect)
        except RuntimeError:
            pass
        self._checking = False
        self._btn_check.setEnabled(True)
        self._btn_check.setText("开始自检")
        self._result.setPlainText("\n".join(self._evaluate()))
        logger.log("传感器自检", f"{self._sensor_name}  帧数={len(self._frames)}")

    def _evaluate(self) -> list:
        lines = [f"════ 自检报告：{self._sensor_name} ════", ""]
        n = len(self._frames)

        # 1) 数据流
        expect = int(_CHECK_SECONDS / 0.1)
        if n == 0:
            lines.append("❌ 数据流：未收到任何遥测帧，检查通信连接/下位机状态")
            return lines
        if n >= expect * 0.7:
            lines.append(f"✅ 数据流：{_CHECK_SECONDS:.0f} 秒收到 {n} 帧（正常）")
        else:
            lines.append(f"⚠️ 数据流：{_CHECK_SECONDS:.0f} 秒仅收到 {n} 帧"
                         f"（期望约 {expect}），可能丢帧或采样周期异常")

        # 2) 激活传感器匹配
        src = self._frames[-1].sensor_source or ""
        short = self._sensor_name.split("(")[-1].rstrip(")")
        if self._sensor_name in src or (short and short in src):
            lines.append(f"✅ 激活传感器：{src}（与本页一致）")
        else:
            lines.append(f"⚠️ 激活传感器：当前数据来自「{src or '未知'}」，"
                         f"非本传感器——请在控制页选中并「保存/应用参数」后重测")

        # 3) 信号质量
        q = statistics.fmean(f.sensor_quality for f in self._frames)
        if q >= 0.8:
            lines.append(f"✅ 信号质量：{q:.2f}（良好）")
        elif q >= 0.5:
            lines.append(f"⚠️ 信号质量：{q:.2f}（偏低，检查屏蔽/接线/安装间隙）")
        else:
            lines.append(f"❌ 信号质量：{q:.2f}（异常）")

        # 4) 角度活动性（需要电机在转）
        speed = statistics.fmean(abs(f.speed_actual) for f in self._frames)
        raws = [f.angle_raw for f in self._frames]
        if speed < 10.0:
            lines.append("➖ 角度活动性：电机接近静止，无法判断（请启动电机后重测）")
        elif len(set(round(r, 6) for r in raws)) > max(3, n // 10):
            lines.append(f"✅ 角度活动性：原始量随转动更新（均值转速 {speed:.0f} rpm）")
        else:
            lines.append(f"❌ 角度活动性：转速 {speed:.0f} rpm 但角度原始量几乎不变，"
                         "检查传感器接线/供电/码盘")

        # 5) 无传感器法专项：观测器收敛度与低速区
        if self._sensor_name.startswith("无位置传感器"):
            conv = statistics.fmean(f.convergence for f in self._frames)
            if conv >= 0.9:
                lines.append(f"✅ 观测器收敛度：{conv:.2f}（已收敛）")
            elif conv >= 0.6:
                lines.append(f"⚠️ 观测器收敛度：{conv:.2f}（收敛中/欠佳，检查参数整定）")
            else:
                lines.append(f"❌ 观测器收敛度：{conv:.2f}（未收敛，位置估计不可信）")
            warn_ratio = sum(1 for f in self._frames if f.low_speed_warn) / n
            if warn_ratio > 0.3:
                lines.append(f"⚠️ 低速区警告：{warn_ratio*100:.0f}% 时间处于该方法"
                             "不可用转速区（HFI 除外，反电动势法低速失效是原理性限制）")
            else:
                lines.append("✅ 低速区：工作转速在方法适用范围内")

        lines += ["", f"结论：{'未见异常 ✅' if all(not l.startswith(('❌','⚠')) for l in lines[2:]) else '存在待确认项，见上 ⚠/❌'}"]
        return lines
