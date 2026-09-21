"""参数辨识页面：两点稳态 + 滑行实验，辨识 B / Tc / J。

原理（只用转速+电流遥测，以 ψf 为转矩锚点）：
  稳态：Kt·iq = B·ω + Tc，两个转速点解出 B、Tc（Kt = 1.5·p·ψf）
  滑行：J·dω/dt = −(B·ω + Tc)，解析衰减区间拟合 J
注意：仅凭转速/电流数据转矩尺度不可观测，ψf 必须由铭牌或
反电动势实验提供；仿真模式下可用虚拟电机真值验证辨识精度。
"""
import math
import os
import time

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.native_telemetry import run_offline_rls_analysis
from communications.protocol import encode_frame
from config.config import CMD_START, CMD_STOP
from controllers.param_identify import fit_inertia, solve_friction, torque_constant
from logs.operation_logger import logger
from rls_offline import load_rls_capture_csv
from widgets.report_dialog import ExperimentReportDialog
from widgets.identify_help_dialog import IdentifyHelpDialog


class _ElectricalIdentifyWorker(QThread):
    completed = Signal(object, object, str)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            dataset = load_rls_capture_csv(self._path)
            analysis = run_offline_rls_analysis(dataset)
        except Exception as exc:
            self.completed.emit(None, None, str(exc))
            return
        self.completed.emit(dataset, analysis, "")


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
        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        mechanical_tab = QWidget()
        mechanical_root = QVBoxLayout(mechanical_tab)
        mechanical_root.setContentsMargins(0, 0, 0, 0)
        tabs.addTab(mechanical_tab, "机械参数 B / Tc / J")

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
        mechanical_root.addWidget(cfg_box)

        # ---- 控制与状态 ----
        h = QHBoxLayout()
        self._btn_run = QPushButton("开始辨识实验")
        self._btn_run.setObjectName("PrimaryButton")
        self._btn_run.clicked.connect(self._on_run)
        self._btn_apply = QPushButton("应用到数字孪生")
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._on_apply)
        self._btn_report = QPushButton("AI 实验报告")
        self._btn_report.setEnabled(False)
        self._btn_report.setToolTip("实验完成后，由 AI 生成格式化实验报告（可保存 Markdown）")
        self._btn_report.clicked.connect(self._on_report)
        self._btn_help = QPushButton("算法说明")
        self._btn_help.setToolTip("辨识用到的物理模型与最小二乘拟合原理")
        self._btn_help.clicked.connect(self._on_help)
        self._status = QLabel("就绪（请先启动仿真或连接真机）")
        h.addWidget(self._btn_run)
        h.addWidget(self._btn_apply)
        h.addWidget(self._btn_report)
        h.addWidget(self._btn_help)
        h.addWidget(self._status, 1)
        mechanical_root.addLayout(h)

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
            "  4) 稳态两点解出 B、Tc；滑行解析衰减区间拟合 J\n")
        rv.addWidget(self._report)
        mechanical_root.addWidget(res_box, 1)

        # ---- 电气参数离线辨识 ----
        electrical_tab = QWidget()
        electrical_root = QVBoxLayout(electrical_tab)
        electrical_root.setContentsMargins(0, 0, 0, 0)
        electrical_bar = QHBoxLayout()
        self._btn_electrical_csv = QPushButton("导入 RLS 辨识 CSV…")
        self._btn_electrical_csv.setObjectName("PrimaryButton")
        self._btn_electrical_csv.clicked.connect(
            self._select_electrical_csv)
        self._btn_electrical_help = QPushButton("公式与可辨识性")
        self._btn_electrical_help.clicked.connect(self._on_help)
        self._electrical_status = QLabel("等待导入同步 RLS 数据")
        electrical_bar.addWidget(self._btn_electrical_csv)
        electrical_bar.addWidget(self._btn_electrical_help)
        electrical_bar.addWidget(self._electrical_status, 1)
        electrical_root.addLayout(electrical_bar)
        method_note = QLabel(
            "两条链路交叉校验：① dq电压方程最小二乘得到R/共享L/磁链；"
            "② 七参数ARX按直流增益和一阶矩投影等效R/L。"
            "七参数不含反电动势/转速回归量，不能单独恢复磁链。")
        method_note.setWordWrap(True)
        method_note.setStyleSheet("color:#90a4ae;")
        electrical_root.addWidget(method_note)
        electrical_box = QGroupBox("电气参数结果与可辨识性校验")
        electrical_box_layout = QVBoxLayout(electrical_box)
        self._electrical_report = QPlainTextEdit()
        self._electrical_report.setReadOnly(True)
        self._electrical_report.setStyleSheet(
            "font-family: Consolas, 'Microsoft YaHei UI', monospace;")
        self._electrical_report.setPlainText(
            "导入“RLS辨识数据.csv”后将显示：\n"
            "  · dq电压方程拟合的 R、共享 L、永磁磁链 ψf\n"
            "  · d/q轴各7个ARX系数的低频等效 R/L 投影\n"
            "  · 两种方法的一致性误差和不可辨识项\n\n"
            "旧格式数据只能给出诊断范围；含独立PRBS和直接Id/IdRef的新版数据"
            "才允许给出物理通过结论。")
        electrical_box_layout.addWidget(self._electrical_report)
        electrical_root.addWidget(electrical_box, 1)
        tabs.addTab(electrical_tab, "电气参数 R / L / ψf")
        self._electrical_worker = None

        comm.telemetryReceived.connect(self._on_telemetry)

    def _select_electrical_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载电气参数辨识数据", "",
            "RLS 辨识数据 (*.csv);;所有文件 (*)")
        if path:
            self._start_electrical_csv(path)

    def _start_electrical_csv(self, path: str) -> None:
        if (self._electrical_worker is not None and
                self._electrical_worker.isRunning()):
            return
        self._btn_electrical_csv.setEnabled(False)
        self._electrical_status.setText(
            f"正在分析 {os.path.basename(path)}…")
        self._electrical_report.setPlainText(
            "正在执行C++ ESO/RLS重放、dq电压方程拟合和交叉校验…")
        self._electrical_worker = _ElectricalIdentifyWorker(path, self)
        self._electrical_worker.completed.connect(
            self._complete_electrical_identification)
        self._electrical_worker.start()

    @staticmethod
    def _format_electrical_analysis(dataset, analysis) -> str:
        def number(value, digits=6):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return "--"
            return f"{value:.{digits}g}" if math.isfinite(value) else "无效"

        legacy = dict((analysis or {}).get("legacy_evidence", {}))
        physical = dict((analysis or {}).get("physical_iv", {}))
        projection = dict((analysis or {}).get("arx_motor_projection", {}))
        cross = dict((analysis or {}).get("arx_motor_cross_check", {}))
        results = list((analysis or {}).get("results", ()))
        lines = [
            "════ 数据与方法 ════",
            f"样本数: {int(dataset['count'])}    采样率: {int(dataset['rate_hz'])} Hz"
            f"    时长: {float(dataset['count']) / float(dataset['rate_hz']):.3f} s",
            "物理链路: dq电压方程分块最小二乘 / 独立PRBS-IV（数据具备时）",
            "校验链路: ARX(3, 双输入)七参数的直流增益+一阶矩投影",
            "",
            "════ dq电压方程结果 ════",
        ]
        if physical.get("verdict") == "usable":
            lines += [
                "数据等级: 独立PRBS-IV物理结果",
                f"Rd/Rq = {number(physical.get('rd_ohm'))} / "
                f"{number(physical.get('rq_ohm'))} Ω",
                f"Ld/Lq = {number(physical.get('ld_mh'))} / "
                f"{number(physical.get('lq_mh'))} mH",
                "ψf: 当前PRBS-IV链路不独立估计磁链",
            ]
        elif legacy.get("verdict") == "diagnostic_only":
            r_range = legacy.get("resistance_ohm_range", (float("nan"),) * 2)
            l_range = legacy.get(
                "shared_inductance_mh_range", (float("nan"),) * 2)
            flux_range = legacy.get("flux_wb_range", (float("nan"),) * 2)
            lines += [
                "数据等级: 旧格式准稳态诊断（不等于物理验收通过）",
                f"R  = {number(legacy.get('resistance_ohm_median'))} Ω"
                f"    范围 {number(r_range[0])}～{number(r_range[1])} Ω",
                f"共享L = {number(legacy.get('shared_inductance_mh_median'))} mH"
                f"    范围 {number(l_range[0])}～{number(l_range[1])} mH",
                f"ψf = {number(1e3 * float(legacy.get('flux_wb_median', float('nan'))))} mWb"
                f"    范围 {number(1e3 * float(flux_range[0]))}～"
                f"{number(1e3 * float(flux_range[1]))} mWb",
                "限制: 只能得到共享L，不能分离Ld/Lq；电压为控制指令而非端电压实测。",
            ]
        else:
            lines.append("没有可用的dq物理参数结果。")

        lines += ["", "════ 七参数ARX低频投影 ════"]
        if results:
            final = results[-1]
            lines.append(
                "θd = [" + ", ".join(number(value) for value in
                                      final.get("theta_d", ())) + "]")
            lines.append(
                "θq = [" + ", ".join(number(value) for value in
                                      final.get("theta_q", ())) + "]")
        for key, label in (("d_axis", "d轴"), ("q_axis", "q轴")):
            axis = dict(projection.get(key, {}))
            validity = "有效" if axis.get("valid") else "无效"
            lines.append(
                f"{label}: R_eq={number(axis.get('resistance_ohm'))} Ω, "
                f"L_eq={number(axis.get('inductance_mh'))} mH  [{validity}]")
            for reason in axis.get("reasons", ()):
                lines.append(f"      - {reason}")
        lines += [
            "ψf_ARX = 不可辨识",
            "原因: 七个回归量没有ωe/反电动势、常数项或ESO扰动状态。",
            "",
            "════ 交叉校验 ════",
            f"参考链路: {cross.get('reference_source', 'none')}",
        ]
        for key, label in (("d_axis", "d轴"), ("q_axis", "q轴")):
            item = dict(cross.get("comparisons", {}).get(key, {}))
            if item.get("comparable"):
                lines.append(
                    f"{label}: ΔR={100.0 * float(item['resistance_relative_error']):+.1f}%  "
                    f"ΔL={100.0 * float(item['inductance_relative_error']):+.1f}%  "
                    f"{'通过' if item.get('matches_tolerance') else '不通过'}")
            else:
                lines.append(f"{label}: 不可比较（{item.get('reason', '无参考')}）")
        absorption = dict(cross.get("back_emf_absorption", {}))
        if absorption.get("available"):
            lines += [
                "",
                "q轴反电动势吸收检验:",
                "  ωe ≈ κ·iq+c，"
                f"κ={number(absorption.get('omega_iq_slope_rad_s_per_a'))} "
                "rad/(s·A)，"
                f"corr={number(absorption.get('omega_iq_correlation'))}",
                "  ΔR_bemf=ψf·κ="
                f"{number(absorption.get('absorbed_resistance_ohm'))} Ω，"
                f"ψf来源={absorption.get('flux_source', 'unknown')}",
                "  Rq预测/ARX实得="
                f"{number(absorption.get('predicted_apparent_resistance_ohm'))}/"
                f"{number(absorption.get('observed_arx_q_resistance_ohm'))} Ω，"
                "偏差="
                f"{100.0 * float(absorption.get('relative_prediction_error')):+.1f}%",
                "  说明未显式建模的ωe·ψf被吸收到q轴自回归/低频增益中。",
            ]
        lines += [
            f"R/L一致性: {'通过' if cross.get('rl_match') else '未通过'}",
            "三参数完整校验: 不可能——ARX七参数无法独立给出ψf。",
            "",
            "结论: 七参数ARX适合复现动态输入输出模型；"
            "物理R/L/ψf必须保留独立dq方程/PRBS链路。",
        ]
        return "\n".join(lines)

    def _complete_electrical_identification(
            self, dataset, analysis, error: str) -> None:
        self._btn_electrical_csv.setEnabled(True)
        if error:
            self._electrical_status.setText(f"辨识失败：{error}")
            self._electrical_report.setPlainText(f"[错误] {error}")
            return
        self._electrical_report.setPlainText(
            self._format_electrical_analysis(dataset, analysis))
        cross = dict((analysis or {}).get("arx_motor_cross_check", {}))
        self._electrical_status.setText(
            "分析完成：R/L交叉校验通过，磁链仍不可由ARX独立辨识"
            if cross.get("rl_match") else
            "分析完成：物理结果已保留，ARX投影校验未通过")

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
        last_sample = self._records[-1] if self._records else None
        self._enter_phase("coast")
        # 保留封管瞬间的初始转速。小惯量电机可能在下一帧 100 ms 遥测
        # 到来前已下降大半，缺少该点会丢掉信息量最大的衰减区间。
        if last_sample is not None:
            self._records.append(
                (time.time(), last_sample[1], last_sample[2]))
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
        kt = torque_constant(float(self._psi_f.value()),
                             int(self._pole_pairs.value()))
        (w1, i1), (w2, i2) = self._steady1, self._steady2
        b_hat, tc_hat = solve_friction(w1, i1, w2, i2, kt)
        j_hat, used = fit_inertia([(t, rpm) for t, rpm, _ in coast],
                                  b_hat, tc_hat)

        self._result = {"B": b_hat, "Tc": tc_hat, "J": j_hat}
        self._btn_apply.setEnabled(True)
        self._status.setText("辨识完成")
        logger.log("参数辨识", f"B={b_hat:.3e} Tc={tc_hat:.3e} J={j_hat:.3e}")

        # 供 AI 实验报告使用的完整上下文
        self._report_ctx_head = (
            "实验类型：电机参数辨识（两点稳态 + 滑行实验）\n"
            f"实验时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
            f"数据来源：{'数字孪生仿真' if self._comm.is_sim_running() else '真机'}\n"
            "实验原理：稳态 Kt·iq = B·ω + Tc 两点解 B/Tc；"
            "滑行 J·dω/dt = −(B·ω + Tc) 解析衰减区间拟合 J\n"
            f"实验配置：ψf={self._psi_f.value()} Wb，"
            f"极对数={self._pole_pairs.value()}，"
            f"稳态点 n1={self._n1.value()} rpm，n2={self._n2.value()} rpm，"
            f"滑行采样 {len(coast)} 帧\n")

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
        self._btn_report.setEnabled(True)

    def _on_report(self) -> None:
        ctx = (getattr(self, "_report_ctx_head", "")
               + "\n实验数据与结果：\n" + self._report.toPlainText())
        ExperimentReportDialog("参数辨识实验", ctx, parent=self).exec()

    def _on_help(self) -> None:
        IdentifyHelpDialog(parent=self).exec()

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
