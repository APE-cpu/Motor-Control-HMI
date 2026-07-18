"""电机控制页面：电机信息、位置传感器、控制方式、参数面板、控制按钮。"""
import json
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QInputDialog, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from communications.comm_manager import CommManager, TelemetryFrame
from communications.protocol import encode_frame
from core import RuntimeState, RuntimeStateMachine, TransitionError
from config.config import (
    CMD_EMERGENCY_STOP, CMD_RESET_FAULT, CMD_SET_PARAMS, CMD_SET_SENSOR,
    CMD_START, CMD_STOP,
    CONTROL_MODES_BY_MOTOR, MOTOR_TYPES, POSITION_SENSORS, SENSOR_REGISTRY,
)
from controllers.angle_position_controller import AnglePositionController
from controllers.current_chopping_controller import CurrentChoppingController
from controllers.mpc_controller import MPCController
from controllers.openloop_controller import OpenLoopController
from controllers.pi_controller import PIController
from controllers.sensorless_controller import SensorlessController
from controllers.voltage_control_controller import VoltageControlController
from pages.control_param_panels import (
    AnglePositionPanel, CurrentChoppingPanel, EKFPanel, HFIPanel, HallPanel,
    MPCPanel, MRASPanel, OpenLoopPanel, PIPanel, QEPPanel, ResolverPanel,
    SMOPanel, SensorlessPanel, VoltageControlPanel,
)
from widgets.motor_info_dialog import MotorInfoDialog, load_motor_info
from widgets.sensor_detail_dialog import SensorDetailDialog
from logs.operation_logger import logger
from runtime_paths import writable_path


_PI_PROFILE_FILE = "config/pi_parameter_profiles.json"
_BUILTIN_PI_PROFILE = "稳定基线（1752/121，2323/2077）"


# 控制方式名称 → (控制器类, 参数面板类) 映射
_MODE_REGISTRY = {
    "闭环PI控制":          (PIController,            PIPanel),
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
        self._max_rpm = QSpinBox(); self._max_rpm.setRange(1, 100000)
        # 野火 78 W PMSM 额定最高转速为 4000 rpm；若档案已保存上限则优先使用。
        self._max_rpm.setValue(int(saved.get("max_rpm", 4000)))
        self._current_limit = QDoubleSpinBox()
        # 当前野火电机参数 NOMINAL_CURRENT=7149，按下位机采样比例约4.496 A。
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
        f.addRow("极对数", self._pole_pairs)
        f.addRow("最高转速 (rpm)", self._max_rpm)
        f.addRow("电流限幅", self._current_limit)
        self._device_limits = QLabel("下位机回读：等待遥测")
        self._device_limits.setWordWrap(True)
        self._device_limits.setToolTip("下位机实际采用的最高转速、Iq限流和动态跑飞阈值")
        f.addRow("保护回读", self._device_limits)
        f.addRow("额定工作点温度", self._rated_temperature)

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
        self._target_speed.setRange(-100000, 100000)
        self._target_speed.setValue(1000)

        v.addWidget(self._build_load_box())
        v.addStretch(1)
        return box

    def _build_load_box(self) -> QGroupBox:
        """负载与机械：负载类型 + B/Tc/J，可应用到数字孪生（仿真下真实生效）。"""
        box = QGroupBox("负载与机械")
        f = QFormLayout(box)
        sp = self._comm.motor_sim_params()

        self._load_type = QComboBox()
        self._load_type.addItems([
            "空载", "恒转矩负载", "风机/泵类 (∝ω²)", "对拖系统 (可正负/回馈)"])
        self._load_type.currentIndexChanged.connect(self._on_load_type_changed)
        self._load_value = QDoubleSpinBox()
        self._load_value.setRange(-100.0, 100.0)
        self._load_value.setDecimals(3)
        self._load_value.setSingleStep(0.05)
        self._load_value.setValue(0.0)
        self._load_value.setEnabled(False)     # 默认空载
        self._load_value.setToolTip(
            "恒转矩/对拖：负载转矩 N·m（对拖可为负=助力/回馈）；\n"
            "风机泵类：额定转速下的负载系数（实际负载 ∝ 转速²）。")

        self._visc_b = self._mech_spin(sp.B, 6, 0.0005)
        self._coulomb = self._mech_spin(sp.T_coulomb, 4, 0.01)
        self._inertia = self._mech_spin(sp.J, 6, 0.0005)

        f.addRow("负载类型", self._load_type)
        f.addRow("负载转矩/系数", self._load_value)
        f.addRow("粘滞摩擦 B (N·m·s/rad)", self._visc_b)
        f.addRow("库仑摩擦 Tc (N·m)", self._coulomb)
        f.addRow("转动惯量 J (kg·m²)", self._inertia)

        btn_apply = QPushButton("应用到数字孪生")
        btn_apply.setToolTip("把负载与机械参数写入虚拟电机（仿真运行时立即生效）")
        btn_apply.clicked.connect(self._on_apply_mechanical)
        f.addRow("", btn_apply)

        # ---- 负载扰动：让运行曲线更丰富 ----
        self._disturb_amp = QDoubleSpinBox()
        self._disturb_amp.setRange(0.01, 100.0)
        self._disturb_amp.setDecimals(3)
        self._disturb_amp.setSingleStep(0.05)
        self._disturb_amp.setValue(0.3)
        self._disturb_amp.setToolTip("扰动幅值 N·m：叠加在当前负载之上的转矩变化量")
        self._disturb_dur = QDoubleSpinBox()
        self._disturb_dur.setRange(0.1, 60.0)
        self._disturb_dur.setDecimals(1)
        self._disturb_dur.setSingleStep(0.5)
        self._disturb_dur.setValue(1.0)
        self._disturb_dur.setToolTip("突加/突卸持续时间 s：到时自动撤除")
        self._disturb_period = QDoubleSpinBox()
        self._disturb_period.setRange(0.2, 60.0)
        self._disturb_period.setDecimals(1)
        self._disturb_period.setSingleStep(0.5)
        self._disturb_period.setValue(2.0)
        self._disturb_period.setToolTip("周期扰动的方波周期 s")
        f.addRow("扰动幅值 (N·m)", self._disturb_amp)

        db = QHBoxLayout()
        self._btn_pulse_add = QPushButton("突加负载")
        self._btn_pulse_add.setToolTip("在当前负载上瞬间叠加 +幅值，持续设定秒数后撤除")
        self._btn_pulse_add.clicked.connect(lambda: self._on_pulse(+1))
        self._btn_pulse_shed = QPushButton("突卸负载")
        self._btn_pulse_shed.setToolTip("瞬间叠加 −幅值（减载/助力），持续设定秒数后恢复")
        self._btn_pulse_shed.clicked.connect(lambda: self._on_pulse(-1))
        db.addWidget(self._btn_pulse_add)
        db.addWidget(self._btn_pulse_shed)
        db.addWidget(QLabel("持续(s)"))
        db.addWidget(self._disturb_dur)
        f.addRow("一次性扰动", db)

        pb = QHBoxLayout()
        self._btn_disturb = QPushButton("开启周期扰动")
        self._btn_disturb.setCheckable(True)
        self._btn_disturb.setToolTip("周期方波负载：在 ±幅值间来回突变，持续激励，曲线一直起伏")
        self._btn_disturb.toggled.connect(self._on_toggle_disturb)
        pb.addWidget(self._btn_disturb)
        pb.addWidget(QLabel("周期(s)"))
        pb.addWidget(self._disturb_period)
        f.addRow("周期扰动", pb)

        self._mech_status = QLabel("提示：仿真运行时生效；真机需外接测功机加载")
        self._mech_status.setWordWrap(True)
        self._mech_status.setStyleSheet("color: #8fa3b8;")
        f.addRow(self._mech_status)
        return box

    @staticmethod
    def _mech_spin(val: float, decimals: int, step: float) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(0.0, 1e4)
        sp.setDecimals(decimals)
        sp.setSingleStep(step)
        sp.setValue(val)
        return sp

    def _on_load_type_changed(self, idx: int) -> None:
        self._load_value.setEnabled(idx != 0)   # 空载禁用数值
        if self.is_sim_running():
            self._apply_load_to_sim()

    def is_sim_running(self) -> bool:
        return self._comm.is_sim_running()

    @staticmethod
    def _compute_load(load_type_idx: int, value: float, rpm: float) -> float:
        """按负载类型与当前转速算外部负载转矩 N·m。"""
        if load_type_idx == 0:        # 空载
            return 0.0
        if load_type_idx == 2:        # 风机/泵：∝ω²，value=1000rpm 处负载
            return value * (rpm / 1000.0) ** 2
        return value                  # 恒转矩 / 对拖（常量，对拖可负）

    def _apply_load_to_sim(self) -> None:
        if not self.is_sim_running():
            return
        load = self._compute_load(self._load_type.currentIndex(),
                                  self._load_value.value(),
                                  abs(self._latest_speed()))
        self._comm.set_sim_load(load)

    def _latest_speed(self) -> float:
        return getattr(self, "_last_speed_rpm", 0.0)

    def _on_apply_mechanical(self) -> None:
        sp = self._comm.motor_sim_params()
        sp.B = self._visc_b.value()
        sp.T_coulomb = self._coulomb.value()
        sp.J = self._inertia.value()
        self._apply_load_to_sim()
        where = "已写入数字孪生（仿真生效）" if self.is_sim_running() else \
            "已保存（仿真启动后生效；真机需外接测功机）"
        self._mech_status.setText(
            f"{self._load_type.currentText()}：负载 "
            f"{self._compute_load(self._load_type.currentIndex(), self._load_value.value(), 1000):.3f} N·m"
            f"@1000rpm，B={sp.B:.4g} Tc={sp.T_coulomb:.4g} J={sp.J:.4g}——{where}")
        logger.log("应用负载与机械",
                   f"类型={self._load_type.currentText()} 值={self._load_value.value()} "
                   f"B={sp.B} Tc={sp.T_coulomb} J={sp.J}")

    def _on_pulse(self, sign: int) -> None:
        """突加(+1)/突卸(-1)负载：一次性阶跃扰动。"""
        if not self.is_sim_running():
            self._mech_status.setText("负载扰动仅在仿真运行时有效，请先启动仿真")
            return
        amp = sign * self._disturb_amp.value()
        dur = self._disturb_dur.value()
        self._comm.pulse_sim_load(amp, dur)
        kind = "突加" if sign > 0 else "突卸"
        self._mech_status.setText(
            f"{kind}负载 {abs(amp):.3f} N·m，持续 {dur:.1f}s——观察转速跌落与恢复")
        logger.log("负载扰动", f"{kind} {amp:+.3f}N·m {dur:.1f}s")

    def _on_toggle_disturb(self, on: bool) -> None:
        """开/关周期方波负载扰动。"""
        if on and not self.is_sim_running():
            self._btn_disturb.setChecked(False)
            self._mech_status.setText("负载扰动仅在仿真运行时有效，请先启动仿真")
            return
        if on:
            amp = self._disturb_amp.value()
            period = self._disturb_period.value()
            self._comm.set_sim_load_disturbance(amp, period)
            self._btn_disturb.setText("停止周期扰动")
            self._mech_status.setText(
                f"周期扰动开启：±{amp:.3f} N·m 方波，周期 {period:.1f}s")
            logger.log("负载扰动", f"周期方波 ±{amp:.3f}N·m 周期{period:.1f}s")
        else:
            self._comm.set_sim_load_disturbance(0.0, 0.0)
            self._btn_disturb.setText("开启周期扰动")
            self._mech_status.setText("周期扰动已停止")
            logger.log("负载扰动", "周期方波停止")

    def _build_param_box(self) -> QGroupBox:
        box = QGroupBox("控制参数调整")
        v = QVBoxLayout(box)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("PI参数方案"))
        self._pi_profile_combo = QComboBox()
        self._pi_profile_combo.setToolTip(
            "选择后点击“加载到界面”；加载不会自动发送，确认数值后再发送到下位机。")
        profile_row.addWidget(self._pi_profile_combo, 1)
        self._btn_profile_load = QPushButton("加载到界面")
        self._btn_profile_save = QPushButton("保存当前PI方案")
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
            "kp_spd": 1752.0, "ki_spd": 121.0,
            "kp_cur": 2323.0, "ki_cur": 2077.0,
            "iq_max": 1.887, "max_current_a": 1.887,
            "max_rpm": 4000,
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
        panel = self._panels["闭环PI控制"]
        name, ok = QInputDialog.getText(
            self, "保存PI参数方案", "方案名称：",
            text=datetime.now().strftime("PI方案 %Y-%m-%d %H-%M"))
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
        values = panel.values()
        profiles[name] = {
            "kp_spd": values["kp_spd"], "ki_spd": values["ki_spd"],
            "kp_cur": values["kp_cur"], "ki_cur": values["ki_cur"],
            "iq_max": values["iq_max"],
            "max_current_a": self._current_limit.value(),
            "max_rpm": self._max_rpm.value(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_pi_profiles(profiles)
        self._reload_pi_profiles(name)
        logger.log("保存PI参数方案", f"方案={name} 参数={profiles[name]}")
        QMessageBox.information(
            self, "已保存", "方案已保存在本机。\n尚未发送到下位机。")

    def _on_load_pi_profile(self) -> None:
        values = self._pi_profile_combo.currentData()
        if not isinstance(values, dict):
            return
        panel = self._panels["闭环PI控制"]
        for key in ("kp_spd", "ki_spd", "kp_cur", "ki_cur", "iq_max"):
            widget = getattr(panel, key, None)
            if widget is not None and key in values:
                widget.setValue(float(values[key]))
        if "max_current_a" in values:
            self._current_limit.setValue(float(values["max_current_a"]))
        if "max_rpm" in values:
            self._max_rpm.setValue(int(values["max_rpm"]))
        self._select_mode("闭环PI控制")
        logger.log("加载PI参数方案", self._pi_profile_combo.currentText())
        QMessageBox.information(
            self, "已加载到界面",
            "参数已填入闭环PI页面，但尚未发送。\n"
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
                self, "删除PI方案", f"确定删除“{name}”吗？") != QMessageBox.Yes:
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

    def _on_motor_type_changed(self, _idx: int) -> None:
        self._refresh_modes_for_motor()
        logger.log("切换电机类型", self._motor_type.currentText())

    def _on_mode_changed(self, _idx: int) -> None:
        mode = self._current_mode()
        if mode in self._panel_index:
            self._stack.setCurrentIndex(self._panel_index[mode])
        self._update_mode_desc()

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
        mechanical = {
            "load_type": self._load_type.currentText(),
            "load_value": self._load_value.value(),
            "viscous_friction_B": self._visc_b.value(),
            "coulomb_friction_Tc": self._coulomb.value(),
            "inertia_J": self._inertia.value(),
            "disturbance_amplitude_Nm": self._disturb_amp.value(),
            "disturbance_duration_s": self._disturb_dur.value(),
            "disturbance_period_s": self._disturb_period.value(),
            "periodic_disturbance_enabled": self._btn_disturb.isChecked(),
        }
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
                "mode_params": mode_params,
                "sensor_params": sensor_params,
                "mechanical_load": mechanical,
            },
            "protection_params": protection,
        }

    def _on_apply(self) -> None:
        mode = self._current_mode()
        if not mode:
            return
        params = self._panels[mode].values()
        current_limit = self._current_limit.value()
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

        # 2) 再下发控制方式参数帧 CMD_SET_PARAMS（保持原文本格式）
        meta = {"motor": self._motor_type.currentText(),
                "mode": mode,
                "sensors": "|".join(sensors)}
        payload_parts = [f"{k}={v}" for k, v in {
            **meta, **params, "max_current_a": current_limit}.items()]
        payload = ";".join(payload_parts).encode("utf-8")
        pending_before = self._comm.protocol_status().get("pending_ack", 0)
        sent = self._comm.send_frame(encode_frame(CMD_SET_PARAMS, payload))
        pending_after = self._comm.protocol_status().get("pending_ack", 0)
        submitted = sent or pending_after > pending_before

        logger.log("保存/应用参数",
                   f"电机={meta['motor']} 控制方式={mode} "
                   f"传感器={meta['sensors'] or '无'} 电流限幅={current_limit:.2f}A")
        downstream_note = (
            "真机已应用：速度PI、电流PI、最高转速与Iq限流；模式和传感器参数仍未由固件解析。"
            if self._comm.is_connected() and not self._comm.is_sim_running()
            else "数字孪生已应用全部界面参数。")
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
        if not self._comm.is_connected() and not self._comm.is_sim_running():
            QMessageBox.warning(
                self, "无法启动",
                "通信未连接，请先在「通信设置」页面建立连接，或启动仿真（虚拟电机）。")
            return
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

        # MCSDK仍停留在FAULT_OVER时，START必然被拒绝。先明确发送故障复位，
        # 保持上位机READY，待下一帧确认mc_state恢复后再由用户启动。
        if getattr(self._comm.latest_frame(), "mc_state", 0) == 11:
            self._comm.send_frame(encode_frame(CMD_RESET_FAULT))
            QMessageBox.information(
                self, "正在复位故障",
                "下位机仍处于FAULT_OVER，已发送故障复位。\n"
                "请确认故障清除后再次点击启动。")
            return

        target = float(self._target_speed.value())
        max_rpm = float(self._max_rpm.value())
        if abs(target) > max_rpm:
            QMessageBox.warning(self, "参数越界",
                                f"目标转速 {target} rpm 超过最高转速 {max_rpm} rpm，请修改后重试。")
            return
        mode_params = self._panels[self._current_mode()].values()
        start_parts = [f"target={target}", f"max_rpm={max_rpm}",
                       f"max_current_a={self._current_limit.value():.3f}"]
        if self._current_mode() == "开环控制":
            start_parts.extend(f"{k}={v}" for k, v in mode_params.items())
        else:
            start_parts.append("control_mode=speed_closed")
        payload = ";".join(start_parts).encode("utf-8")
        sent = self._comm.send_frame(encode_frame(CMD_START, payload))
        if not sent:
            return
        if self._state_machine is not None:
            # v2 ACK 信号可能已在 send_frame 内同步推进状态；legacy-v1 仍在此确认。
            if self._state_machine.state is RuntimeState.READY:
                try:
                    self._state_machine.confirm_started("启动命令本地发送成功")
                except TransitionError as exc:
                    QMessageBox.warning(self, "状态转换失败", str(exc))
                    return
        logger.log("启动电机",
                   f"目标转速={target} rpm 控制方式={self._current_mode()} "
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
        if self._state_machine is not None:
            # v2 ACK/NACK 可能已通过 commandResult 同步改变状态。
            if sent and self._state_machine.state is RuntimeState.STOPPING:
                self._state_machine.confirm_stopped("停止命令本地发送成功")
            elif not sent and self._state_machine.state is RuntimeState.STOPPING:
                protocol = self._comm.protocol_status()
                if not (protocol["mode"] in ("virtual-v2", "negotiated-v2") and
                        protocol["pending_ack"] > 0):
                    self._state_machine.lock_fault("停止命令发送失败，设备状态未知")
        logger.log("停止电机")

    def _on_emergency(self) -> None:
        for c in self._controllers.values():
            c.reset()
        self._comm.send_frame(encode_frame(CMD_EMERGENCY_STOP))
        if self._state_machine is not None:
            self._state_machine.lock_fault("用户触发紧急停止")
        logger.log("紧急停止")
        QMessageBox.critical(self, "紧急停止", "已发送紧急停止指令！")

    def _on_toggle_sim(self) -> None:
        if not self._sim_running:
            if self._comm.is_connected():
                QMessageBox.warning(
                    self, "不可用",
                    "真实设备已连接。请先断开真机通信，再启动仿真。")
                return
            self._comm.start_simulation()
            self._sim_running = True
            if self._state_machine is not None:
                self._state_machine.connection_changed(True, "数字孪生已连接")
            self._btn_sim.setText("停止仿真")
            logger.log("启动仿真")
        else:
            if (self._state_machine is not None and
                    self._state_machine.state in
                    (RuntimeState.RUNNING, RuntimeState.STOPPING)):
                QMessageBox.warning(self, "不能停止仿真",
                                    "电机仍在运行，请先执行正常停机或紧急停止。")
                return
            self._comm.stop_simulation()
            self._sim_running = False
            if self._state_machine is not None:
                self._state_machine.connection_changed(False, "数字孪生已断开")
            self._btn_sim.setText("启动仿真")
            logger.log("停止仿真")

    def _on_telemetry(self, frame: TelemetryFrame) -> None:
        self._last_speed_rpm = frame.speed_actual
        if getattr(frame, "data_source", "") == "real":
            self._device_limits.setText(
                f"最高 {frame.max_rpm:.0f} rpm｜Iq限流 {frame.current_limit_a:.3f} A｜"
                f"跑飞阈值 {frame.runaway_limit_rpm:.0f} rpm")
        # 风机/泵类负载随转速平方实时变化，需逐帧刷新
        if (self.is_sim_running() and hasattr(self, "_load_type")
                and self._load_type.currentIndex() == 2):
            self._apply_load_to_sim()
