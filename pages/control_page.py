"""电机控制页面：电机信息、位置传感器、控制方式、参数面板、控制按钮。"""
import json
import math
import time
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QInputDialog, QLineEdit, QMessageBox, QPushButton, QSizePolicy, QSpinBox,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from core import RuntimeState, RuntimeStateMachine, TransitionError
from config.config import (
    CMD_EMERGENCY_STOP, CMD_SET_PARAMS, CMD_SET_SENSOR,
    CMD_START, CMD_STOP,
    CONTROL_MODES_BY_MOTOR, MOTOR_TYPES, POSITION_SENSORS, SENSOR_REGISTRY,
)
from controllers.angle_position_controller import AnglePositionController
from controllers.current_chopping_controller import CurrentChoppingController
from controllers.mpc_controller import MPCController
from controllers.openloop_controller import OpenLoopController
from controllers.pi_controller import PIController
from controllers.position_controller import PositionController
from controllers.sensorless_controller import SensorlessController
from controllers.voltage_control_controller import VoltageControlController
from pages.control_param_panels import (
    AnglePositionPanel, CurrentChoppingPanel, EKFPanel, HFIPanel, HallPanel,
    MPCPanel, MRASPanel, OpenLoopPanel, PIPanel, PositionPanel, QEPPanel, ResolverPanel,
    SMOPanel, SensorlessPanel, VoltageControlPanel,
)
from widgets.motor_info_dialog import MotorInfoDialog, load_motor_info
from widgets.sensor_detail_dialog import SensorDetailDialog
from logs.operation_logger import logger
from runtime_paths import writable_path


_PI_PROFILE_FILE = "config/pi_parameter_profiles.json"
_BUILTIN_PI_PROFILE = "稳定基线（1752/121，2323/2077）"

_PROFILE_WIDGET_ALIASES = {
    "spd_sample_time": "dt_spd",
    "cur_sample_time": "dt_cur",
    "pos_sample_time": "dt_pos",
    "position_ff_lpf_hz": "ff_lpf_hz",
    "iq_ref_a": "iq_ref",
    "iq_ramp_ms": "ramp_ms",
    "replaced_loop": "loop",
    "prediction_horizon": "N",
    "control_horizon": "M",
    "weight_q": "q",
    "weight_r": "r",
    "u_min": "umin",
    "u_max": "umax",
    "delta_u_max": "dumax",
    "x_min": "xmin",
    "x_max": "xmax",
    "observer_gain": "gain",
    "start_current": "start_curr",
    "current_upper": "i_up",
    "current_lower": "i_low",
    "chopping_frequency": "f_chop",
    "hysteresis_band": "band",
    "turn_on_angle": "theta_on",
    "turn_off_angle": "theta_off",
    "advance_angle": "theta_adv",
    "current_limit": "i_limit",
    "dc_bus_voltage": "vdc",
    "pwm_frequency": "f_pwm",
    "voltage_limit": "v_limit",
}

# F407 apply_runtime_params only parses these keys. Sending Chinese UI meta
# plus duplicate kp/ki/sample_time fields used to push SET_PARAMS over the
# 384-byte firmware copy buffer, so the whole apply was NACKed.
_FIRMWARE_RUNTIME_KEYS = (
    "control_mode",
    "target",
    "max_rpm",
    "max_current_a",
    "iq_max",
    "kp_spd",
    "ki_spd",
    "kp_cur",
    "ki_cur",
    "position_target_deg",
    "kp_pos",
    "kd_pos",
    "kpf_pos",
    "position_ff_lpf_hz",
    "position_speed_limit_rpm",
    "position_accel_limit_rpm_s",
    "position_speed_ff_rpm",
    "iq_ref_a",
    "iq_ramp_ms",
    "current_filter_enabled",
    "current_filter_alpha_q15",
    "speed_fifo_depth",
)
_TELEMETRY_FRESH_S = 1.5
_CURRENT_LOOP_SAMPLE_RATE_HZ = 16000.0
# Eco telemetry (F1 200 Hz, F2/F3 off). Must NOT be sent in the same click as
# START: stacking 0x22 + 0x10 made START miss its 1 s ACK window in lab.


def firmware_runtime_payload(values: dict) -> bytes:
    """Compact SET_PARAMS/START body the F407 parser can copy and apply."""
    parts = []
    for key in _FIRMWARE_RUNTIME_KEYS:
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, float):
            text = f"{value:.6g}"
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    return ";".join(parts).encode("utf-8")


def current_filter_alpha_q15(cutoff_hz: float) -> int:
    """把一阶低通截止频率转换成 y=alpha*x+(1-alpha)*y1 的Q15系数。"""
    cutoff = float(cutoff_hz)
    if not math.isfinite(cutoff) or not 1000.0 <= cutoff <= 4000.0:
        raise ValueError("电流反馈滤波截止频率必须在1000..4000 Hz")
    alpha = 1.0 - math.exp(
        -2.0 * math.pi * cutoff / _CURRENT_LOOP_SAMPLE_RATE_HZ)
    return max(1, min(32767, round(alpha * 32768.0)))


def current_filter_cutoff_hz(alpha_q15: int) -> float:
    """把固件回读Q15系数还原为16 kHz采样下的等效截止频率。"""
    alpha = int(alpha_q15) / 32768.0
    if not 0.0 < alpha < 1.0:
        return 0.0
    return (-_CURRENT_LOOP_SAMPLE_RATE_HZ / (2.0 * math.pi)
            * math.log(1.0 - alpha))


def _field_with_hint(widget: QWidget, hint: str) -> QWidget:
    """在输入框右侧显示低对比度的范围说明。"""
    field = QWidget()
    field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    widget_policy = widget.sizePolicy()
    widget_policy.setHorizontalPolicy(QSizePolicy.Expanding)
    widget.setSizePolicy(widget_policy)
    row = QHBoxLayout(field)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addWidget(widget, 1)
    range_label = QLabel(hint)
    range_label.setObjectName("RangeHintLabel")
    range_label.setStyleSheet("color: #748291; font-size: 11px;")
    range_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(range_label)
    return field


# 控制方式名称 → (控制器类, 参数面板类) 映射
_MODE_REGISTRY = {
    "闭环PI控制":          (PIController,            PIPanel),
    "位置三环控制":        (PositionController,      PositionPanel),
    "开环控制":            (OpenLoopController,      OpenLoopPanel),
    "模型预测控制(MPC)":    (MPCController,           MPCPanel),
    "无位置传感器控制":     (SensorlessController,    SensorlessPanel),
    "电流斩波控制(CCC)":   (CurrentChoppingController, CurrentChoppingPanel),
    "角度位置控制(APC)":   (AnglePositionController,  AnglePositionPanel),
    "电压PWM控制":         (VoltageControlController, VoltageControlPanel),
}

# 传感器名称 → 参数面板类（顺序与 POSITION_SENSORS 一致）
_SENSOR_PANEL_CLS = {
    "霍尔传感器(Hall)": HallPanel,
    "增量式编码器(QEP)": QEPPanel,
    "旋转变压器(Resolver)": ResolverPanel,
    "无位置传感器-滑模观测器(SMO)": SMOPanel,
    "无位置传感器-扩展卡尔曼(EKF)": EKFPanel,
    "无位置传感器-模型参考自适应(MRAS)": MRASPanel,
    "无位置传感器-高频注入(HFI)": HFIPanel,
}

# 控制方式名称后追加的警告标记
_WARN_SUFFIX = " ⚠"

# 各控制方式的一句话原理 + 适用场景（选中时显示在下拉框下方）
_MODE_DESCRIPTIONS = {
    "闭环PI控制":
        "转速/电流双闭环 PI，工业标配：参数少、易整定、稳态精度高。"
        "适合绝大多数调速场景。",
    "位置三环控制":
        "PMSM 位置—速度—电流级联：位置比例加目标速度前馈输出速度给定，"
        "复用现有速度 PI 与电流 PI；用于对拖台架的位置阶跃和轨迹跟踪。",
    "开环控制":
        "速度环旁路、d/q电流环闭环，直接给定受限 Iqref。"
        "专用于电流环 PI 整定，不是普通 V/f 开环。",
    "模型预测控制(MPC)":
        "基于电机模型滚动优化未来若干拍的控制量，动态响应快、可显式处理"
        "电流/电压约束；依赖参数准确度，计算量大。",
    "无位置传感器控制":
        "用观测器（SMO/EKF/MRAS/HFI）估算转子位置替代物理传感器，省成本、"
        "高可靠；低速性能取决于所选方法（反电动势法低速失效，HFI 可到零速）。",
    "电流斩波控制(CCC)":
        "电流滞环斩波限幅，双凸极电机低速大转矩的常用方式；转矩脉动较大，"
        "斩波频率不固定。",
    "角度位置控制(APC)":
        "按转子位置角控制各相开通/关断角，双凸极电机中高速区的主流方式；"
        "开通/关断角整定直接影响效率与转矩脉动。",
    "电压PWM控制":
        "直接调节 PWM 占空比控制绕组平均电压，实现简单、响应直接；"
        "无电流闭环保护，注意限流。",
}


class ControlPage(QWidget):
    def __init__(self, comm: CommManager,
                 state_machine: RuntimeStateMachine | None = None) -> None:
        super().__init__()
        self._comm = comm
        self._state_machine = state_machine
        self._current_sensor_name = POSITION_SENSORS[1]
        self._start_command_pending = False
        self._start_retry_not_before = 0.0
        self._simulation_snapshot_provider = None

        # 为所有可能的控制方式各建一份控制器和面板（懒加载亦可，这里为简洁全建）
        self._controllers = {name: cls() for name, (cls, _) in _MODE_REGISTRY.items()}
        self._panels = {name: panel_cls() for name, (_, panel_cls) in _MODE_REGISTRY.items()}
        self._sensor_panels = {name: panel_cls() for name, panel_cls in _SENSOR_PANEL_CLS.items()}

        root = QVBoxLayout(self)

        title = QLabel("电机控制")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        top = QHBoxLayout()
        top.addWidget(self._build_motor_box(), 1)
        top.addWidget(self._build_sensor_box(), 1)
        top.addWidget(self._build_mode_box(), 1)
        root.addLayout(top)

        root.addWidget(self._build_param_box(), 1)
        root.addLayout(self._build_buttons())

        comm.telemetryReceived.connect(self._on_telemetry)
        comm.commandResult.connect(self._on_command_result)
        comm.statusChanged.connect(self._on_comm_status_changed)

        # 顶部“电流限幅”与 PI 面板 iq_max 是同一物理量，保持双向同步。
        self._syncing_iq_limit = False
        self._wire_iq_limit_sync()

        # 初始按当前电机类型刷新控制方式列表
        self._refresh_modes_for_motor()

    # ─── 子构件 ──────────────────────────────────────────────
    def _build_motor_box(self) -> QGroupBox:
        box = QGroupBox("电机信息")
        f = QFormLayout(box)
        saved = load_motor_info()
        self._motor_type = QComboBox()
        self._motor_type.addItems(MOTOR_TYPES)
        self._motor_type.currentIndexChanged.connect(self._on_motor_type_changed)
        self._motor_model = QLineEdit(saved.get("model", "野火 78W PMSM"))
        self._pole_pairs = QSpinBox(); self._pole_pairs.setRange(1, 64)
        self._pole_pairs.setValue(int(saved.get("pole_pairs", 4)))
        self._max_rpm = QSpinBox(); self._max_rpm.setRange(1, 4000)
        # 野火 78 W PMSM 额定最高转速为 4000 rpm；若档案已保存上限则优先使用。
        self._max_rpm.setValue(int(saved.get("max_rpm", 4000)))
        self._max_rpm.setToolTip(
            "允许范围：1～4000 rpm；不是建议长期运行转速")
        self._current_limit = QDoubleSpinBox()
        # 恢复实验基线：3000 digit ≈ 1.887 A（NOMINAL 上限约 4.49 A）。
        self._current_limit.setRange(0.1, 4.49)
        self._current_limit.setDecimals(3)
        self._current_limit.setSuffix(" A")
        saved_current = float(saved.get("rated", {}).get("current_A") or 1.887)
        self._current_limit.setValue(
            saved_current if 0.1 <= saved_current <= 4.49 else 1.887)
        self._current_limit.setToolTip(
            "限制速度环输出的q轴电流给定；真机仍必须由下位机和硬件独立限流。")
        self._rated_temperature = QDoubleSpinBox()
        self._rated_temperature.setRange(0.0, 250.0)
        self._rated_temperature.setDecimals(1)
        self._rated_temperature.setSuffix(" °C")
        self._rated_temperature.setSpecialValueText("未设置")
        self._rated_temperature.setValue(float(
            saved.get("rated", {}).get("temperature_C", 0.0)))

        f.addRow("电机类型", self._motor_type)
        f.addRow("电机型号", self._motor_model)
        f.addRow("极对数", _field_with_hint(self._pole_pairs, "1 ～ 64"))
        f.addRow("最高转速", _field_with_hint(self._max_rpm, "1 ～ 4000 rpm"))
        f.addRow("电流限幅", _field_with_hint(self._current_limit, "0.1 ～ 4.49 A"))
        self._device_limits = QLabel("下位机回读：等待遥测")
        self._device_limits.setWordWrap(True)
        self._device_limits.setToolTip("下位机实际采用的最高转速、Iq限流和动态跑飞阈值")
        f.addRow("保护回读", self._device_limits)
        f.addRow("额定工作点温度",
                 _field_with_hint(self._rated_temperature, "0 ～ 250 °C"))

        btn_detail = QPushButton("电机详情（额定/实测/描述）…")
        btn_detail.clicked.connect(self._on_motor_detail)
        f.addRow("", btn_detail)
        return box

    def _on_sensor_detail(self) -> None:
        name = self._current_sensor_name or POSITION_SENSORS[1]
        SensorDetailDialog(name, self._comm, self).exec()

    def _on_motor_detail(self) -> None:
        dlg = MotorInfoDialog(self._comm, self)
        if dlg.exec():
            info = load_motor_info()
            if info.get("model"):
                self._motor_model.setText(info["model"])
            if info.get("pole_pairs"):
                self._pole_pairs.setValue(int(info["pole_pairs"]))
            rated = info.get("rated", {})
            if rated.get("current_A"):
                self._current_limit.setValue(float(rated["current_A"]))
            self._rated_temperature.setValue(float(rated.get("temperature_C", 0.0)))

    def _build_sensor_box(self) -> QGroupBox:
        """位置传感器树形列表：有传感器 / 无位置传感器 两组。"""
        box = QGroupBox("位置传感器")
        v = QVBoxLayout(box)
        self._sensor_tree = QTreeWidget()
        self._sensor_tree.setHeaderHidden(True)
        self._sensor_tree.setSelectionMode(QTreeWidget.SingleSelection)

        _WITH = ["霍尔传感器(Hall)", "增量式编码器(QEP)", "旋转变压器(Resolver)"]
        _SENSORLESS = [
            "无位置传感器-滑模观测器(SMO)",
            "无位置传感器-扩展卡尔曼(EKF)",
            "无位置传感器-模型参考自适应(MRAS)",
            "无位置传感器-高频注入(HFI)",
        ]
        grp_with = QTreeWidgetItem(self._sensor_tree, ["有位置传感器"])
        grp_with.setFlags(grp_with.flags() & ~Qt.ItemIsSelectable)
        for name in _WITH:
            QTreeWidgetItem(grp_with, [name])
        grp_sl = QTreeWidgetItem(self._sensor_tree, ["无位置传感器"])
        grp_sl.setFlags(grp_sl.flags() & ~Qt.ItemIsSelectable)
        for name in _SENSORLESS:
            QTreeWidgetItem(grp_sl, [name])
        self._sensor_tree.expandAll()
        # 默认选 QEP
        self._sensor_tree.setCurrentItem(grp_with.child(1))
        v.addWidget(self._sensor_tree)

        self._sensor_status = QLabel("已选：增量式编码器(QEP)")
        v.addWidget(self._sensor_status)

        btn_sensor_detail = QPushButton("传感器详情 / 自检…")
        btn_sensor_detail.clicked.connect(self._on_sensor_detail)
        v.addWidget(btn_sensor_detail)

        self._sensor_param_stack = QStackedWidget()
        self._sensor_panel_index: dict[str, int] = {}
        for name in POSITION_SENSORS:
            idx = self._sensor_param_stack.addWidget(self._sensor_panels[name])
            self._sensor_panel_index[name] = idx
        self._sensor_param_stack.setCurrentIndex(self._sensor_panel_index[POSITION_SENSORS[1]])
        v.addWidget(self._sensor_param_stack)

        self._sensor_tree.currentItemChanged.connect(self._on_sensor_changed)
        meta = SENSOR_REGISTRY.get(POSITION_SENSORS[1])
        if meta is not None:
            self._comm.set_active_sensor(meta["sensor_id"], POSITION_SENSORS[1])
        return box

    def _build_mode_box(self) -> QGroupBox:
        box = QGroupBox("控制方式")
        v = QVBoxLayout(box)
        self._mode_combo = QComboBox()
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        v.addWidget(self._mode_combo)

        self._mode_desc = QLabel("")
        self._mode_desc.setWordWrap(True)
        self._mode_desc.setStyleSheet("color: #8fa3b8;")
        v.addWidget(self._mode_desc)

        # 目标转速统一在监控页设置；监控页构造时会把它的转速框注入进来
        self._target_speed = QSpinBox()
        self._target_speed.setRange(-4000, 4000)
        self._target_speed.setValue(1000)
        self._target_position = QDoubleSpinBox()
        self._target_position.setRange(-36000.0, 36000.0)
        self._target_position.setDecimals(2)
        self._target_position.setSingleStep(1.0)
        self._target_position.setValue(90.0)
        self._target_position.setSuffix(" °")
        self._target_position.setToolTip(
            "相对启动时捕获点的连续机械角；允许 ±100 圈。"
            "大角度实验仍应从低限速、低加速度开始。")
        self._target_position_label = QLabel("位置目标（仅位置三环）")
        self._target_position_field = _field_with_hint(
            self._target_position, "−36000° ～ +36000°（±100 圈）")
        v.addWidget(self._target_position_label)
        v.addWidget(self._target_position_field)
        self._set_position_target_visible(False)

        filter_box = QGroupBox("固件 Id/Iq 反馈滤波")
        filter_form = QFormLayout(filter_box)
        self._current_filter_mode = QComboBox()
        self._current_filter_mode.addItem("旁路（原始反馈）", False)
        self._current_filter_mode.addItem("一阶 IIR（参与电流 PI）", True)
        self._current_filter_mode.currentIndexChanged.connect(
            self._on_current_filter_mode_changed)
        self._current_filter_cutoff = QSpinBox()
        self._current_filter_cutoff.setRange(1000, 4000)
        self._current_filter_cutoff.setSingleStep(100)
        self._current_filter_cutoff.setValue(2500)
        self._current_filter_cutoff.setSuffix(" Hz")
        self._current_filter_cutoff.setToolTip(
            "16 kHz电流环的一阶低通截止频率。越低降噪越强，"
            "但相位滞后越大；运行中禁止修改。")
        self._current_filter_status = QLabel("固件回读：等待遥测")
        self._current_filter_status.setWordWrap(True)
        self._current_filter_status.setStyleSheet("color:#90a4ae;")
        self._btn_apply_current_filter = QPushButton("单独下发滤波设置")
        self._btn_apply_current_filter.clicked.connect(
            self._on_apply_current_filter)
        filter_form.addRow("方式", self._current_filter_mode)
        filter_form.addRow(
            "截止频率",
            _field_with_hint(self._current_filter_cutoff, "1 ～ 4 kHz"))
        filter_form.addRow("实际状态", self._current_filter_status)
        filter_form.addRow("", self._btn_apply_current_filter)
        v.addWidget(filter_box)
        self._on_current_filter_mode_changed()

        speed_fifo_box = QGroupBox("编码器速度反馈 FIFO")
        speed_fifo_form = QFormLayout(speed_fifo_box)
        self._speed_fifo_depth = QComboBox()
        for depth in (1, 2, 4, 8, 16):
            delay_ms = (depth - 1) / 2.0 / 500.0 * 1000.0
            self._speed_fifo_depth.addItem(
                f"{depth} 点（约 {delay_ms:g} ms 群延迟）", depth)
        self._speed_fifo_depth.setCurrentIndex(
            self._speed_fifo_depth.findData(16))
        self._speed_fifo_depth.setToolTip(
            "MCSDK编码器速度在500 Hz下的滑动平均窗口。窗口越小延迟越低，"
            "但编码器量化噪声越明显；只允许停机时修改。")
        self._speed_fifo_status = QLabel("固件回读：等待遥测")
        self._speed_fifo_status.setWordWrap(True)
        self._speed_fifo_status.setStyleSheet("color:#90a4ae;")
        self._btn_apply_speed_fifo = QPushButton("单独下发 FIFO 设置")
        self._btn_apply_speed_fifo.clicked.connect(self._on_apply_speed_fifo)
        speed_fifo_form.addRow("平均窗口", self._speed_fifo_depth)
        speed_fifo_form.addRow("实际状态", self._speed_fifo_status)
        speed_fifo_form.addRow("", self._btn_apply_speed_fifo)
        v.addWidget(speed_fifo_box)

        v.addStretch(1)
        return box

    def _on_current_filter_mode_changed(self, _index: int = -1) -> None:
        enabled = bool(self._current_filter_mode.currentData())
        self._current_filter_cutoff.setEnabled(enabled)
        cutoff = float(self._current_filter_cutoff.value())
        alpha = current_filter_alpha_q15(cutoff)
        self._current_filter_mode.setToolTip(
            (f"IIR将参与Id/Iq电流PI；当前α={alpha}/32768"
             if enabled else
             "完全旁路：Id/Iq原始反馈直接进入电流PI"))

    def _current_filter_runtime_values(self) -> dict:
        return {
            "current_filter_enabled": (
                1 if bool(self._current_filter_mode.currentData()) else 0),
            "current_filter_alpha_q15": current_filter_alpha_q15(
                self._current_filter_cutoff.value()),
        }

    def _on_apply_current_filter(self) -> None:
        if not self._comm.is_connected():
            QMessageBox.warning(self, "无法下发", "请先连接真实F407控制器。")
            return
        if int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6:
            QMessageBox.warning(
                self, "运行中禁止修改",
                "请先停机再切换旁路或调整截止频率，避免闭环相位发生突变。")
            return
        values = self._current_filter_runtime_values()
        payload = firmware_runtime_payload(values)
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(CMD_SET_PARAMS, payload))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        submitted = sent or pending_after > pending_before
        self._current_filter_status.setText(
            "设置已提交，等待固件遥测回读"
            if submitted else "未发送：请检查v2连接与固件能力")
        logger.log(
            "下发电流反馈滤波",
            f"enabled={values['current_filter_enabled']} "
            f"alpha_q15={values['current_filter_alpha_q15']} "
            f"cutoff={self._current_filter_cutoff.value()}Hz")

    def _speed_fifo_runtime_values(self) -> dict:
        return {"speed_fifo_depth": int(self._speed_fifo_depth.currentData())}

    def _on_apply_speed_fifo(self) -> None:
        if not self._comm.is_connected():
            QMessageBox.warning(self, "无法下发", "请先连接真实F407控制器。")
            return
        if int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6:
            QMessageBox.warning(
                self, "运行中禁止修改",
                "请先停机再修改编码器速度FIFO，避免速度反馈瞬间跳变。")
            return
        values = self._speed_fifo_runtime_values()
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(
            CMD_SET_PARAMS, firmware_runtime_payload(values)))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        submitted = sent or pending_after > pending_before
        self._speed_fifo_status.setText(
            "设置已提交，等待固件遥测回读"
            if submitted else "未发送：请检查v2连接与固件能力")
        logger.log("下发编码器速度FIFO",
                   f"depth={values['speed_fifo_depth']}")

    def is_sim_running(self) -> bool:
        return self._comm.is_sim_running()

    def _build_param_box(self) -> QGroupBox:
        box = QGroupBox("控制参数调整")
        v = QVBoxLayout(box)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("控制参数方案"))
        self._pi_profile_combo = QComboBox()
        self._pi_profile_combo.setToolTip(
            "选择后点击“加载到界面”；加载不会自动发送，确认数值后再发送到下位机。")
        profile_row.addWidget(self._pi_profile_combo, 1)
        self._btn_profile_load = QPushButton("加载到界面")
        self._btn_profile_save = QPushButton("保存当前方案")
        self._btn_profile_delete = QPushButton("删除方案")
        self._btn_profile_load.clicked.connect(self._on_load_pi_profile)
        self._btn_profile_save.clicked.connect(self._on_save_pi_profile)
        self._btn_profile_delete.clicked.connect(self._on_delete_pi_profile)
        profile_row.addWidget(self._btn_profile_load)
        profile_row.addWidget(self._btn_profile_save)
        profile_row.addWidget(self._btn_profile_delete)
        v.addLayout(profile_row)
        self._reload_pi_profiles()
        self._stack = QStackedWidget()
        # 把所有面板按固定顺序加入栈，记录索引
        self._panel_index: dict[str, int] = {}
        for name, panel in self._panels.items():
            self._panel_index[name] = self._stack.addWidget(panel)
        v.addWidget(self._stack)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        h = QHBoxLayout()
        self._btn_apply = QPushButton("发送当前参数到下位机")
        self._btn_apply.setToolTip("把当前界面参数通过v2协议发送；收到ACK后下位机立即采用")
        self._btn_apply.clicked.connect(self._on_apply)
        h.addWidget(self._btn_apply)
        h.addStretch(1)
        return h

    @staticmethod
    def _builtin_pi_profile() -> dict:
        return {
            "profile_version": 2,
            "control_mode": "闭环PI控制",
            "kp_spd": 1752.0, "ki_spd": 121.0,
            "kp_cur": 2323.0, "ki_cur": 2077.0,
            "iq_max": 1.887, "max_current_a": 1.887,
            "max_rpm": 4000,
            "current_filter_enabled": 0,
            "current_filter_cutoff_hz": 2500,
            "speed_fifo_depth": 16,
            "description": "当前实验平台稳定基线",
        }

    def _read_pi_profiles(self) -> dict:
        path = writable_path(*_PI_PROFILE_FILE.split("/"))
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_pi_profiles(self, profiles: dict) -> None:
        path = writable_path(*_PI_PROFILE_FILE.split("/"))
        path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def _reload_pi_profiles(self, selected: str = "") -> None:
        profiles = self._read_pi_profiles()
        self._pi_profile_combo.clear()
        self._pi_profile_combo.addItem(_BUILTIN_PI_PROFILE,
                                       self._builtin_pi_profile())
        for name, values in profiles.items():
            self._pi_profile_combo.addItem(name, values)
        if selected:
            index = self._pi_profile_combo.findText(selected)
            if index >= 0:
                self._pi_profile_combo.setCurrentIndex(index)

    def _on_save_pi_profile(self) -> None:
        mode = self._current_mode()
        panel = self._panels[mode]
        name, ok = QInputDialog.getText(
            self, "保存控制参数方案", "方案名称：",
            text=datetime.now().strftime("控制方案 %Y-%m-%d %H-%M"))
        name = name.strip()
        if not ok or not name:
            return
        if name == _BUILTIN_PI_PROFILE:
            QMessageBox.warning(self, "名称不可用", "内置基线方案不可覆盖。")
            return
        profiles = self._read_pi_profiles()
        if name in profiles and QMessageBox.question(
                self, "覆盖方案", f"“{name}”已存在，是否覆盖？") != QMessageBox.Yes:
            return
        values = dict(panel.values())
        profiles[name] = {
            **values,
            **self._current_filter_runtime_values(),
            **self._speed_fifo_runtime_values(),
            "current_filter_cutoff_hz": self._current_filter_cutoff.value(),
            "profile_version": 2,
            "control_mode": mode,
            "target_speed_rpm": self._target_speed.value(),
            "target_position_deg": self._target_position.value(),
            "max_current_a": self._current_limit.value(),
            "max_rpm": self._max_rpm.value(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_pi_profiles(profiles)
        self._reload_pi_profiles(name)
        logger.log("保存控制参数方案",
                   f"方案={name} 模式={mode} 参数={profiles[name]}")
        QMessageBox.information(
            self, "已保存", "方案已保存在本机。\n尚未发送到下位机。")

    @staticmethod
    def _apply_profile_to_panel(panel: QWidget, values: dict) -> list[str]:
        """把档案中的参数恢复到对应面板，返回实际恢复的参数键。"""
        loaded = []
        redundant_aliases = {"kp", "ki", "kd", "sample_time"}
        for key, value in values.items():
            if key in redundant_aliases:
                continue
            attr = _PROFILE_WIDGET_ALIASES.get(key, key)
            widget = getattr(panel, attr, None)
            if widget is None:
                continue
            try:
                if isinstance(widget, QComboBox):
                    index = widget.findText(str(value))
                    if index < 0:
                        continue
                    widget.setCurrentIndex(index)
                elif hasattr(widget, "setValue"):
                    widget.setValue(float(value))
                else:
                    continue
            except (TypeError, ValueError):
                continue
            loaded.append(key)
        return loaded

    def _on_load_pi_profile(self) -> None:
        values = self._pi_profile_combo.currentData()
        if not isinstance(values, dict):
            return
        saved_mode = values.get("control_mode")
        # 旧版方案未记录控制方式：保持用户当前页面，绝不再强制跳到闭环 PI。
        mode = saved_mode if saved_mode in self._panels else self._current_mode()
        self._select_mode(mode)
        panel = self._panels[mode]
        loaded = self._apply_profile_to_panel(panel, values)
        # iq_max / max_current_a / 顶部电流限幅三者统一
        if "max_current_a" in values:
            self._current_limit.setValue(float(values["max_current_a"]))
        elif "iq_max" in values:
            self._current_limit.setValue(float(values["iq_max"]))
        if hasattr(panel, "iq_max"):
            panel.iq_max.setValue(self._current_limit.value())
        if "max_rpm" in values:
            self._max_rpm.setValue(int(values["max_rpm"]))
        if "target_speed_rpm" in values:
            self._target_speed.setValue(int(values["target_speed_rpm"]))
        if "target_position_deg" in values:
            self._target_position.setValue(float(values["target_position_deg"]))
        if "current_filter_enabled" in values:
            index = self._current_filter_mode.findData(
                bool(int(values["current_filter_enabled"])))
            if index >= 0:
                self._current_filter_mode.setCurrentIndex(index)
        if "current_filter_cutoff_hz" in values:
            self._current_filter_cutoff.setValue(
                int(values["current_filter_cutoff_hz"]))
        if "speed_fifo_depth" in values:
            index = self._speed_fifo_depth.findData(
                int(values["speed_fifo_depth"]))
            if index >= 0:
                self._speed_fifo_depth.setCurrentIndex(index)
        logger.log(
            "加载控制参数方案",
            f"方案={self._pi_profile_combo.currentText()} 模式={mode} "
            f"恢复参数={','.join(loaded) or '无'}")
        QMessageBox.information(
            self, "已加载到界面",
            f"参数已填入“{mode}”页面，但尚未发送。\n"
            "核对后请点击“发送当前参数到下位机”。")

    def _on_delete_pi_profile(self) -> None:
        name = self._pi_profile_combo.currentText()
        if name == _BUILTIN_PI_PROFILE:
            QMessageBox.warning(self, "不可删除", "内置稳定基线不可删除。")
            return
        profiles = self._read_pi_profiles()
        if name not in profiles:
            return
        if QMessageBox.question(
                self, "删除控制参数方案",
                f"确定删除“{name}”吗？") != QMessageBox.Yes:
            return
        del profiles[name]
        self._write_pi_profiles(profiles)
        self._reload_pi_profiles()
        logger.log("删除PI参数方案", name)

    # ─── slots ──────────────────────────────────────────────
    def _refresh_modes_for_motor(self) -> None:
        primary = self._current_sensor_name
        self._refresh_modes_for_sensor(primary)

    def _refresh_modes_for_sensor(self, sensor_name: str) -> None:
        """按当前电机类型 + 主传感器联合过滤控制方式。"""
        motor = self._motor_type.currentText()
        base_modes = list(CONTROL_MODES_BY_MOTOR.get(motor, []))
        meta = SENSOR_REGISTRY.get(sensor_name) if sensor_name else None
        allowed = (meta or {}).get("allowed_modes") or []
        warn = set((meta or {}).get("warn_modes") or [])

        if allowed:
            filtered = [m for m in base_modes if m in allowed]
            fallback_warn = not filtered
            if fallback_warn:
                filtered = base_modes
        else:
            filtered = base_modes
            fallback_warn = False

        prev = self._current_mode()
        self._mode_combo.blockSignals(True)
        self._mode_combo.clear()
        for m in filtered:
            label = m + _WARN_SUFFIX if m in warn or (fallback_warn and allowed and m not in allowed) else m
            self._mode_combo.addItem(label)
        self._mode_combo.blockSignals(False)
        if not filtered:
            return
        # 保留之前选中的模式（如果还在列表里）
        target = prev if prev in filtered else filtered[0]
        for i in range(self._mode_combo.count()):
            if self._mode_text_to_mode(self._mode_combo.itemText(i)) == target:
                self._mode_combo.setCurrentIndex(i)
                break
        self._stack.setCurrentIndex(self._panel_index[target])
        self._update_mode_desc()
        self._set_position_target_visible(target == "位置三环控制")

    def _on_motor_type_changed(self, _idx: int) -> None:
        self._refresh_modes_for_motor()
        logger.log("切换电机类型", self._motor_type.currentText())

    def _on_mode_changed(self, _idx: int) -> None:
        mode = self._current_mode()
        if mode in self._panel_index:
            self._stack.setCurrentIndex(self._panel_index[mode])
        self._update_mode_desc()
        self._set_position_target_visible(mode == "位置三环控制")

    def _set_position_target_visible(self, visible: bool) -> None:
        """位置目标的标题、输入框和范围提示始终一起显示/隐藏。"""
        if hasattr(self, "_target_position_label"):
            self._target_position_label.setVisible(visible)
        if hasattr(self, "_target_position_field"):
            self._target_position_field.setVisible(visible)

    def _update_mode_desc(self) -> None:
        if not hasattr(self, "_mode_desc"):
            return
        mode = self._current_mode()
        desc = _MODE_DESCRIPTIONS.get(mode, "")
        if self._mode_combo.currentText().endswith(_WARN_SUFFIX):
            desc += "\n⚠ 当前传感器与该方式匹配度低（分辨率/带宽受限），谨慎使用。"
        self._mode_desc.setText(desc)

    def _on_sensor_changed(self, _curr: QTreeWidgetItem, _prev: QTreeWidgetItem) -> None:
        sensors = self._selected_sensors()
        text = sensors[0] if sensors else "未选择"
        self._sensor_status.setText(f"已选：{text}")

        primary = self._primary_sensor(sensors)
        self._current_sensor_name = primary
        if primary:
            self._sensor_param_stack.setCurrentIndex(self._sensor_panel_index[primary])
            meta = SENSOR_REGISTRY.get(primary, {})
            self._comm.set_active_sensor(meta.get("sensor_id", 0), primary)
            sm = meta.get("sensorless_method")
            if sm:
                self._refresh_modes_for_sensor(primary)
                self._select_mode("无位置传感器控制")
                panel = self._panels.get("无位置传感器控制")
                if panel is not None and hasattr(panel, "method"):
                    idx = panel.method.findText(sm)
                    if idx >= 0:
                        panel.method.setCurrentIndex(idx)
                return
        self._refresh_modes_for_sensor(primary)

    @staticmethod
    def _primary_sensor(sensors: list[str]) -> str:
        """多选时挑一个作为主传感器：优先有传感器（Hall/QEP/Resolver）。"""
        if not sensors:
            return ""
        for s in sensors:
            if not s.startswith("无位置传感器"):
                return s
        return sensors[-1]

    @staticmethod
    def _mode_text_to_mode(text: str) -> str:
        if text.endswith(_WARN_SUFFIX):
            return text[: -len(_WARN_SUFFIX)]
        return text

    def _select_mode(self, mode: str) -> None:
        for i in range(self._mode_combo.count()):
            if self._mode_text_to_mode(self._mode_combo.itemText(i)) == mode:
                self._mode_combo.setCurrentIndex(i)
                self._stack.setCurrentIndex(self._panel_index[mode])
                return

    def _selected_sensors(self) -> list[str]:
        item = self._sensor_tree.currentItem()
        if item is None or item.childCount() > 0:
            return []
        return [item.text(0)]

    def _current_mode(self) -> str:
        return self._mode_text_to_mode(self._mode_combo.currentText())

    def set_simulation_snapshot_provider(self, provider) -> None:
        """注入独立数字孪生页的只读归档接口，避免控制页持有仿真控件。"""
        self._simulation_snapshot_provider = provider

    def current_loop_analysis_snapshot(self) -> dict:
        """供波特图页读取当前UI增益和固件滤波回读，不改变控制状态。"""
        mode = self._current_mode()
        mode_params = (
            dict(self._panels[mode].values()) if mode in self._panels else {})
        frame = self._comm.latest_frame()
        real_feedback = getattr(frame, "data_source", "") == "real"
        filter_enabled = (
            bool(getattr(frame, "current_filter_enabled", False))
            if real_feedback else
            bool(self._current_filter_mode.currentData()))
        filter_alpha_q15 = (
            int(getattr(frame, "current_filter_alpha_q15", 0) or 0)
            if real_feedback else
            current_filter_alpha_q15(self._current_filter_cutoff.value()))
        if filter_alpha_q15 <= 0:
            filter_alpha_q15 = current_filter_alpha_q15(
                self._current_filter_cutoff.value())
        return {
            "kp_cur_digit": int(round(float(mode_params.get("kp_cur", 2323)))),
            "ki_cur_digit": int(round(float(mode_params.get("ki_cur", 2077)))),
            "sample_rate_hz": int(_CURRENT_LOOP_SAMPLE_RATE_HZ),
            "vbus_v": float(getattr(frame, "vdc", 0.0) or 24.0),
            "filter_enabled": filter_enabled,
            "filter_alpha_q15": filter_alpha_q15,
            "source": "固件遥测+当前控制页" if real_feedback else "当前控制页默认值",
        }

    def experiment_snapshot(self) -> dict:
        """导出当前控制配置的只读实验快照，不下发参数也不改变 UI 状态。"""
        motor_info = load_motor_info()
        rated = dict(motor_info.get("rated", {}))
        measured = dict(motor_info.get("measured", {}))
        mode = self._current_mode()
        mode_params = dict(self._panels[mode].values()) if mode in self._panels else {}
        sensors = self._selected_sensors()
        sensor_params = {
            name: dict(self._sensor_panels[name].values())
            for name in sensors if name in self._sensor_panels
        }
        mechanical = (
            dict(self._simulation_snapshot_provider())
            if callable(self._simulation_snapshot_provider) else {})
        protection_keys = {
            "iq_max", "current_upper", "current_limit", "voltage_limit",
            "u_min", "u_max", "delta_u_max", "x_min", "x_max",
        }
        protection = {key: value for key, value in mode_params.items()
                      if key in protection_keys}
        protection["max_rpm"] = self._max_rpm.value()
        protection["max_current_a"] = self._current_limit.value()
        return {
            "device": {
                "name": self._motor_model.text().strip() or "未命名电机",
                "motor_type": self._motor_type.currentText(),
                "rated_power_w": rated.get("power_W") or None,
                "sensors": sensors,
                "extra": {
                    "model": self._motor_model.text().strip(),
                    "pole_pairs": self._pole_pairs.value(),
                    "max_rpm": self._max_rpm.value(),
                    "current_limit_a": self._current_limit.value(),
                    "rated_operating_temperature_c": (
                        self._rated_temperature.value() or None),
                    "rated": rated,
                    "measured": measured,
                    "description": motor_info.get("description", ""),
                },
            },
            "controller_params": {
                "motor_type": self._motor_type.currentText(),
                "control_mode": mode,
                "target_speed_rpm": self._target_speed.value(),
                "target_position_deg": self._target_position.value(),
                "mode_params": mode_params,
                "current_feedback_filter": {
                    **self._current_filter_runtime_values(),
                    "cutoff_hz": self._current_filter_cutoff.value(),
                    "sample_rate_hz": int(_CURRENT_LOOP_SAMPLE_RATE_HZ),
                },
                "speed_feedback_fifo": {
                    **self._speed_fifo_runtime_values(),
                    "sample_rate_hz": 500,
                    "group_delay_ms": (
                        (self._speed_fifo_runtime_values()["speed_fifo_depth"] - 1)
                        / 2.0 / 500.0 * 1000.0),
                },
                "sensor_params": sensor_params,
                "mechanical_load": mechanical,
            },
            "protection_params": protection,
        }

    def _wire_iq_limit_sync(self) -> None:
        """电流限幅 ↔ 转速环 iq_max 双向同步，避免界面上出现两个互相打架的上限。"""
        panels = [panel for panel in self._panels.values()
                  if hasattr(panel, "iq_max")]
        if not panels:
            return
        # 以顶部电流限幅为初始权威值，覆盖各级联面板旧默认值。
        for panel in panels:
            panel.iq_max.setValue(self._current_limit.value())
            panel.iq_max.valueChanged.connect(self._on_pi_iq_max_changed)
        self._current_limit.valueChanged.connect(self._on_top_current_limit_changed)

    def _on_top_current_limit_changed(self, value: float) -> None:
        if self._syncing_iq_limit:
            return
        self._syncing_iq_limit = True
        try:
            for panel in self._panels.values():
                if hasattr(panel, "iq_max"):
                    panel.iq_max.setValue(float(value))
        finally:
            self._syncing_iq_limit = False

    def _on_pi_iq_max_changed(self, value: float) -> None:
        if self._syncing_iq_limit:
            return
        self._syncing_iq_limit = True
        try:
            self._current_limit.setValue(float(value))
        finally:
            self._syncing_iq_limit = False

    def _on_apply(self) -> None:
        if self._comm.is_sim_running() and not self._comm.is_connected():
            QMessageBox.information(
                self, "固件参数与仿真已分离",
                "当前页面只发送 F407 固件参数。数字孪生采用独立的控制结构和参数单位，"
                "请在“数字孪生”页面设置仿真目标、机械参数和负载。")
            return
        mode = self._current_mode()
        if not mode:
            return
        params = dict(self._panels[mode].values())
        # PI 面板 iq_max 与顶部电流限幅统一后再下发，固件只认 max_current_a。
        if "iq_max" in params:
            current_limit = float(params["iq_max"])
            self._current_limit.setValue(current_limit)
            params["iq_max"] = current_limit
        else:
            current_limit = float(self._current_limit.value())
        self._comm.configure_motor_current_limit(current_limit)
        try:
            self._controllers[mode].set_params(**params)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", f"参数应用失败：{exc}")
            return
        sensors = self._selected_sensors()
        primary = self._primary_sensor(sensors)

        # 1) 先下发传感器配置帧 CMD_SET_SENSOR
        if primary:
            self._send_sensor_frame(primary)
            # 若是无传感器估算，把对应方法 + 参数透传给 SensorlessController
            sensor_meta = SENSOR_REGISTRY.get(primary, {})
            sm = sensor_meta.get("sensorless_method")
            if sm and "无位置传感器控制" in self._controllers:
                sensor_params = self._sensor_panels[primary].values()
                try:
                    self._controllers["无位置传感器控制"].set_params(method=sm, **sensor_params)
                except Exception:
                    pass

        # 2) 再下发控制方式参数帧 CMD_SET_PARAMS（只发固件认得的键）
        meta = {"motor": self._motor_type.currentText(),
                "mode": mode,
                "sensors": "|".join(sensors),
                "control_mode": (
                    "position_closed" if mode == "位置三环控制" else
                    "current_loop_test" if mode == "开环控制" else
                    "speed_closed"),
                "target": self._target_speed.value(),
                "position_target_deg": self._target_position.value()}
        filter_values = (
            {} if int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6
            else self._current_filter_runtime_values())
        fifo_values = (
            {} if int(getattr(self._comm.latest_frame(), "mc_state", 0)) == 6
            else self._speed_fifo_runtime_values())
        payload = firmware_runtime_payload({
            **params,
            **filter_values,
            **fifo_values,
            "control_mode": meta["control_mode"],
            "target": meta["target"],
            "position_target_deg": meta["position_target_deg"],
            "max_rpm": self._max_rpm.value(),
            "max_current_a": current_limit,
        })
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(CMD_SET_PARAMS, payload))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        submitted = sent or pending_after > pending_before

        logger.log("保存/应用参数",
                   f"电机={meta['motor']} 控制方式={mode} "
                   f"传感器={meta['sensors'] or '无'} 电流限幅={current_limit:.2f}A")
        downstream_note = (
            "真机已应用：位置/速度/电流PI、速度前馈、位置速度限幅、最高转速与Iq限流。"
            if self._comm.is_connected() else
            "未连接真机，参数没有发送；数字孪生参数请在独立页面设置。")
        QMessageBox.information(
            self, "已应用",
            f"电机：{meta['motor']}\n"
            f"控制方式：{mode}\n"
            f"位置传感器：{meta['sensors'] or '无'}\n"
            f"电流限幅：{current_limit:.2f} A（真机仍需硬件独立保护）\n"
            f"发送状态：{'已提交，等待ACK' if submitted else '未发送'}\n"
            f"{downstream_note}\n参数：{params}",
        )

    def _send_sensor_frame(self, sensor_name: str) -> None:
        """组装 CMD_SET_SENSOR 帧：payload[0]=sensor_id, 之后是 utf-8 文本参数。"""
        meta = SENSOR_REGISTRY.get(sensor_name, {})
        sensor_id = int(meta.get("sensor_id", 0)) & 0xFF
        sensor_params = self._sensor_panels[sensor_name].values()
        params_text = ";".join(f"{k}={v}" for k, v in sensor_params.items())
        payload = bytes([sensor_id]) + params_text.encode("utf-8")
        frame = encode_frame(CMD_SET_SENSOR, payload)
        can_id = int(meta.get("can_id_default", 0x100)) or 0x100
        self._comm.send_frame_with_id(frame, can_id=can_id)

    def _on_start(self) -> None:
        if self._comm.is_sim_running() and not self._comm.is_connected():
            QMessageBox.information(
                self, "请使用数字孪生工作台",
                "固件控制与数字孪生已经分离。请到“数字孪生”页面运行仿真模型。")
            return
        now = time.monotonic()
        if self._start_command_pending:
            logger.log("忽略重复启动", "上一条START仍在等待设备ACK")
            return
        if now < self._start_retry_not_before:
            logger.log("忽略过快启动", "停机/启动应答后的安全冷却尚未结束")
            return
        if not self._comm.is_connected() and not self._comm.is_sim_running():
            QMessageBox.warning(
                self, "无法启动",
                "通信未连接，请先在「通信设置」页面建立连接，或启动仿真（虚拟电机）。")
            return
        # UI 高速绘图拥塞时，STOP ACK/状态信号可能晚于设备实际停机。
        # 只在遥测同时证明“非 RUN、零目标、近零速、无故障”时修复残留状态；
        # 任一条件不满足都保持原有安全拦截。
        if (self._state_machine is not None and
                self._state_machine.state in
                (RuntimeState.RUNNING, RuntimeState.STOPPING)):
            latest = self._comm.latest_frame()
            telemetry_fresh = self._comm.telemetry_age_s() <= _TELEMETRY_FRESH_S
            device_stopped = (
                telemetry_fresh and
                int(getattr(latest, "mc_state", 0) or 0) == 0 and
                int(getattr(latest, "fault_code", 0) or 0) == 0 and
                abs(float(getattr(latest, "speed_target", 0.0) or 0.0)) < 0.5 and
                abs(float(getattr(latest, "speed_actual", 0.0) or 0.0)) < 30.0
            )
            if device_stopped:
                self._state_machine.observe_device_stopped(
                    "启动前遥测确认设备已停机，修复残留运行状态")
        if (self._state_machine is not None and
                self._state_machine.state is not RuntimeState.READY):
            QMessageBox.warning(
                self, "状态不允许启动",
                "设备尚未进入 READY。请先在「实验管理」页面执行运行预检。\n"
                f"当前状态：{self._state_machine.state.value}")
            return
        if not self._selected_sensors():
            ans = QMessageBox.question(
                self, "未选择位置传感器",
                "请先在左侧列表中选择一种位置传感器，是否仍要启动？",
            )
            if ans != QMessageBox.Yes:
                return

        # MCSDK FAULT_OVER（mc_state=11）不再拦截：固件 START 通过联锁检查后
        # 会自行 MC_AcknowledgeFaultMotor1() 清掉已消失的故障；若联锁未通过，
        # START 会收到明确 NACK，由 commandResult 路径提示。
        if getattr(self._comm.latest_frame(), "mc_state", 0) == 11:
            logger.log("启动时FAULT_OVER待确认", "由下位机START流程自动确认")

        mode = self._current_mode()
        target = float(self._target_speed.value())
        max_rpm = float(self._max_rpm.value())
        mode_params = self._panels[mode].values()
        position_target = float(self._target_position.value())
        if mode != "位置三环控制" and abs(target) > max_rpm:
            QMessageBox.warning(self, "参数越界",
                                f"目标转速 {target} rpm 超过最高转速 {max_rpm} rpm，请修改后重试。")
            return
        if mode == "位置三环控制":
            speed_limit = float(mode_params.get("position_speed_limit_rpm", 0.0))
            if speed_limit <= 0.0 or speed_limit > max_rpm:
                QMessageBox.warning(
                    self, "位置环限幅无效",
                    f"位置环速度限幅必须在 1..{max_rpm:.0f} rpm 内。")
                return
        if mode == "位置三环控制":
            start_parts = [
                "control_mode=position_closed",
                f"position_target_deg={position_target:.3f}",
                f"kp_pos={mode_params.get('kp_pos', 8.0):.3f}",
                f"kd_pos={mode_params.get('kd_pos', 0.20):.3f}",
                f"kpf_pos={mode_params.get('kpf_pos', 0.0):.3f}",
                f"position_ff_lpf_hz={mode_params.get('position_ff_lpf_hz', 8.0):.3f}",
                f"position_speed_limit_rpm={mode_params.get('position_speed_limit_rpm', 300.0):.1f}",
                f"position_accel_limit_rpm_s={mode_params.get('position_accel_limit_rpm_s', 60.0):.1f}",
                f"max_rpm={max_rpm:.0f}",
                f"max_current_a={self._current_limit.value():.3f}",
            ]
        else:
            start_parts = [
                f"target={target}",
                f"max_rpm={max_rpm:.0f}",
                f"max_current_a={self._current_limit.value():.3f}",
            ]
            if mode == "开环控制":
                start_parts.extend(f"{k}={v}" for k, v in mode_params.items())
            else:
                start_parts.append("control_mode=speed_closed")
        payload = ";".join(start_parts).encode("utf-8")
        logger.log("START载荷", f"{len(payload)}B {payload.decode('utf-8', 'replace')}")
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(CMD_START, payload))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        submitted_async = (
            self._comm.protocol_status().get("mode") == "negotiated-v2" and
            pending_after > pending_before
        )
        if submitted_async:
            self._start_command_pending = True
        if not sent and not submitted_async:
            return
        logger.log("启动电机命令已提交",
                   f"目标={'位置 '+str(position_target)+' deg' if mode == '位置三环控制' else str(target)+' rpm'} 控制方式={mode} "
                   f"等待设备ACK={submitted_async}")
        if self._state_machine is not None:
            # negotiated-v2 的“已提交”绝不等于“设备已启动”。真实设备只能由
            # MainWindow 收到 START ACK 后推进到 RUNNING；NACK 时必须保持 READY。
            # virtual-v2 的同步 ACK 已在 send_frame 内发出 commandResult，异步 ACK
            # 也由同一路径处理。仅 legacy/本地仿真保留本地确认。
            protocol_mode = self._comm.protocol_status().get("mode")
            device_ack_authoritative = protocol_mode in (
                "negotiated-v2", "virtual-v2")
            if (not device_ack_authoritative and
                    self._state_machine.state is RuntimeState.READY):
                try:
                    self._state_machine.confirm_started("启动命令本地发送成功")
                except TransitionError as exc:
                    QMessageBox.warning(self, "状态转换失败", str(exc))
                    return
        logger.log("启动电机",
                   f"目标={'位置 '+str(position_target)+' deg' if mode == '位置三环控制' else str(target)+' rpm'} 控制方式={mode} "
                   f"传感器={'|'.join(self._selected_sensors()) or '无'}")

    def _on_stop(self) -> None:
        # STOP is a safety action: never block transmission merely because
        # the UI state is stale, fault-locked, or reports disconnected.  The
        # communication manager remains the authority on whether bytes can
        # actually be sent.
        ready_stop = (self._state_machine is not None and
                      self._state_machine.state is RuntimeState.READY)
        running_stop = (self._state_machine is not None and
                        self._state_machine.state is RuntimeState.RUNNING)
        if running_stop:
            try:
                self._state_machine.request_stop("用户请求正常停机")
            except TransitionError as exc:
                logger.log("停止状态转换异常", str(exc))
        sent = self._comm.send_frame(encode_frame(CMD_STOP))
        # 清除尚未处理的START意图，并给功率级/MCSDK状态回落留出时间。
        self._start_command_pending = False
        self._start_retry_not_before = time.monotonic() + 2.0
        if self._state_machine is not None:
            # v2 ACK/NACK 可能已通过 commandResult 同步改变状态。
            mode = self._comm.protocol_status().get("mode")
            if (sent and (mode == "virtual-v2" or self._comm.is_sim_running()) and
                    self._state_machine.state is RuntimeState.STOPPING):
                self._state_machine.confirm_stopped("数字孪生确认停机")
            elif not sent and self._state_machine.state is RuntimeState.STOPPING:
                protocol = self._comm.protocol_status()
                if not (protocol["mode"] in ("virtual-v2", "negotiated-v2") and
                        protocol["pending_ack"] > 0):
                    self._state_machine.lock_fault("停止命令发送失败，设备状态未知")
        logger.log("停止电机")

    def _on_command_result(self, result) -> None:
        if getattr(result, "command", None) == CMD_START:
            self._start_command_pending = False
            # ACK/NACK后短暂抑制积压鼠标事件再次发START。
            self._start_retry_not_before = time.monotonic() + 1.0

    def _on_comm_status_changed(self, connected: bool, _message: str) -> None:
        if not connected:
            self._start_command_pending = False

    def _on_emergency(self) -> None:
        for c in self._controllers.values():
            c.reset()
        self._comm.send_frame(encode_frame(CMD_EMERGENCY_STOP))
        if self._state_machine is not None:
            self._state_machine.lock_fault("用户触发紧急停止")
        logger.log("紧急停止")
        QMessageBox.critical(self, "紧急停止", "已发送紧急停止指令！")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._last_speed_rpm = frame.speed_actual
        if getattr(frame, "data_source", "") == "real":
            self._device_limits.setText(
                f"最高 {frame.max_rpm:.0f} rpm｜Iq限流 {frame.current_limit_a:.3f} A｜"
                f"跑飞阈值 {frame.runaway_limit_rpm:.0f} rpm")
            alpha = int(getattr(frame, "current_filter_alpha_q15", 0) or 0)
            enabled = bool(getattr(frame, "current_filter_enabled", False))
            if alpha > 0:
                cutoff = current_filter_cutoff_hz(alpha)
                self._current_filter_status.setText(
                    (f"已启用 · Id/Iq参与PI · fc≈{cutoff:.0f} Hz · α={alpha} · "
                     "16 kHz F1 Iq显示PI实际反馈"
                     if enabled else
                     f"已旁路 · α预置={alpha}（fc≈{cutoff:.0f} Hz）"))
                self._current_filter_status.setStyleSheet(
                    "color:#69f0ae;" if enabled else "color:#90a4ae;")
            else:
                self._current_filter_status.setText(
                    "固件未回报滤波状态（可能是旧版本）")
                self._current_filter_status.setStyleSheet("color:#ffb74d;")
            fifo_depth = int(getattr(frame, "speed_fifo_depth", 0) or 0)
            if 1 <= fifo_depth <= 16:
                delay_ms = (fifo_depth - 1) / 2.0 / 500.0 * 1000.0
                self._speed_fifo_status.setText(
                    f"已应用 · {fifo_depth}点平均 · 约{delay_ms:g} ms群延迟")
                self._speed_fifo_status.setStyleSheet("color:#69f0ae;")
            else:
                self._speed_fifo_status.setText(
                    "固件未回报FIFO深度（可能是旧版本）")
                self._speed_fifo_status.setStyleSheet("color:#ffb74d;")
