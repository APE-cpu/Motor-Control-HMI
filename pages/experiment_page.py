"""实验管理页面：创建实验、控制会话生命周期并展示归档状态。"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
    QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem,
)

from communications.comm_manager import CommManager
from communications.protocol import encode_frame
from config.config import CMD_RESET_FAULT
from core import RuntimeState, RuntimeStateMachine, TransitionError
from experiments import (
    DeviceProfile, EquipmentProfile, EquipmentProfileRepository,
    ExperimentRecorder, ExperimentSessionManager,
    ExperimentReportGenerator, ExperimentTemplate, ExperimentTemplateRepository,
    SessionStatus, WorkflowStep,
)
from logs.operation_logger import logger
from runtime_paths import writable_path
from widgets.historical_telemetry_dialog import HistoricalTelemetryDialog
from widgets.experiment_conclusion_dialog import ExperimentConclusionDialog


_STATUS_TEXT = {
    SessionStatus.CREATED: "已创建",
    SessionStatus.RUNNING: "记录中",
    SessionStatus.COMPLETED: "已完成",
    SessionStatus.ABORTED: "已中止",
}
_RUNTIME_TEXT = {
    RuntimeState.DISCONNECTED: "未连接",
    RuntimeState.CONNECTED: "已连接，待预检",
    RuntimeState.PRECHECK: "预检中",
    RuntimeState.READY: "已就绪",
    RuntimeState.RUNNING: "运行中",
    RuntimeState.STOPPING: "停机中",
    RuntimeState.FAULT_LOCKED: "故障锁定",
}


class ExperimentPage(QWidget):
    def __init__(self, comm: CommManager, *, software_version: str = "",
                 storage_root: str | Path | None = None,
                 snapshot_provider: Callable[[], dict] | None = None,
                 runtime_state: RuntimeStateMachine | None = None) -> None:
        super().__init__()
        self._comm = comm
        root_path = (Path(storage_root) if storage_root is not None else
                     writable_path("experiment_records", ".keep").parent)
        self.manager = ExperimentSessionManager(root_path)
        self.template_repository = ExperimentTemplateRepository(
            root_path / "_templates")
        self.equipment_repository = EquipmentProfileRepository(
            root_path / "_equipment")
        self.report_generator = ExperimentReportGenerator(self.manager.repository)
        self.recorder = ExperimentRecorder(self.manager, comm, logger, self)
        self._software_version = software_version
        self._snapshot_provider = snapshot_provider
        self._runtime_state = runtime_state
        self._last_session = None

        root = QVBoxLayout(self)
        title = QLabel("实验管理")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        intro = QLabel(
            "一次实验会自动归档设备与参数快照、遥测、操作事件和结束原因。"
            "实验模板会冻结方案和安全边界，并逐步引导、记录每一次确认。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8fa3b8;")
        root.addWidget(intro)

        root.addWidget(self._build_runtime_box())
        root.addWidget(self._build_config_box())
        root.addWidget(self._build_equipment_box())
        root.addWidget(self._build_template_box())
        root.addWidget(self._build_status_box())
        root.addWidget(self._build_history_box(), 1)

        self.recorder.countersChanged.connect(self._refresh_status)
        self.recorder.recordingError.connect(self._on_recording_error)
        if self._runtime_state is not None:
            self._runtime_state.stateChanged.connect(self._on_runtime_state_changed)
        self._refresh_templates()
        self._refresh_equipment_profiles()
        self._apply_selected_equipment()
        self._apply_selected_template()
        self._apply_snapshot_preview()
        self._refresh_runtime_state()
        self._refresh_status()
        self._refresh_history()

    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("实验信息")
        form = QFormLayout(box)
        self._name = QLineEdit()
        self._name.setPlaceholderText("例如：1500 rpm 转速阶跃")
        self._purpose = QPlainTextEdit()
        self._purpose.setPlaceholderText("本次实验要验证的问题")
        self._purpose.setMaximumHeight(70)
        self._operator = QLineEdit()
        self._device_name = QLineEdit("78W PMSM")
        self._rated_power = QDoubleSpinBox()
        self._rated_power.setRange(0, 1_000_000)
        self._rated_power.setSuffix(" W")
        self._rated_power.setValue(78)
        self._bus_voltage = QDoubleSpinBox()
        self._bus_voltage.setRange(0, 10_000)
        self._bus_voltage.setSuffix(" V")
        self._source = QComboBox()
        self._source.addItem("真实设备", "real")
        self._source.addItem("数字孪生", "sim")
        form.addRow("实验名称", self._name)
        form.addRow("实验目的", self._purpose)
        form.addRow("操作者", self._operator)
        form.addRow("设备名称", self._device_name)
        form.addRow("额定功率", self._rated_power)
        form.addRow("直流母线", self._bus_voltage)
        form.addRow("数据来源", self._source)
        return box

    def _build_equipment_box(self) -> QGroupBox:
        box = QGroupBox("设备组合档案（不可变修订）")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self._equipment_combo = QComboBox()
        self._equipment_combo.setEditable(True)
        self._equipment_combo.setMinimumWidth(280)
        self._btn_apply_equipment = QPushButton("应用档案")
        self._btn_save_equipment = QPushButton("保存新修订")
        self._btn_delete_equipment = QPushButton("删除当前修订")
        self._btn_apply_equipment.clicked.connect(self._apply_selected_equipment)
        self._btn_save_equipment.clicked.connect(self._save_equipment_revision)
        self._btn_delete_equipment.clicked.connect(self._delete_selected_equipment)
        row.addWidget(QLabel("档案："))
        row.addWidget(self._equipment_combo, 1)
        row.addWidget(self._btn_apply_equipment)
        row.addWidget(self._btn_save_equipment)
        row.addWidget(self._btn_delete_equipment)
        layout.addLayout(row)

        form = QFormLayout()
        self._equipment_inverter = QLineEdit()
        self._equipment_controller = QLineEdit()
        self._equipment_sensors = QLineEdit()
        self._equipment_sensors.setPlaceholderText("多个传感器用逗号分隔")
        hardware_row = QHBoxLayout()
        hardware_row.addWidget(QLabel("逆变器"))
        hardware_row.addWidget(self._equipment_inverter, 1)
        hardware_row.addWidget(QLabel("控制器"))
        hardware_row.addWidget(self._equipment_controller, 1)
        hardware_row.addWidget(QLabel("传感器"))
        hardware_row.addWidget(self._equipment_sensors, 1)
        hardware_widget = QWidget(); hardware_widget.setLayout(hardware_row)
        form.addRow("硬件组合", hardware_widget)

        self._expected_device_id = QLineEdit()
        self._expected_hardware = QLineEdit()
        self._expected_firmware_prefix = QLineEdit()
        identity_row = QHBoxLayout()
        identity_row.addWidget(QLabel("设备ID")); identity_row.addWidget(self._expected_device_id, 1)
        identity_row.addWidget(QLabel("硬件版本")); identity_row.addWidget(self._expected_hardware, 1)
        identity_row.addWidget(QLabel("固件前缀")); identity_row.addWidget(self._expected_firmware_prefix, 1)
        identity_widget = QWidget(); identity_widget.setLayout(identity_row)
        form.addRow("真实v2白名单", identity_widget)

        self._equipment_limits: dict[str, QDoubleSpinBox] = {}
        limits_row = QHBoxLayout()
        for key, title, suffix, maximum in (
            ("max_rpm", "转速", " rpm", 100_000),
            ("max_bus_voltage_v", "母线", " V", 10_000),
            ("max_current_a", "电流", " A", 10_000),
            ("max_temperature_c", "温度", " °C", 1_000),
        ):
            spin = QDoubleSpinBox(); spin.setRange(0, maximum); spin.setSuffix(suffix)
            self._equipment_limits[key] = spin
            limits_row.addWidget(QLabel(title)); limits_row.addWidget(spin)
        limits_widget = QWidget(); limits_widget.setLayout(limits_row)
        form.addRow("档案安全上限", limits_widget)
        layout.addLayout(form)
        return box

    def _build_runtime_box(self) -> QGroupBox:
        box = QGroupBox("设备运行状态机")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self._runtime_value = QLabel("未启用")
        self._runtime_value.setStyleSheet("font-size: 16px; font-weight: 600;")
        self._btn_precheck = QPushButton("执行运行预检")
        self._btn_precheck.setObjectName("PrimaryButton")
        self._btn_reset_fault = QPushButton("确认并复位故障")
        self._btn_reset_fault.setObjectName("EmergencyButton")
        self._btn_precheck.clicked.connect(self._run_precheck)
        self._btn_reset_fault.clicked.connect(self._reset_fault)
        row.addWidget(QLabel("当前状态："))
        row.addWidget(self._runtime_value)
        row.addStretch(1)
        row.addWidget(self._btn_precheck)
        row.addWidget(self._btn_reset_fault)
        layout.addLayout(row)
        self._precheck_detail = QLabel(
            "连接数字孪生或真实设备后执行预检；READY 之前启动命令会被拒绝。")
        self._precheck_detail.setWordWrap(True)
        self._precheck_detail.setStyleSheet("color: #8fa3b8;")
        layout.addWidget(self._precheck_detail)
        return box

    def _build_template_box(self) -> QGroupBox:
        box = QGroupBox("实验方案与引导步骤")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self._template_combo = QComboBox()
        self._template_combo.setEditable(True)
        self._template_combo.setMinimumWidth(280)
        self._btn_apply_template = QPushButton("应用模板")
        self._btn_save_template = QPushButton("将当前配置另存为模板")
        self._btn_delete_template = QPushButton("删除模板")
        self._btn_apply_template.clicked.connect(self._apply_selected_template)
        self._btn_save_template.clicked.connect(self._save_current_template)
        self._btn_delete_template.clicked.connect(self._delete_selected_template)
        row.addWidget(QLabel("实验模板："))
        row.addWidget(self._template_combo, 1)
        row.addWidget(self._btn_apply_template)
        row.addWidget(self._btn_save_template)
        row.addWidget(self._btn_delete_template)
        layout.addLayout(row)

        self._template_steps = QPlainTextEdit()
        self._template_steps.setMaximumHeight(76)
        self._template_steps.setPlaceholderText(
            "每行：! 标题 | 操作说明 | 预期结果 | 要求状态；? 表示可选")
        layout.addWidget(self._template_steps)

        guide = QHBoxLayout()
        self._workflow_step = QLabel("尚未开始引导流程")
        self._workflow_step.setWordWrap(True)
        self._workflow_step.setStyleSheet("color: #b8c6d8;")
        self._workflow_note = QLineEdit()
        self._workflow_note.setPlaceholderText("本步骤观察、结果或跳过原因")
        self._btn_confirm_step = QPushButton("确认本步骤完成")
        self._btn_confirm_step.setObjectName("PrimaryButton")
        self._btn_skip_step = QPushButton("跳过可选步骤")
        self._btn_confirm_step.clicked.connect(self._confirm_workflow_step)
        self._btn_skip_step.clicked.connect(self._skip_workflow_step)
        guide.addWidget(self._workflow_step, 2)
        guide.addWidget(self._workflow_note, 2)
        guide.addWidget(self._btn_confirm_step)
        guide.addWidget(self._btn_skip_step)
        layout.addLayout(guide)
        return box

    def _build_status_box(self) -> QGroupBox:
        box = QGroupBox("当前实验")
        layout = QVBoxLayout(box)
        form = QFormLayout()
        self._id_value = QLabel("—")
        self._status_value = QLabel("未开始")
        self._counts_value = QLabel("遥测 0 条　｜　事件 0 条")
        self._path_value = QLabel(str(self.manager.repository.root))
        self._path_value.setWordWrap(True)
        form.addRow("实验编号", self._id_value)
        form.addRow("状态", self._status_value)
        form.addRow("记录数量", self._counts_value)
        form.addRow("保存目录", self._path_value)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self._btn_start = QPushButton("新建并开始实验")
        self._btn_start.setObjectName("PrimaryButton")
        self._btn_complete = QPushButton("正常结束")
        self._btn_abort = QPushButton("异常中止")
        self._btn_abort.setObjectName("EmergencyButton")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_complete.clicked.connect(self._on_complete)
        self._btn_abort.clicked.connect(self._on_abort)
        buttons.addWidget(self._btn_start)
        buttons.addWidget(self._btn_complete)
        buttons.addWidget(self._btn_abort)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        markers = QHBoxLayout()
        self._marker_type = QComboBox()
        for label, category in (
            ("目标转速改变", "target_changed"), ("负载投入", "load_applied"),
            ("负载卸载", "load_removed"), ("控制参数修改", "parameter_changed"),
            ("出现振荡", "oscillation"),
            ("出现异响", "abnormal_sound"), ("保护触发", "protection"),
            ("自定义事件", "custom"),
        ):
            self._marker_type.addItem(label, category)
        self._marker_note = QLineEdit()
        self._marker_note.setPlaceholderText("补充观察（自定义事件必填）")
        self._btn_add_marker = QPushButton("记录时间标记")
        self._btn_add_marker.clicked.connect(self._record_quick_marker)
        markers.addWidget(QLabel("快捷事件："))
        markers.addWidget(self._marker_type)
        markers.addWidget(self._marker_note, 1)
        markers.addWidget(self._btn_add_marker)
        layout.addLayout(markers)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        layout.addWidget(self._message)
        return box

    def _build_history_box(self) -> QGroupBox:
        box = QGroupBox("历史实验（只读）")
        layout = QVBoxLayout(box)
        bar = QHBoxLayout()
        hint = QLabel("选择记录可查看完整元数据；历史实验不会恢复为运行状态。")
        hint.setStyleSheet("color: #8fa3b8;")
        self._btn_refresh_history = QPushButton("刷新")
        self._btn_refresh_history.clicked.connect(self._refresh_history)
        self._btn_curves = QPushButton("查看遥测曲线")
        self._btn_curves.setEnabled(False)
        self._btn_curves.clicked.connect(self._open_historical_telemetry)
        self._btn_report = QPushButton("生成实验报告")
        self._btn_report.setEnabled(False)
        self._btn_report.clicked.connect(self._generate_selected_report)
        self._btn_conclusion = QPushButton("编辑实验结论")
        self._btn_conclusion.setEnabled(False)
        self._btn_conclusion.clicked.connect(self._edit_selected_conclusion)
        bar.addWidget(hint, 1)
        bar.addWidget(self._btn_conclusion)
        bar.addWidget(self._btn_report)
        bar.addWidget(self._btn_curves)
        bar.addWidget(self._btn_refresh_history)
        layout.addLayout(bar)

        body = QHBoxLayout()
        self._history = QTableWidget(0, 6)
        self._history.setHorizontalHeaderLabels(
            ["实验编号", "名称", "状态", "数据源", "遥测", "开始时间"])
        self._history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._history.setSelectionMode(QAbstractItemView.SingleSelection)
        self._history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._history.verticalHeader().setVisible(False)
        header = self._history.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._history.itemSelectionChanged.connect(self._show_selected_history)
        body.addWidget(self._history, 3)

        self._history_detail = QPlainTextEdit()
        self._history_detail.setReadOnly(True)
        self._history_detail.setPlaceholderText("选择一条历史实验查看详情")
        body.addWidget(self._history_detail, 2)
        layout.addLayout(body, 1)
        return box

    def _on_start(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._set_message("请填写实验名称。", error=True)
            return
        try:
            template = self._load_selected_template()
            equipment = self._load_selected_equipment()
            snapshot = self._snapshot_provider() if self._snapshot_provider else {}
            device_data = dict(snapshot.get("device", {}))
            protocol = self._comm.protocol_status()
            equipment_extra = {}
            if equipment is not None:
                equipment_extra = {
                    "equipment_profile_id": equipment.profile_id,
                    "equipment_family_id": equipment.family_id,
                    "equipment_revision": equipment.revision,
                    "equipment_safety_limits": dict(equipment.safety_limits),
                    "expected_device_identity": {
                        "device_id": equipment.expected_device_id,
                        "hardware_version": equipment.expected_hardware_version,
                        "firmware_prefix": equipment.expected_firmware_prefix,
                    },
                }
            extra = dict(device_data.get("extra", {}))
            extra.update(equipment_extra)
            device = DeviceProfile(
                name=device_data.get("name") or self._device_name.text().strip()
                or "未命名设备",
                motor_type=device_data.get("motor_type", "PMSM"),
                rated_power_w=device_data.get("rated_power_w")
                or self._rated_power.value() or None,
                dc_bus_voltage_v=device_data.get("dc_bus_voltage_v")
                or self._bus_voltage.value() or None,
                inverter=device_data.get("inverter") or
                (equipment.inverter if equipment else ""),
                controller=device_data.get("controller") or
                (equipment.controller if equipment else ""),
                sensors=list(device_data.get("sensors") or
                             (equipment.sensors if equipment else [])),
                firmware_version=device_data.get("firmware_version") or
                protocol.get("firmware_version", ""),
                protocol_version=device_data.get("protocol_version") or
                (str(protocol.get("protocol_version"))
                 if protocol.get("protocol_version") is not None else ""),
                extra=extra,
            )
            session = self.manager.create_session(
                name,
                purpose=self._purpose.toPlainText().strip(),
                operator=self._operator.text().strip(),
                data_source=self._source.currentData(),
                software_version=self._software_version,
                device=device,
                controller_params=dict(snapshot.get("controller_params", {})),
                protection_params=dict(snapshot.get("protection_params", {})),
                template=template,
            )
            self.manager.start()
            self._capture_runtime_context("start")
            self._last_session = session
            logger.log("开始实验", f"{session.experiment_id} {session.name}")
            self._set_message(f"实验 {session.experiment_id} 正在记录。")
        except Exception as exc:
            self._set_message(f"实验启动失败：{exc}", error=True)
        self._refresh_status()
        self._refresh_workflow()

    def _on_complete(self) -> None:
        session = self.manager.active_session
        if session is None or session.status is not SessionStatus.RUNNING:
            self._set_message("当前没有正在运行的实验。", error=True)
            return
        if (self._runtime_state is not None and self._runtime_state.state in
                {RuntimeState.RUNNING, RuntimeState.STOPPING}):
            self._set_message("设备尚未安全停止，不能正常结束实验。", error=True)
            return
        logger.log("结束实验", f"{session.experiment_id} 正常结束")
        try:
            self._capture_runtime_context("end")
            self._last_session = self.manager.complete()
            self._set_message(f"实验 {session.experiment_id} 已保存。")
        except Exception as exc:
            self._set_message(f"实验结束失败：{exc}", error=True)
        self._refresh_status()
        self._refresh_workflow()
        self._refresh_history(select_id=session.experiment_id)

    def _on_abort(self) -> None:
        session = self.manager.active_session
        if session is None or session.status is not SessionStatus.RUNNING:
            self._set_message("当前没有正在运行的实验。", error=True)
            return
        answer = QMessageBox.question(
            self, "确认中止实验",
            "中止会保留已经记录的数据，并把本次实验标记为异常中止。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        logger.log("中止实验", f"{session.experiment_id} 用户中止")
        try:
            self._capture_runtime_context("end")
            self._last_session = self.manager.abort("用户从实验管理页中止")
            self._set_message(f"实验 {session.experiment_id} 已中止，已有数据已保留。",
                              error=True)
        except Exception as exc:
            self._set_message(f"实验中止失败：{exc}", error=True)
        self._refresh_status()
        self._refresh_workflow()
        self._refresh_history(select_id=session.experiment_id)

    def shutdown(self) -> None:
        """主窗口关闭时保存现场并解除信号连接。"""
        session = self.manager.active_session
        if session is not None and session.status is SessionStatus.RUNNING:
            try:
                logger.log("中止实验", f"{session.experiment_id} 软件关闭")
                self._capture_runtime_context("end")
                self._last_session = self.manager.abort("上位机软件关闭")
            except Exception as exc:
                self._set_message(f"关闭时保存实验失败：{exc}", error=True)
        self.recorder.close()
        self._refresh_status()
        self._refresh_workflow()

    def _refresh_status(self) -> None:
        session = self.manager.active_session or self._last_session
        running = (self.manager.active_session is not None and
                   self.manager.active_session.status is SessionStatus.RUNNING)
        self._btn_start.setEnabled(not running)
        self._btn_complete.setEnabled(running)
        self._btn_abort.setEnabled(running)
        self._marker_type.setEnabled(running)
        self._marker_note.setEnabled(running)
        self._btn_add_marker.setEnabled(running)
        for widget in (
            self._name, self._purpose, self._operator, self._device_name,
            self._rated_power, self._bus_voltage, self._source,
        ):
            widget.setEnabled(not running)
        for widget in (
            self._template_combo, self._btn_apply_template,
            self._btn_save_template, self._btn_delete_template,
            self._template_steps,
        ):
            widget.setEnabled(not running)
        for widget in (
            self._equipment_combo, self._btn_apply_equipment,
            self._btn_save_equipment, self._btn_delete_equipment,
            self._equipment_inverter, self._equipment_controller,
            self._equipment_sensors, self._expected_device_id,
            self._expected_hardware, self._expected_firmware_prefix,
            *self._equipment_limits.values(),
        ):
            widget.setEnabled(not running)
        if session is None:
            self._id_value.setText("—")
            self._status_value.setText("未开始")
            self._counts_value.setText("遥测 0 条　｜　事件 0 条")
            self._refresh_workflow()
            return
        self._id_value.setText(session.experiment_id)
        self._status_value.setText(_STATUS_TEXT[session.status])
        self._counts_value.setText(
            f"遥测 {session.telemetry_count} 条　｜　事件 {session.event_count} 条")
        self._refresh_workflow()

    def _on_recording_error(self, message: str) -> None:
        self._set_message(message, error=True)

    def _run_precheck(self) -> None:
        if self._runtime_state is None:
            return
        try:
            self._runtime_state.begin_precheck()
        except TransitionError as exc:
            self._set_message(str(exc), error=True)
            return
        failures = []
        snapshot = {}
        try:
            snapshot = self._snapshot_provider() if self._snapshot_provider else {}
        except Exception as exc:
            failures.append(f"控制配置无法读取：{exc}")
        controller = snapshot.get("controller_params", {})
        device = snapshot.get("device", {})
        if not (self._comm.is_connected() or self._comm.is_sim_running()):
            failures.append("通信或数字孪生未实际连接")
        latest = self._comm.latest_frame()
        if latest.bus_state == "ov":
            failures.append("直流母线处于过压跳闸状态")
        if latest.fault_code:
            failures.append(latest.fault_text or
                            f"下位机故障位 0x{latest.fault_code:02X}")
        if not device.get("name"):
            failures.append("电机设备名称为空")
        if not device.get("sensors"):
            failures.append("未选择位置传感器")
        if not controller.get("control_mode"):
            failures.append("未选择控制方式")
        target = controller.get("target_speed_rpm")
        max_rpm = device.get("extra", {}).get("max_rpm")
        if target is not None and max_rpm is not None and abs(target) > max_rpm:
            failures.append(f"目标转速 {target} rpm 超过上限 {max_rpm} rpm")
        configured_max_current = snapshot.get("protection_params", {}).get(
            "max_current_a")
        if (configured_max_current is not None and
                abs(latest.current_actual) > float(configured_max_current)):
            failures.append(
                f"当前电流 {abs(latest.current_actual):.2f} A 超过控制页限幅 "
                f"{float(configured_max_current):.2f} A")
        limits = self._active_template_limits()
        template_max_rpm = limits.get("max_rpm")
        if (target is not None and template_max_rpm is not None and
                abs(float(target)) > float(template_max_rpm)):
            failures.append(
                f"目标转速 {target} rpm 超过模板上限 {template_max_rpm} rpm")
        max_bus = limits.get("max_bus_voltage_v")
        if max_bus is not None and latest.vdc > float(max_bus):
            failures.append(
                f"当前母线 {latest.vdc:.1f} V 超过模板上限 {float(max_bus):.1f} V")
        equipment = self._load_selected_equipment()
        equipment_limits = equipment.safety_limits if equipment else {}
        eq_max_rpm = equipment_limits.get("max_rpm")
        if (target is not None and eq_max_rpm is not None and
                abs(float(target)) > float(eq_max_rpm)):
            failures.append(
                f"目标转速 {target} rpm 超过设备档案上限 {eq_max_rpm} rpm")
        eq_max_bus = equipment_limits.get("max_bus_voltage_v")
        if eq_max_bus is not None and latest.vdc > float(eq_max_bus):
            failures.append(
                f"当前母线 {latest.vdc:.1f} V 超过设备档案上限 "
                f"{float(eq_max_bus):.1f} V")
        eq_max_current = equipment_limits.get("max_current_a")
        # 当前协议上报的是等效电流标量，后续扩展三相值时可改为相电流峰值。
        current_peak = abs(latest.current_actual)
        if eq_max_current is not None and current_peak > float(eq_max_current):
            failures.append(
                f"当前相电流 {current_peak:.2f} A 超过设备档案上限 "
                f"{float(eq_max_current):.2f} A")
        eq_max_temp = equipment_limits.get("max_temperature_c")
        if eq_max_temp is not None and latest.temperature > float(eq_max_temp):
            failures.append(
                f"当前温度 {latest.temperature:.1f} °C 超过设备档案上限 "
                f"{float(eq_max_temp):.1f} °C")
        expected = self._source.currentData()
        if expected == "real" and not self._comm.is_connected():
            failures.append("实验数据源选择真实设备，但真实通信未连接")
        if expected == "real" and equipment is not None:
            protocol = self._comm.protocol_status()
            if not equipment.expected_device_id:
                failures.append("设备档案未配置真实v2设备ID，请先保存新修订")
            elif protocol.get("mode") != "negotiated-v2":
                failures.append("真机设备档案必须使用 negotiated-v2 严格握手")
            elif not protocol.get("identity_verified"):
                failures.append("真机v2握手尚未通过设备身份白名单")
        if expected == "sim" and not self._comm.is_sim_running():
            failures.append("实验数据源选择数字孪生，但仿真未启动")
        if failures:
            reason = "；".join(failures)
            self._runtime_state.fail_precheck(reason)
            self._precheck_detail.setText("预检未通过：" + reason)
            self._set_message("运行预检未通过。", error=True)
            logger.log("运行预检失败", reason)
        else:
            self._runtime_state.pass_precheck("软件运行预检通过")
            mode = self._comm.protocol_status().get("mode", "legacy-v1")
            ack_hint = ("v2危险命令仍需等待设备ACK。" if mode != "legacy-v1" else
                        "legacy-v1没有设备ACK，真机需额外观察下位机状态。")
            self._precheck_detail.setText(
                "预检通过：连接、数据源、电机、传感器、控制方式和转速边界有效。"
                + ack_hint)
            self._set_message("运行预检通过，设备已进入 READY。")
            logger.log("运行预检通过", device.get("name", ""))

    def _reset_fault(self) -> None:
        if self._runtime_state is None:
            return
        answer = QMessageBox.question(
            self, "确认故障复位",
            "请确认故障原因已经排除、下位机保护已复位且功率级处于安全状态。\n"
            "上位机复位不会替代硬件或下位机保护复位。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        device_reset_sent = False
        if self._comm.is_connected():
            latest = self._comm.latest_frame()
            if (getattr(latest, "mc_state", 0) == 11 or
                    int(getattr(latest, "fault_code", 0) or 0) != 0):
                self._comm.send_frame(encode_frame(CMD_RESET_FAULT))
                device_reset_sent = True
                logger.log("下位机故障确认复位", "已发送RESET_FAULT清除MCSDK/应用层锁存")
        if device_reset_sent:
            # negotiated-v2 的发送结果是异步 ACK/NACK。运行状态只能由主窗口
            # 收到设备 ACK 后复位，不能在这里提前伪装成 CONNECTED。
            self._set_message("已发送下位机故障复位，正在等待设备ACK……")
            return
        try:
            if self._runtime_state.state is RuntimeState.FAULT_LOCKED:
                self._runtime_state.reset_fault()
                logger.log("故障状态复位", "用户确认故障原因已排除")
            if self._runtime_state.state is not RuntimeState.FAULT_LOCKED:
                self._set_message("当前上位机和下位机均无待确认故障。")
        except TransitionError as exc:
            self._set_message(str(exc), error=True)

    def _on_runtime_state_changed(self, _previous, _current, reason: str) -> None:
        self._refresh_runtime_state()
        self._precheck_detail.setText(reason)

    def _refresh_runtime_state(self) -> None:
        if self._runtime_state is None:
            self._runtime_value.setText("未启用")
            self._btn_precheck.setEnabled(False)
            self._btn_reset_fault.setEnabled(False)
            return
        state = self._runtime_state.state
        self._runtime_value.setText(_RUNTIME_TEXT[state] + f"（{state.value}）")
        color = {
            RuntimeState.READY: "#66bb6a",
            RuntimeState.RUNNING: "#4fc3f7",
            RuntimeState.FAULT_LOCKED: "#ef5350",
            RuntimeState.PRECHECK: "#ffa726",
        }.get(state, "#b8c6d8")
        self._runtime_value.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {color};")
        self._btn_precheck.setEnabled(state is RuntimeState.CONNECTED)
        self._btn_reset_fault.setEnabled(
            state is RuntimeState.FAULT_LOCKED or self._comm.is_connected())

    def _apply_snapshot_preview(self) -> None:
        """显示控制页当前设备摘要；真正快照仍在实验开始瞬间重新读取。"""
        if self._snapshot_provider is None:
            return
        try:
            device = self._snapshot_provider().get("device", {})
        except Exception as exc:
            self._set_message(f"控制配置读取失败：{exc}", error=True)
            return
        if device.get("name"):
            self._device_name.setText(str(device["name"]))
        if device.get("rated_power_w") is not None:
            self._rated_power.setValue(float(device["rated_power_w"]))
        if device.get("dc_bus_voltage_v") is not None:
            self._bus_voltage.setValue(float(device["dc_bus_voltage_v"]))

    def _refresh_equipment_profiles(self, select_id: str = "") -> None:
        self._equipment_combo.clear()
        for profile in self.equipment_repository.list_profiles():
            built_in = "（内置）" if profile.built_in else ""
            self._equipment_combo.addItem(
                f"{profile.name} · R{profile.revision:03d}{built_in}",
                profile.profile_id)
        if select_id:
            index = self._equipment_combo.findData(select_id)
            if index >= 0:
                self._equipment_combo.setCurrentIndex(index)

    def _load_selected_equipment(self) -> EquipmentProfile | None:
        profile_id = self._equipment_combo.currentData()
        if not profile_id:
            return None
        return self.equipment_repository.load(str(profile_id))

    def _apply_selected_equipment(self) -> None:
        try:
            profile = self._load_selected_equipment()
        except Exception as exc:
            self._set_message(f"设备档案读取失败：{exc}", error=True)
            return
        if profile is None:
            self._comm.configure_expected_device_identity()
            self._set_message("已关闭设备档案白名单，当前为兼容模式。")
            return
        self._device_name.setText(profile.name)
        self._rated_power.setValue(float(profile.rated_power_w or 0))
        self._bus_voltage.setValue(float(profile.nominal_bus_voltage_v or 0))
        self._equipment_inverter.setText(profile.inverter)
        self._equipment_controller.setText(profile.controller)
        self._equipment_sensors.setText("，".join(profile.sensors))
        self._expected_device_id.setText(profile.expected_device_id)
        self._expected_hardware.setText(profile.expected_hardware_version)
        self._expected_firmware_prefix.setText(profile.expected_firmware_prefix)
        for key, spin in self._equipment_limits.items():
            spin.setValue(float(profile.safety_limits.get(key, 0) or 0))
        accepted = self._comm.configure_expected_device_identity(
            profile.expected_device_id, profile.expected_hardware_version,
            profile.expected_firmware_prefix)
        limits = "，".join(
            f"{key}={value}" for key, value in profile.safety_limits.items())
        suffix = "" if accepted else "；当前真机会话身份不匹配，已失效"
        self._set_message(
            f"已应用 {profile.profile_id}；安全上限：{limits or '未设置'}{suffix}",
            error=not accepted)

    def _save_equipment_revision(self) -> None:
        try:
            current = self._load_selected_equipment()
            typed_name = self._equipment_combo.currentText().strip()
            if current is not None:
                name = current.name
                family_id = current.family_id
            else:
                if not typed_name or typed_name.startswith("不使用设备档案"):
                    raise ValueError("请在档案名称框输入新设备档案名称")
                name = typed_name
                family_id = ""
            sensors = [item.strip() for item in
                       self._equipment_sensors.text().replace("，", ",").split(",")
                       if item.strip()]
            safety_limits = {
                key: spin.value() for key, spin in self._equipment_limits.items()
                if spin.value() > 0
            }
            profile = self.equipment_repository.create_revision(
                name, family_id=family_id, motor_type="PMSM",
                rated_power_w=self._rated_power.value() or None,
                nominal_bus_voltage_v=self._bus_voltage.value() or None,
                inverter=self._equipment_inverter.text().strip(),
                controller=self._equipment_controller.text().strip(),
                sensors=sensors,
                expected_device_id=self._expected_device_id.text().strip(),
                expected_hardware_version=self._expected_hardware.text().strip(),
                expected_firmware_prefix=self._expected_firmware_prefix.text().strip(),
                safety_limits=safety_limits,
                notes="由上位机设备档案页创建的不可变修订",
            )
            self._refresh_equipment_profiles(profile.profile_id)
            self._apply_selected_equipment()
            self._set_message(
                f"设备档案新修订 {profile.profile_id} 已保存，旧修订未被覆盖。")
        except Exception as exc:
            self._set_message(f"设备档案保存失败：{exc}", error=True)

    def _delete_selected_equipment(self) -> None:
        profile_id = self._equipment_combo.currentData()
        if not profile_id:
            self._set_message("当前没有可删除的设备档案修订。", error=True)
            return
        try:
            self.equipment_repository.delete(str(profile_id))
            self._refresh_equipment_profiles()
            self._comm.configure_expected_device_identity()
            self._set_message("设备档案修订已删除。")
        except Exception as exc:
            self._set_message(str(exc), error=True)

    def _refresh_templates(self, select_id: str = "") -> None:
        self._template_combo.clear()
        for template in self.template_repository.list_templates():
            label = template.name + ("（内置）" if template.built_in else "")
            self._template_combo.addItem(label, template.template_id)
        if select_id:
            index = self._template_combo.findData(select_id)
            if index >= 0:
                self._template_combo.setCurrentIndex(index)

    def _load_selected_template(self) -> ExperimentTemplate | None:
        template_id = self._template_combo.currentData()
        if not template_id:
            return None
        return self.template_repository.load(str(template_id))

    def _apply_selected_template(self) -> None:
        try:
            template = self._load_selected_template()
        except Exception as exc:
            self._set_message(f"模板读取失败：{exc}", error=True)
            return
        if template is None:
            self._template_steps.clear()
            return
        self._name.setText(template.name)
        self._purpose.setPlainText(template.purpose)
        source_index = self._source.findData(template.data_source)
        if source_index >= 0:
            self._source.setCurrentIndex(source_index)
        device = template.device_defaults
        if device.get("name"):
            self._device_name.setText(str(device["name"]))
        if device.get("rated_power_w") is not None:
            self._rated_power.setValue(float(device["rated_power_w"]))
        if device.get("dc_bus_voltage_v") is not None:
            self._bus_voltage.setValue(float(device["dc_bus_voltage_v"]))
        lines = []
        for step in template.steps:
            prefix = "!" if step.required else "?"
            lines.append(
                f"{prefix} {step.title} | {step.instruction} | "
                f"{step.expected_result} | {step.required_runtime_state}")
        self._template_steps.setPlainText("\n".join(lines))
        limits = "，".join(f"{key}={value}" for key, value in
                           template.safety_limits.items()) or "未设置"
        self._set_message(f"已应用模板 {template.name}；安全边界：{limits}")

    def _parse_template_steps(self) -> list[WorkflowStep]:
        steps = []
        for raw in self._template_steps.toPlainText().splitlines():
            text = raw.strip()
            if not text:
                continue
            required = not text.startswith("?")
            if text[:1] in {"!", "?"}:
                text = text[1:].strip()
            parts = [part.strip() for part in text.split("|", 3)]
            if not parts[0]:
                raise ValueError("步骤标题不能为空")
            steps.append(WorkflowStep(
                f"S{len(steps) + 1:02d}", parts[0],
                parts[1] if len(parts) > 1 else "",
                parts[2] if len(parts) > 2 else "", required,
                parts[3] if len(parts) > 3 else ""))
        if not steps:
            raise ValueError("实验模板至少需要一个步骤")
        return steps

    def _save_current_template(self) -> None:
        name = self._template_combo.currentText().strip()
        if not name or name.startswith("不使用模板"):
            self._set_message("请在模板名称框输入新模板名称。", error=True)
            return
        try:
            snapshot = self._snapshot_provider() if self._snapshot_provider else {}
            protection = dict(snapshot.get("protection_params", {}))
            template = self.template_repository.create(
                name.removesuffix("（内置）"),
                purpose=self._purpose.toPlainText().strip(),
                data_source=self._source.currentData(),
                device_defaults={
                    "name": self._device_name.text().strip(),
                    "motor_type": "PMSM",
                    "rated_power_w": self._rated_power.value() or None,
                    "dc_bus_voltage_v": self._bus_voltage.value() or None,
                },
                safety_limits=protection,
                steps=self._parse_template_steps(),
            )
            self._refresh_templates(template.template_id)
            self._set_message(f"实验模板 {template.name} 已保存。")
        except Exception as exc:
            self._set_message(f"模板保存失败：{exc}", error=True)

    def _delete_selected_template(self) -> None:
        template_id = self._template_combo.currentData()
        if not template_id:
            self._set_message("当前没有可删除的用户模板。", error=True)
            return
        try:
            self.template_repository.delete(str(template_id))
            self._refresh_templates()
            self._apply_selected_template()
            self._set_message("用户模板已删除。")
        except Exception as exc:
            self._set_message(str(exc), error=True)

    def _confirm_workflow_step(self) -> None:
        try:
            step = self.manager.current_workflow_step()
            if (step is not None and step.required_runtime_state and
                    self._runtime_state is not None and
                    self._runtime_state.state.value != step.required_runtime_state):
                raise RuntimeError(
                    f"本步骤要求运行状态为 {step.required_runtime_state}，"
                    f"当前为 {self._runtime_state.state.value}")
            step = self.manager.confirm_current_step(
                self._workflow_note.text().strip())
            logger.log("实验步骤完成", f"{step.step_id} {step.title}")
            self._workflow_note.clear()
            self._set_message(f"已记录步骤：{step.title}")
        except Exception as exc:
            self._set_message(str(exc), error=True)
        self._refresh_status()

    def _skip_workflow_step(self) -> None:
        try:
            step = self.manager.skip_current_step(
                self._workflow_note.text().strip())
            logger.log("跳过实验步骤", f"{step.step_id} {step.title}")
            self._workflow_note.clear()
            self._set_message(f"已记录跳过：{step.title}")
        except Exception as exc:
            self._set_message(str(exc), error=True)
        self._refresh_status()

    def _refresh_workflow(self) -> None:
        session = self.manager.active_session or self._last_session
        running = (self.manager.active_session is not None and
                   self.manager.active_session.status is SessionStatus.RUNNING)
        step = None
        current = total = 0
        if running:
            try:
                current, total = self.manager.workflow_progress()
                step = self.manager.current_workflow_step()
            except RuntimeError:
                pass
        elif session is not None:
            current = session.workflow_current_index
            total = len(session.template_snapshot.get("steps", []))
        if session is None or not session.template_snapshot:
            self._workflow_step.setText("当前实验未启用模板引导。")
        elif step is not None:
            kind = "必做" if step.required else "可选"
            state_hint = (f"；要求状态={step.required_runtime_state}"
                          if step.required_runtime_state else "")
            self._workflow_step.setText(
                f"步骤 {current + 1}/{total}（{kind}{state_hint}）：{step.title}\n"
                f"{step.instruction}\n预期：{step.expected_result or '—'}")
        else:
            self._workflow_step.setText(f"模板流程进度：{current}/{total}，必做步骤已完成。")
        self._btn_confirm_step.setEnabled(running and step is not None)
        self._btn_skip_step.setEnabled(
            running and step is not None and not step.required)
        self._workflow_note.setEnabled(running and step is not None)

    def _active_template_limits(self) -> dict:
        session = self.manager.active_session
        if session is not None and session.template_snapshot:
            return dict(session.template_snapshot.get("safety_limits", {}))
        try:
            template = self._load_selected_template()
            return dict(template.safety_limits) if template else {}
        except Exception:
            return {}

    def _refresh_history(self, select_id: str | None = None) -> None:
        sessions = self.manager.repository.list_sessions()
        self._btn_curves.setEnabled(False)
        self._btn_report.setEnabled(False)
        self._btn_conclusion.setEnabled(False)
        self._history.setRowCount(len(sessions))
        selected_row = -1
        for row, session in enumerate(sessions):
            values = (
                session.experiment_id,
                session.name,
                _STATUS_TEXT[session.status],
                "数字孪生" if session.data_source == "sim" else "真实设备",
                str(session.telemetry_count),
                session.started_at or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self._history.setItem(row, column, item)
            if session.experiment_id == select_id:
                selected_row = row
        if selected_row >= 0:
            self._history.selectRow(selected_row)
        elif not sessions:
            self._history_detail.clear()

    def _show_selected_history(self) -> None:
        row = self._history.currentRow()
        if row < 0 or self._history.item(row, 0) is None:
            return
        experiment_id = self._history.item(row, 0).text()
        try:
            session = self.manager.load(experiment_id)
            self._btn_curves.setEnabled(session.telemetry_count > 0)
            self._btn_report.setEnabled(True)
            self._btn_conclusion.setEnabled(True)
            device = session.device.to_dict() if session.device else {}
            detail = (
                f"实验编号：{session.experiment_id}\n"
                f"实验名称：{session.name}\n"
                f"状态：{_STATUS_TEXT[session.status]}\n"
                f"实验目的：{session.purpose or '—'}\n"
                f"操作者：{session.operator or '—'}\n"
                f"创建时间：{session.created_at or '—'}\n"
                f"开始时间：{session.started_at or '—'}\n"
                f"结束时间：{session.ended_at or '—'}\n"
                f"结束原因：{session.end_reason or '—'}\n"
                f"遥测/事件：{session.telemetry_count} / {session.event_count}\n"
                f"实验模板：{session.template_snapshot.get('name', '自由实验')}\n"
                f"工作流进度：{session.workflow_current_index} / "
                f"{len(session.template_snapshot.get('steps', []))}\n"
                f"结论状态：{session.conclusion.get('result_status', 'pending')}\n"
                f"软件版本：{session.software_version or '—'}\n"
                f"保存目录：{self.manager.repository.session_dir(experiment_id)}\n\n"
                "设备快照：\n" + json.dumps(device, ensure_ascii=False, indent=2) +
                "\n\n控制参数：\n" +
                json.dumps(session.controller_params, ensure_ascii=False, indent=2) +
                "\n\n保护参数：\n" +
                json.dumps(session.protection_params, ensure_ascii=False, indent=2)
            )
            self._history_detail.setPlainText(detail)
        except Exception as exc:
            self._btn_curves.setEnabled(False)
            self._btn_report.setEnabled(False)
            self._btn_conclusion.setEnabled(False)
            self._history_detail.setPlainText(f"历史实验读取失败：{exc}")

    def _open_historical_telemetry(self) -> None:
        row = self._history.currentRow()
        if row < 0 or self._history.item(row, 0) is None:
            return
        experiment_id = self._history.item(row, 0).text()
        try:
            rows = self.manager.repository.read_telemetry(experiment_id)
        except Exception as exc:
            self._set_message(f"遥测读取失败：{exc}", error=True)
            return
        if not rows:
            self._set_message("该实验没有可用的遥测数据。", error=True)
            return
        events = self.manager.repository.read_events(experiment_id)
        dialog = HistoricalTelemetryDialog(experiment_id, rows, events, self)
        try:
            dialog.exec()
        finally:
            # 测试可能替换exec而跳过Qt正常模态生命周期；显式释放也避免历史
            # 曲线中的pyqtgraph对象堆积到解释器退出阶段再集中析构。
            dialog.close()
            dialog.deleteLater()

    def _record_quick_marker(self) -> None:
        session = self.manager.active_session
        if session is None or session.status is not SessionStatus.RUNNING:
            self._set_message("只有记录中的实验可以添加时间标记。", error=True)
            return
        category = str(self._marker_type.currentData())
        label = self._marker_type.currentText()
        note = self._marker_note.text().strip()
        if category == "custom" and not note:
            self._set_message("自定义事件必须填写说明。", error=True)
            return
        latest = self._comm.latest_frame()
        snapshot = {
            "runtime_state": (self._runtime_state.state.value
                              if self._runtime_state is not None else "not_enabled"),
            "speed_actual": latest.speed_actual,
            "speed_target": latest.speed_target,
            "current_actual": latest.current_actual,
            "torque_actual": latest.torque_actual,
            "vdc": latest.vdc,
            "temperature": latest.temperature,
            "fault_code": latest.fault_code,
            "fault_text": latest.fault_text,
        }
        try:
            message = note if category == "custom" else label
            self.manager.record_marker(category, message, snapshot, note)
            logger.log("实验事件标记", f"{label} {note}".strip())
            self._marker_note.clear()
            self._set_message(f"已记录时间标记：{message}")
            self._refresh_status()
        except Exception as exc:
            self._set_message(f"事件标记失败：{exc}", error=True)

    def _edit_selected_conclusion(self) -> None:
        row = self._history.currentRow()
        if row < 0 or self._history.item(row, 0) is None:
            self._set_message("请先选择一条历史实验。", error=True)
            return
        experiment_id = self._history.item(row, 0).text()
        try:
            session = self.manager.load(experiment_id)
            dialog = ExperimentConclusionDialog(
                experiment_id, session.conclusion, self)
            try:
                accepted = dialog.exec() == QDialog.Accepted
                conclusion = dialog.conclusion() if accepted else None
            finally:
                dialog.close()
                dialog.deleteLater()
            if conclusion is None:
                return
            self.manager.update_conclusion(experiment_id, conclusion)
            logger.log("更新实验结论", experiment_id)
            self._show_selected_history()
            self._set_message("实验结论已保存；重新生成报告即可更新报告内容。")
        except Exception as exc:
            self._set_message(f"实验结论保存失败：{exc}", error=True)

    def _generate_selected_report(self) -> None:
        row = self._history.currentRow()
        if row < 0 or self._history.item(row, 0) is None:
            self._set_message("请先选择一条历史实验。", error=True)
            return
        experiment_id = self._history.item(row, 0).text()
        try:
            paths = self.report_generator.generate(experiment_id)
            logger.log("生成实验报告", f"{experiment_id} {paths.markdown}")
            self._set_message(f"报告已生成：{paths.markdown}")
            self._history_detail.appendPlainText(
                f"\n\n报告文件：\n{paths.markdown}\n{paths.html}\n{paths.svg}")
        except Exception as exc:
            self._set_message(f"实验报告生成失败：{exc}", error=True)

    def _capture_runtime_context(self, stage: str) -> None:
        latest = self._comm.latest_frame()
        context = {
            "runtime_state": (self._runtime_state.state.value
                              if self._runtime_state is not None else "not_enabled"),
            "protocol": self._json_safe(self._comm.protocol_status()),
            "latest_telemetry": {
                field: self._json_safe(getattr(latest, field))
                for field in (
                    "speed_actual", "speed_target", "current_actual", "vdc",
                    "temperature", "bus_state", "fault_code", "fault_text",
                    "data_source",
                )
            },
        }
        self.manager.capture_runtime_context(stage, context)

    @classmethod
    def _json_safe(cls, value):
        if is_dataclass(value):
            return cls._json_safe(asdict(value))
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _set_message(self, message: str, *, error: bool = False) -> None:
        self._message.setText(message)
        self._message.setStyleSheet(
            "color: #ef5350;" if error else "color: #66bb6a;"
        )
        if not error:
            QTimer.singleShot(8000, lambda: self._message.setText(""))
