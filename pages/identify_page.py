"""参数辨识页面：两点稳态 + 滑行实验，辨识 B / Tc / J。

原理（只用转速+电流遥测，以 ψf 为转矩锚点）：
  稳态：Kt·iq = B·ω + Tc，两个转速点解出 B、Tc（Kt = 1.5·p·ψf）
  滑行：J·dω/dt = −(B·ω + Tc)，最小二乘拟合 J
注意：仅凭转速/电流数据转矩尺度不可观测，ψf 必须由铭牌或
反电动势实验提供；仿真模式下可用虚拟电机真值验证辨识精度。
"""
import math
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from config.config import CMD_START, CMD_STOP
from logs.operation_logger import logger


class IdentifyPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm
        self._phase = None            # None / steady1 / steady2 / coast
        self._records: list = []      # 当前阶段的 (t, speed_rpm, iq)
        self._steady1 = None          # (omega, iq)
        self._steady2 = None
        self._result = None           # dict(B=, Tc=, J=)

        root = QVBoxLayout(self)
        title = QLabel("电机参数辨识")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        # ---- 实验配置 ----
        cfg_box = QGroupBox("实验配置")
        f = QFormLayout(cfg_box)
        sim_p = comm.motor_sim_params()
        self._psi_f = QDoubleSpinBox()
        self._psi_f.setDecimals(4); self._psi_f.setRange(0.001, 10.0)
        self._psi_f.setValue(sim_p.psi_f)
        self._pole_pairs = QSpinBox(); self._pole_pairs.setRange(1, 50)
        self._pole_pairs.setValue(sim_p.pole_pairs)
        self._n1 = QSpinBox(); self._n1.setRange(100, 20000); self._n1.setValue(1500)
        self._n2 = QSpinBox(); self._n2.setRange(100, 20000); self._n2.setValue(2800)
        f.addRow("磁链 ψf (Wb，铭牌/反电动势实验)", self._psi_f)
        f.addRow("极对数 p", self._pole_pairs)
        f.addRow("稳态转速点 1 (rpm)", self._n1)
        f.addRow("稳态转速点 2 (rpm)", self._n2)
        root.addWidget(cfg_box)

        # ---- 控制与状态 ----
        h = QHBoxLayout()
        self._btn_run = QPushButton("开始辨识实验")
        self._btn_run.setObjectName("PrimaryButton")
        self._btn_run.clicked.connect(self._on_run)
        self._btn_apply = QPushButton("应用到数字孪生")
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._on_apply)
        self._status = QLabel("就绪（请先启动仿真或连接真机）")
        h.addWidget(self._btn_run)
        h.addWidget(self._btn_apply)
        h.addWidget(self._status, 1)
        root.addLayout(h)

        # ---- 结果 ----
        res_box = QGroupBox("辨识结果")
        rv = QVBoxLayout(res_box)
        self._report = QPlainTextEdit()
        self._report.setReadOnly(True)
        self._report.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self._report.setPlainText(
            "实验流程：\n"
            "  1) 升速至转速点 1，等待稳态，采集 ω/iq 均值\n"
            "  2) 升速至转速点 2，等待稳态，采集 ω/iq 均值\n"
            "  3) 封管滑行，记录降速曲线\n"
            "  4) 稳态两点解出 B、Tc；滑行曲线最小二乘拟合 J\n")
        rv.addWidget(self._report)
        root.addWidget(res_box, 1)

        comm.telemetryReceived.connect(self._on_telemetry)

    # ---------- 实验流程 ----------
    def _on_run(self) -> None:
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            QMessageBox.warning(self, "无法开始", "请先启动仿真（虚拟电机）或连接真机。")
            return
        if self._phase is not None:
            return
        self._btn_run.setEnabled(False)
        self._btn_apply.setEnabled(False)
        self._steady1 = self._steady2 = self._result = None
        logger.log("参数辨识", f"开始实验 n1={self._n1.value()} n2={self._n2.value()}")

        self._enter_phase("steady1")
        self._send_start(float(self._n1.value()))
        # 5 s 后取稳态均值，进入下一阶段
        QTimer.singleShot(5000, self._finish_steady1)

    def _finish_steady1(self) -> None:
        self._steady1 = self._steady_average()
        self._enter_phase("steady2")
        self._send_start(float(self._n2.value()))
        QTimer.singleShot(5000, self._finish_steady2)

    def _finish_steady2(self) -> None:
        self._steady2 = self._steady_average()
        self._enter_phase("coast")
        self._comm.send_frame(encode_frame(CMD_STOP))
        QTimer.singleShot(6000, self._finish_coast)

    def _finish_coast(self) -> None:
        coast = list(self._records)
        self._phase = None
        self._btn_run.setEnabled(True)
        try:
            self._compute(coast)
        except Exception as e:
            self._status.setText(f"辨识失败：{e}")
            self._report.appendPlainText(f"\n[错误] {e}")

    def _enter_phase(self, phase: str) -> None:
        self._phase = phase
        self._records = []
        labels = {"steady1": "阶段 1/3：稳态点 1 采集中…",
                  "steady2": "阶段 2/3：稳态点 2 采集中…",
                  "coast": "阶段 3/3：滑行降速记录中…"}
        self._status.setText(labels[phase])

    def _send_start(self, target_rpm: float) -> None:
        payload = f"target={target_rpm}".encode("utf-8")
        self._comm.send_frame(encode_frame(CMD_START, payload))

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        if self._phase is not None:
            self._records.append(
                (time.time(), frame.speed_actual, frame.current_actual))

    def _steady_average(self, last_n: int = 15) -> tuple:
        """取阶段末尾 last_n 帧的均值（跳过升速瞬态）。"""
        pts = self._records[-last_n:]
        if len(pts) < 5:
            raise RuntimeError("采集帧数不足，检查数据流是否正常")
        omega = sum(p[1] for p in pts) / len(pts) * math.pi / 30.0
        iq = sum(p[2] for p in pts) / len(pts)
        return omega, iq

    # ---------- 参数求解 ----------
    def _compute(self, coast: list) -> None:
        psi_f = float(self._psi_f.value())
        p = int(self._pole_pairs.value())
        kt = 1.5 * p * psi_f

        (w1, i1), (w2, i2) = self._steady1, self._steady2
        if abs(w2 - w1) < 1.0:
            raise RuntimeError("两个稳态转速点太接近")
        # Kt·iq = B·ω + Tc
        b_hat = kt * (i2 - i1) / (w2 - w1)
        tc_hat = kt * i1 - b_hat * w1

        # 滑行：J·dω/dt = −(B·ω + Tc)，取降速段做最小二乘
        ws = [(t, rpm * math.pi / 30.0) for t, rpm, _ in coast]
        num = den = 0.0
        used = 0
        for k in range(1, len(ws)):
            dt = ws[k][0] - ws[k - 1][0]
            if dt <= 0:
                continue
            dwdt = (ws[k][1] - ws[k - 1][1]) / dt
            w_mid = 0.5 * (ws[k][1] + ws[k - 1][1])
            if dwdt > -1.0 or w_mid < 5.0:   # 只用明显降速且未停死的区段
                continue
            torque = -(b_hat * w_mid + tc_hat)
            num += torque * dwdt
            den += dwdt * dwdt
            used += 1
        if used < 3:
            raise RuntimeError(f"滑行段有效数据太少（{used} 点），试试提高转速点 2")
        j_hat = num / den

        self._result = {"B": b_hat, "Tc": tc_hat, "J": j_hat}
        self._btn_apply.setEnabled(True)
        self._status.setText("辨识完成")
        logger.log("参数辨识", f"B={b_hat:.3e} Tc={tc_hat:.3e} J={j_hat:.3e}")

        # 报告（仿真模式下附孪生真值对比）
        lines = [
            "════ 辨识结果 ════",
            f"稳态点1: ω={w1:7.1f} rad/s  iq={i1:.2f} A",
            f"稳态点2: ω={w2:7.1f} rad/s  iq={i2:.2f} A",
            f"滑行段有效点数: {used}",
            "",
            f"  B  (粘滞摩擦) = {b_hat:.4e} N·m·s/rad",
            f"  Tc (库仑摩擦) = {tc_hat:.4e} N·m",
            f"  J  (转动惯量) = {j_hat:.4e} kg·m²",
        ]
        if self._comm.is_sim_running():
            sp = self._comm.motor_sim_params()
            def err(est, true):
                return f"{(est - true) / true * 100.0:+.1f}%" if true else "--"
            lines += [
                "",
                "──── 与虚拟电机真值对比（辨识精度验证）────",
                f"  B : 真值 {sp.B:.4e}   误差 {err(b_hat, sp.B)}",
                f"  Tc: 真值 {sp.T_coulomb:.4e}   误差 {err(tc_hat, sp.T_coulomb)}",
                f"  J : 真值 {sp.J:.4e}   误差 {err(j_hat, sp.J)}",
            ]
        self._report.setPlainText("\n".join(lines))

    def _on_apply(self) -> None:
        if not self._result:
            return
        sp = self._comm.motor_sim_params()
        sp.B = self._result["B"]
        sp.T_coulomb = self._result["Tc"]
        sp.J = self._result["J"]
        sp.psi_f = float(self._psi_f.value())
        sp.pole_pairs = int(self._pole_pairs.value())
        self._status.setText("已写入数字孪生参数")
        logger.log("参数辨识", "辨识结果已应用到数字孪生")
