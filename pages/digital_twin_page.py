"""数字孪生工作台：直接驱动 Simulink Coder 生成的 C++ 模型。"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from communications.comm_manager import CommManager
from core import RuntimeState, RuntimeStateMachine, TransitionError
from logs.operation_logger import logger
from simulink_host import (
    SimulinkEngineThread,
    SimulinkHostInputs,
    SimulinkHostParameters,
    resolve_simulink_dll,
)
from widgets.trend_curve import TrendCurve


def compute_simulated_load(load_type_idx: int, value: float, rpm: float) -> float:
    """按数字孪生负载模型计算当前外部负载转矩。"""
    if load_type_idx == 0:
        return 0.0
    if load_type_idx == 2:
        return value * (rpm / 1000.0) ** 2
    return value


class DigitalTwinPage(QWidget):
    """由 R2024b Simulink Coder C++ 模型驱动的数字孪生工作台。"""

    def __init__(self, comm: CommManager,
                 runtime_state: RuntimeStateMachine | None = None) -> None:
        super().__init__()
        self._comm = comm
        self._runtime_state = runtime_state
        self._last_speed_rpm = 0.0
        self._last_sim_time = 0.0
        self._environment_ready = False
        self._engine_state = "stopped"
        self._engine: SimulinkEngineThread | None = None
        self._dll_path = resolve_simulink_dll()
        self._pulse_delta_nm = 0.0
        self._pulse_until = 0.0
        self._ui_refresh_timer = QTimer(self)
        self._ui_refresh_timer.setInterval(33)  # 约30 FPS，且只读取最新帧
        self._ui_refresh_timer.timeout.connect(self._drain_engine_sample)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        title_row = QHBoxLayout()
        title = QLabel("数字孪生工作台")
        title.setObjectName("TitleLabel")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._source_badge = QLabel("[ C++ 模型未启动 ]")
        self._source_badge.setStyleSheet("color: #90a4ae; font-weight: bold;")
        title_row.addWidget(self._source_badge)
        root.addLayout(title_row)

        boundary = QLabel(
            "本页面直接运行 MATLAB R2024b Simulink Coder 生成的 PMSM C++ 模型。"
            "控制周期为 10 µs；模型在后台线程批量步进，界面仅以低频读取状态，"
            "不会用 Python 逐点执行控制算法。")
        boundary.setWordWrap(True)
        boundary.setObjectName("HintLabel")
        boundary.setStyleSheet(
            "QLabel { color: #9fb3c8; background: rgba(23, 43, 61, 0.72); "
            "border: 1px solid rgba(64, 180, 255, 0.28); border-radius: 8px; "
            "padding: 10px 12px; }")
        root.addWidget(boundary)

        top = QHBoxLayout()
        top.addWidget(self._build_runtime_box(), 3)
        top.addWidget(self._build_live_box(), 2)
        root.addLayout(top)
        root.addWidget(self._build_load_box())

        curves = QHBoxLayout()
        self._curve_speed = TrendCurve(
            "转速", {"实际转速": "#55d6be", "目标转速": "#ffc857"}, "rpm",
            buffer_size=3000)
        self._curve_current = TrendCurve(
            "dq 电流", {"Id": "#62b5ff", "Iq": "#ff6b8a"}, "A",
            buffer_size=3000)
        self._curve_torque = TrendCurve(
            "电磁转矩与负载", {"电磁转矩": "#b388ff", "负载": "#ffb74d"},
            "N·m", buffer_size=3000)
        for curve in (self._curve_speed, self._curve_current,
                      self._curve_torque):
            curve.setMinimumHeight(220)
            curve.set_view_window(8.0)
            curves.addWidget(curve, 1)
        root.addLayout(curves)

        comm.statusChanged.connect(self._on_comm_status_changed)
        if runtime_state is not None:
            runtime_state.stateChanged.connect(
                lambda _previous, _current, _reason: self._sync_controls())
        self._sync_controls()

    def _build_runtime_box(self) -> QGroupBox:
        box = QGroupBox("Simulink C++ 仿真运行")
        form = QFormLayout(box)

        self._target_speed = QSpinBox()
        self._target_speed.setRange(-4000, 4000)
        self._target_speed.setValue(500)
        self._target_speed.setSuffix(" rpm")
        self._target_speed.valueChanged.connect(self._update_engine_inputs)
        form.addRow("目标转速", self._target_speed)

        self._id_ref = self._double_spin(-100.0, 100.0, 0.0, 3, 0.1)
        self._id_ref.setSuffix(" A")
        self._id_ref.valueChanged.connect(self._update_engine_inputs)
        form.addRow("Id 给定", self._id_ref)

        parameter_row = QHBoxLayout()
        self._speed_kp = self._double_spin(0.0, 1000.0, 0.8, 4, 0.05)
        self._speed_ki = self._double_spin(0.0, 10000.0, 3.5, 4, 0.1)
        self._iq_limit = self._double_spin(0.01, 500.0, 5.0, 3, 0.5)
        parameter_row.addWidget(QLabel("Kp"))
        parameter_row.addWidget(self._speed_kp)
        parameter_row.addWidget(QLabel("Ki"))
        parameter_row.addWidget(self._speed_ki)
        parameter_row.addWidget(QLabel("Iq限幅"))
        parameter_row.addWidget(self._iq_limit)
        form.addRow("速度环参数", parameter_row)

        self._noise_variance = self._double_spin(
            0.0, 100.0, 0.006, 6, 0.001)
        form.addRow("电流噪声方差", self._noise_variance)

        apply_parameters = QPushButton("应用在线参数")
        apply_parameters.clicked.connect(self._apply_online_parameters)
        form.addRow("", apply_parameters)

        environment_row = QHBoxLayout()
        self._btn_environment = QPushButton("加载 C++ 模型")
        self._btn_environment.clicked.connect(self._toggle_environment)
        environment_row.addWidget(self._btn_environment)
        self._dll_status = QLabel()
        self._dll_status.setWordWrap(True)
        environment_row.addWidget(self._dll_status, 1)
        form.addRow("模型环境", environment_row)

        run_row = QHBoxLayout()
        self._btn_run = QPushButton("启动仿真")
        self._btn_run.setObjectName("PrimaryButton")
        self._btn_run.clicked.connect(self._run_model)
        self._btn_pause = QPushButton("暂停")
        self._btn_pause.clicked.connect(self._toggle_pause)
        self._btn_reset = QPushButton("复位")
        self._btn_reset.clicked.connect(self._reset_model)
        self._btn_stop = QPushButton("停止")
        self._btn_stop.clicked.connect(self._stop_model)
        for button in (self._btn_run, self._btn_pause,
                       self._btn_reset, self._btn_stop):
            run_row.addWidget(button)
        form.addRow("模型控制", run_row)

        self._runtime_hint = QLabel()
        self._runtime_hint.setWordWrap(True)
        self._runtime_hint.setStyleSheet("color: #8fa3b8;")
        form.addRow(self._runtime_hint)
        return box

    def _build_live_box(self) -> QGroupBox:
        box = QGroupBox("C++ 模型状态")
        form = QFormLayout(box)
        self._live_speed = QLabel("— rpm")
        self._live_id = QLabel("— A")
        self._live_current = QLabel("— A")
        self._live_torque = QLabel("— N·m")
        self._live_voltage = QLabel("— V")
        self._live_angle = QLabel("— rad")
        self._live_time = QLabel("— s")
        form.addRow("转速", self._live_speed)
        form.addRow("Id", self._live_id)
        form.addRow("Iq", self._live_current)
        form.addRow("电磁转矩", self._live_torque)
        form.addRow("Ud / Uq", self._live_voltage)
        form.addRow("电角度", self._live_angle)
        form.addRow("仿真时间", self._live_time)
        return box

    def _build_load_box(self) -> QGroupBox:
        box = QGroupBox("负载与机械（Simulink C++）")
        form = QFormLayout(box)

        self._load_type = QComboBox()
        self._load_type.addItems([
            "空载", "恒转矩负载", "风机/泵类 (∝ω²)", "对拖系统 (可正负/回馈)"])
        self._load_type.currentIndexChanged.connect(self._on_load_type_changed)
        self._load_value = self._double_spin(-100.0, 100.0, 0.0, 3, 0.05)
        self._load_value.setEnabled(False)
        self._load_value.valueChanged.connect(self._update_engine_inputs)
        self._load_value.setToolTip(
            "恒转矩/对拖：转矩 N·m；风机泵类：1000 rpm 时的负载转矩。")

        self._visc_b = self._double_spin(0.0, 1e4, 0.0002024, 7, 0.00005)
        self._inertia = self._double_spin(1e-8, 1e4, 0.0016, 7, 0.0001)
        self._coulomb = self._double_spin(0.0, 1e4, 0.0, 4, 0.01)
        self._visc_b.setEnabled(False)
        self._inertia.setEnabled(False)
        self._coulomb.setEnabled(False)
        fixed_mechanical_tip = "当前 PMSM 库块将此参数编译为常量，不能在线修改。"
        self._visc_b.setToolTip(fixed_mechanical_tip)
        self._inertia.setToolTip(fixed_mechanical_tip)
        self._coulomb.setToolTip("当前 PMSM 模型未独立开放库仑摩擦参数。")

        form.addRow("负载类型", self._load_type)
        form.addRow("负载转矩/系数", self._load_value)
        form.addRow("粘滞摩擦 B (模型固定)", self._visc_b)
        form.addRow("库仑摩擦 Tc (未开放)", self._coulomb)
        form.addRow("转动惯量 J (模型固定)", self._inertia)

        apply_button = QPushButton("应用到 C++ 模型")
        apply_button.clicked.connect(self._apply_mechanical)
        form.addRow("", apply_button)

        self._disturb_amp = self._double_spin(0.01, 100.0, 0.3, 3, 0.05)
        self._disturb_dur = self._double_spin(0.1, 60.0, 1.0, 1, 0.5)
        self._disturb_period = self._double_spin(0.2, 60.0, 2.0, 1, 0.5)
        form.addRow("扰动幅值 (N·m)", self._disturb_amp)

        pulse_row = QHBoxLayout()
        self._btn_pulse_add = QPushButton("突加负载")
        self._btn_pulse_add.clicked.connect(lambda: self._pulse_load(+1))
        self._btn_pulse_shed = QPushButton("突卸负载")
        self._btn_pulse_shed.clicked.connect(lambda: self._pulse_load(-1))
        pulse_row.addWidget(self._btn_pulse_add)
        pulse_row.addWidget(self._btn_pulse_shed)
        pulse_row.addWidget(QLabel("持续(s)"))
        pulse_row.addWidget(self._disturb_dur)
        form.addRow("一次性扰动", pulse_row)

        periodic_row = QHBoxLayout()
        self._btn_disturb = QPushButton("开启周期扰动")
        self._btn_disturb.setCheckable(True)
        self._btn_disturb.toggled.connect(self._toggle_disturbance)
        periodic_row.addWidget(self._btn_disturb)
        periodic_row.addWidget(QLabel("周期(s)"))
        periodic_row.addWidget(self._disturb_period)
        form.addRow("周期扰动", periodic_row)

        self._mech_status = QLabel("机械参数采用 HOST 模型默认值")
        self._mech_status.setWordWrap(True)
        self._mech_status.setStyleSheet("color: #8fa3b8;")
        form.addRow(self._mech_status)
        return box

    @staticmethod
    def _double_spin(minimum: float, maximum: float, value: float,
                     decimals: int, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def mechanical_snapshot(self) -> dict:
        """供实验归档读取当前 Simulink C++ 仿真设置。"""
        return {
            "simulation_backend": "simulink-coder-cpp",
            "load_type": self._load_type.currentText(),
            "load_value": self._load_value.value(),
            "viscous_friction_B": self._visc_b.value(),
            "coulomb_friction_Tc": self._coulomb.value(),
            "inertia_J": self._inertia.value(),
            "speed_kp": self._speed_kp.value(),
            "speed_ki": self._speed_ki.value(),
            "iq_limit_A": self._iq_limit.value(),
            "current_noise_variance": self._noise_variance.value(),
            "disturbance_amplitude_Nm": self._disturb_amp.value(),
            "disturbance_duration_s": self._disturb_dur.value(),
            "disturbance_period_s": self._disturb_period.value(),
            "periodic_disturbance_enabled": self._btn_disturb.isChecked(),
        }

    def _toggle_environment(self) -> None:
        if self._environment_ready:
            self._stop_environment()
        else:
            self._start_environment()

    def _start_environment(self) -> bool:
        if self._comm.is_connected() or self._comm.is_sim_running():
            QMessageBox.warning(
                self, "不可用", "真实设备或其他仿真数据源已连接，请先断开。")
            return False
        self._dll_path = resolve_simulink_dll()
        if self._dll_path is None:
            QMessageBox.warning(
                self, "模型不可用",
                "未找到 pmsm_simulink_host.dll。请先构建 HOST 模型与 C++ DLL。")
            self._sync_controls()
            return False
        self._environment_ready = True
        if self._runtime_state is not None:
            self._runtime_state.connection_changed(True, "Simulink C++ 环境已加载")
        self._runtime_hint.setText("C++ 模型已加载，可以启动仿真。")
        logger.log("加载 Simulink C++ 数字孪生", str(self._dll_path))
        self._sync_controls()
        return True

    def _stop_environment(self) -> None:
        if self._model_enabled():
            QMessageBox.warning(self, "不能卸载模型", "模型仍在运行，请先停止。")
            return
        self._environment_ready = False
        if self._runtime_state is not None:
            self._runtime_state.connection_changed(False, "Simulink C++ 环境已卸载")
        self._runtime_hint.setText("C++ 模型已卸载。")
        logger.log("卸载 Simulink C++ 数字孪生")
        self._sync_controls()

    def _run_model(self) -> None:
        if not self._environment_ready and not self._start_environment():
            return
        if self._engine is not None and self._engine.isRunning():
            if self._engine_state == "paused":
                self._engine.set_paused(False)
            return
        if self._runtime_state is not None:
            try:
                if self._runtime_state.state is RuntimeState.CONNECTED:
                    self._runtime_state.begin_precheck("Simulink C++ 模型检查")
                    self._runtime_state.pass_precheck("Simulink C++ 模型检查通过")
                if self._runtime_state.state is not RuntimeState.READY:
                    raise TransitionError(
                        f"当前不能启动（状态：{self._runtime_state.state.value}）")
            except TransitionError as exc:
                QMessageBox.warning(self, "无法启动模型", str(exc))
                return

        self._apply_mechanical()
        self._clear_curves()
        self._engine_state = "starting"
        self._engine = SimulinkEngineThread(
            self._dll_path, self._input_snapshot(), self._parameter_snapshot(),
            parent=self)
        self._engine.stateChanged.connect(self._on_engine_state_changed)
        self._engine.failed.connect(self._on_engine_failed)
        self._engine.finished.connect(self._on_engine_finished)
        self._engine.start()
        self._ui_refresh_timer.start()
        self._runtime_hint.setText("正在初始化并启动 C++ 模型……")
        logger.log("启动 Simulink C++ 模型",
                   f"目标转速={self._target_speed.value()} rpm")
        self._sync_controls()

    def _toggle_pause(self) -> None:
        if self._engine is None or not self._engine.isRunning():
            return
        self._engine.set_paused(self._engine_state != "paused")

    def _reset_model(self) -> None:
        if self._engine is None or not self._engine.isRunning():
            return
        self._engine.reset_model()
        self._last_sim_time = 0.0
        self._clear_curves()
        self._runtime_hint.setText("模型已请求复位，状态将在下一批步进前清零。")
        logger.log("复位 Simulink C++ 模型")

    def _stop_model(self) -> None:
        if self._engine is None or not self._engine.isRunning():
            self._runtime_hint.setText("C++ 模型尚未运行。")
            return
        if (self._runtime_state is not None and
                self._runtime_state.state is RuntimeState.RUNNING):
            self._runtime_state.request_stop("用户停止 Simulink C++ 模型")
        self._engine_state = "stopping"
        self._engine.stop_model()
        self._btn_disturb.setChecked(False)
        self._runtime_hint.setText("正在停止 C++ 模型……")
        logger.log("停止 Simulink C++ 模型")
        self._sync_controls()

    def _on_load_type_changed(self, index: int) -> None:
        self._load_value.setEnabled(index != 0)
        self._update_engine_inputs()

    def _base_load_nm(self) -> float:
        return compute_simulated_load(
            self._load_type.currentIndex(), self._load_value.value(),
            abs(self._last_speed_rpm))

    def _current_load_nm(self) -> float:
        load = self._base_load_nm()
        if time.monotonic() < self._pulse_until:
            load += self._pulse_delta_nm
        else:
            self._pulse_delta_nm = 0.0
        if self._btn_disturb.isChecked():
            period = max(0.2, self._disturb_period.value())
            load += self._disturb_amp.value() * math.sin(
                2.0 * math.pi * self._last_sim_time / period)
        return load

    def _input_snapshot(self) -> SimulinkHostInputs:
        return SimulinkHostInputs(
            speed_ref_rpm=float(self._target_speed.value()),
            id_ref_a=self._id_ref.value(),
            load_torque_nm=self._current_load_nm(),
        )

    def _parameter_snapshot(self) -> SimulinkHostParameters:
        return SimulinkHostParameters(
            speed_kp=self._speed_kp.value(),
            speed_ki=self._speed_ki.value(),
            iq_limit_a=self._iq_limit.value(),
            current_noise_variance=self._noise_variance.value(),
        )

    def _update_engine_inputs(self, *_args) -> None:
        if self._engine is not None and self._engine.isRunning():
            self._engine.update_inputs(self._input_snapshot())

    def _apply_online_parameters(self) -> None:
        if self._engine is not None and self._engine.isRunning():
            self._engine.update_parameters(self._parameter_snapshot())
            self._runtime_hint.setText("在线参数已提交，将在下一批步进前生效。")
        else:
            self._runtime_hint.setText("参数已保存，将在下次启动模型时生效。")

    def _apply_mechanical(self) -> None:
        self._update_engine_inputs()
        self._apply_online_parameters()
        where = "负载已在线提交" if self._model_enabled() else "负载将在启动后生效"
        self._mech_status.setText(
            f"{self._load_type.currentText()}：{self._base_load_nm():.3f} N·m，"
            f"B={self._visc_b.value():.7g}、J={self._inertia.value():.7g}为模型固定值；"
            f"{where}")
        logger.log(
            "应用 Simulink C++ 机械参数",
            f"负载={self._base_load_nm()} B={self._visc_b.value()} "
            f"J={self._inertia.value()}")

    def _pulse_load(self, sign: int) -> None:
        if not self._model_enabled():
            self._mech_status.setText("请先启动 C++ 模型，再施加负载扰动。")
            return
        self._pulse_delta_nm = sign * self._disturb_amp.value()
        self._pulse_until = time.monotonic() + self._disturb_dur.value()
        self._update_engine_inputs()
        action = "突加" if sign > 0 else "突卸"
        self._mech_status.setText(
            f"{action}负载 {abs(self._pulse_delta_nm):.3f} N·m，"
            f"持续 {self._disturb_dur.value():.1f} s。")

    def _toggle_disturbance(self, enabled: bool) -> None:
        if enabled and not self._model_enabled():
            self._btn_disturb.blockSignals(True)
            self._btn_disturb.setChecked(False)
            self._btn_disturb.blockSignals(False)
            self._mech_status.setText("请先启动 C++ 模型，再开启周期扰动。")
            return
        self._btn_disturb.setText("停止周期扰动" if enabled else "开启周期扰动")
        self._update_engine_inputs()

    def _on_engine_sample(self, sample: dict) -> None:
        self._last_speed_rpm = float(sample["speed_rpm"])
        self._last_sim_time = float(sample["simulation_time_s"])
        self._live_speed.setText(f"{self._last_speed_rpm:.2f} rpm")
        self._live_id.setText(f"{float(sample['id_a']):.4f} A")
        self._live_current.setText(f"{float(sample['iq_a']):.4f} A")
        self._live_torque.setText(f"{float(sample['torque_nm']):.4f} N·m")
        self._live_voltage.setText(
            f"{float(sample['ud_v']):.3f} / {float(sample['uq_v']):.3f} V")
        self._live_angle.setText(f"{float(sample['theta_e_rad']):.4f} rad")
        self._live_time.setText(f"{self._last_sim_time:.3f} s")
        self._curve_speed.append({
            "实际转速": self._last_speed_rpm,
            "目标转速": float(sample["speed_ref_rpm"]),
        })
        self._curve_current.append({
            "Id": float(sample["id_a"]),
            "Iq": float(sample["iq_a"]),
        })
        self._curve_torque.append({
            "电磁转矩": float(sample["torque_nm"]),
            "负载": float(sample["load_torque_ref_nm"]),
        })
        self._update_engine_inputs()

    def _drain_engine_sample(self) -> None:
        """按 UI 帧率读取最新模型状态，不回放已经过时的排队帧。"""
        engine = self._engine
        if engine is None:
            return
        sample = engine.take_latest_sample()
        if sample is not None:
            self._on_engine_sample(sample)

    def _on_engine_state_changed(self, state: str) -> None:
        self._engine_state = state
        if state == "running":
            if (self._runtime_state is not None and
                    self._runtime_state.state is RuntimeState.READY):
                self._runtime_state.confirm_started("Simulink C++ 模型已启动")
            self._runtime_hint.setText("C++ 模型运行中，按 10 µs 固定步长推进。")
        elif state == "paused":
            self._runtime_hint.setText("C++ 模型已暂停，内部状态保持不变。")
        elif state == "stopped":
            self._ui_refresh_timer.stop()
            self._drain_engine_sample()
            if (self._runtime_state is not None and
                    self._runtime_state.state is RuntimeState.STOPPING):
                self._runtime_state.confirm_stopped("Simulink C++ 模型已停止")
            self._runtime_hint.setText("C++ 模型已停止。")
        self._sync_controls()

    def _on_engine_failed(self, message: str) -> None:
        self._ui_refresh_timer.stop()
        self._runtime_hint.setText(f"C++ 模型错误：{message}")
        logger.log("Simulink C++ 模型错误", message)
        QMessageBox.critical(self, "Simulink C++ 模型错误", message)

    def _on_engine_finished(self) -> None:
        self._ui_refresh_timer.stop()
        self._drain_engine_sample()
        self._engine = None
        self._sync_controls()

    def _clear_curves(self) -> None:
        for curve in (self._curve_speed, self._curve_current,
                      self._curve_torque):
            curve.clear()

    def _model_enabled(self) -> bool:
        return self._engine_state in {"starting", "running", "paused", "stopping"}

    def _on_comm_status_changed(self, _connected: bool, _message: str) -> None:
        self._sync_controls()

    def _sync_controls(self) -> None:
        dll_available = self._dll_path is not None
        active = self._model_enabled()
        running = self._engine_state == "running"
        paused = self._engine_state == "paused"

        self._dll_status.setText(
            "R2024b DLL 已找到" if dll_available else "未找到 C++ 模型 DLL")
        self._dll_status.setStyleSheet(
            "color: #66e3b4;" if dll_available else "color: #ff6b8a;")
        if not self._runtime_hint.text():
            self._runtime_hint.setText(
                "先加载 C++ 模型，再启动仿真。" if dll_available else
                "请先生成并编译 pmsm_simulink_host.dll。")

        self._btn_environment.setText(
            "卸载 C++ 模型" if self._environment_ready else "加载 C++ 模型")
        self._btn_environment.setEnabled(dll_available and not active)
        self._btn_run.setEnabled(self._environment_ready and not active)
        self._btn_pause.setEnabled(running or paused)
        self._btn_pause.setText("继续" if paused else "暂停")
        self._btn_reset.setEnabled(running or paused)
        self._btn_stop.setEnabled(active)

        if running:
            badge, color = "[ Simulink C++ 运行中 ]", "#66e3b4"
        elif paused:
            badge, color = "[ Simulink C++ 已暂停 ]", "#ffc857"
        elif self._environment_ready:
            badge, color = "[ C++ 模型已加载 ]", "#62b5ff"
        else:
            badge, color = "[ C++ 模型未启动 ]", "#90a4ae"
        self._source_badge.setText(badge)
        self._source_badge.setStyleSheet(f"color: {color}; font-weight: bold;")

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """同步停止模型线程，供页面和主窗口安全退出时调用。

        主窗口关闭不会自动触发所有子页面的 closeEvent，因此退出流程必须
        显式调用此方法。若原生模型未能及时返回，拒绝销毁仍在运行的 QThread，
        避免 Qt 在解释器退出阶段直接中止进程。
        """
        self._ui_refresh_timer.stop()
        engine = self._engine
        if engine is None:
            return True
        if engine.isRunning():
            engine.stop_model()
            if not engine.wait(max(0, int(timeout_ms))):
                logger.log(
                    "Simulink C++ 模型退出超时",
                    f"等待后台线程 {int(timeout_ms)} ms 后仍未停止")
                return False
        self._engine_state = "stopped"
        self._engine = None
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)
