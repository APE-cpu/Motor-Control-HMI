import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from communications.comm_manager import CommManager, TelemetryFrame
from experiments import SessionStatus
from core import RuntimeStateMachine
from main_window import MainWindow
from pages.control_page import ControlPage
from pages.experiment_page import ExperimentPage


def _app():
    return QApplication.instance() or QApplication([])


def test_界面可以完成创建记录与正常结束(tmp_path, monkeypatch):
    _app()
    comm = CommManager()
    page = ExperimentPage(comm, software_version="test",
                          storage_root=tmp_path / "records")
    page._name.setText("界面转速阶跃")
    page._operator.setText("tester")

    page._on_start()
    session = page.manager.active_session
    assert session is not None
    assert session.status is SessionStatus.RUNNING
    assert page._btn_start.isEnabled() is False
    assert page._btn_complete.isEnabled() is True

    frame = TelemetryFrame()
    frame.speed_actual = 1000
    comm.telemetryReceived.emit(frame)
    page._on_complete()

    loaded = page.manager.load(session.experiment_id)
    assert loaded.status is SessionStatus.COMPLETED
    assert set(loaded.runtime_context) == {"start", "end"}
    assert "protocol" in loaded.runtime_context["end"]
    assert loaded.telemetry_count == 1
    assert loaded.event_count >= 4  # 会话开始、开始操作、结束操作、会话结束
    assert page._status_value.text() == "已完成"
    assert page._btn_curves.isEnabled() is True
    assert page._btn_report.isEnabled() is True
    opened = []
    monkeypatch.setattr(
        "pages.experiment_page.HistoricalTelemetryDialog.exec",
        lambda dialog: opened.append(dialog.summary["samples"]),
    )
    page._open_historical_telemetry()
    assert opened == [1]
    monkeypatch.setattr(
        "pages.experiment_page.ExperimentConclusionDialog.exec",
        lambda dialog: QDialog.Accepted)
    monkeypatch.setattr(
        "pages.experiment_page.ExperimentConclusionDialog.conclusion",
        lambda dialog: {
            "result_status": "passed", "observations": "跟踪稳定",
            "anomalies": "", "recommendations": "保持参数",
            "next_plan": "负载阶跃",
        })
    page._edit_selected_conclusion()
    assert page.manager.load(session.experiment_id).conclusion["result_status"] == "passed"
    page._generate_selected_report()
    report_dir = page.manager.repository.session_dir(session.experiment_id) / "report"
    assert (report_dir / "report.md").is_file()
    assert (report_dir / "report.html").is_file()
    assert (report_dir / "telemetry.svg").is_file()
    page.shutdown()


def test_关闭页面自动中止运行实验并保留数据(tmp_path):
    _app()
    comm = CommManager()
    page = ExperimentPage(comm, storage_root=tmp_path / "records")
    page._name.setText("关闭恢复测试")
    page._on_start()
    session = page.manager.active_session
    comm.telemetryReceived.emit(TelemetryFrame())

    page.shutdown()

    loaded = page.manager.load(session.experiment_id)
    assert loaded.status is SessionStatus.ABORTED
    assert loaded.end_reason == "上位机软件关闭"
    assert loaded.telemetry_count == 1
    assert page.recorder.is_attached is False


def test_运行中快捷事件冻结现场且自定义说明必填(tmp_path):
    _app()
    comm = CommManager()
    frame = TelemetryFrame()
    frame.speed_actual = 888
    frame.current_actual = 2.3
    frame.vdc = 47.5
    comm._latest_frame = frame
    page = ExperimentPage(comm, storage_root=tmp_path / "records")
    page._name.setText("事件标记实验")
    page._on_start()
    session = page.manager.active_session

    page._marker_type.setCurrentIndex(page._marker_type.findData("custom"))
    page._record_quick_marker()
    assert "必须填写" in page._message.text()
    page._marker_note.setText("观察到轻微机械共振")
    page._record_quick_marker()

    events = page.manager.repository.read_events(session.experiment_id)
    marker = next(item for item in events if item["type"] == "experiment_marker")
    assert marker["message"] == "观察到轻微机械共振"
    assert marker["details"]["snapshot"]["speed_actual"] == 888
    assert marker["details"]["snapshot"]["vdc"] == 47.5
    page.shutdown()


def test_主窗口包含实验管理页面且导航索引正确(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    window = MainWindow(enable_training=False)

    assert window.stack.count() == 11
    assert window.stack.indexOf(window.experiment_page) == 8
    assert window.stack.indexOf(window.operation_log_page) == 9
    assert window.stack.indexOf(window.manual_page) == 10
    window.nav.select_page(8)
    assert window.stack.currentWidget() is window.experiment_page
    window.close()


def test_历史列表可在软件重启后只读打开(tmp_path):
    _app()
    root = tmp_path / "records"
    first_page = ExperimentPage(CommManager(), storage_root=root)
    first_page._name.setText("历史回看实验")
    first_page._purpose.setPlainText("验证重新打开")
    first_page._on_start()
    experiment_id = first_page.manager.active_session.experiment_id
    first_page._on_complete()
    first_page.shutdown()

    reopened = ExperimentPage(CommManager(), storage_root=root)
    assert reopened._history.rowCount() == 1
    assert reopened._history.item(0, 0).text() == experiment_id
    reopened._history.selectRow(0)

    detail = reopened._history_detail.toPlainText()
    assert "历史回看实验" in detail
    assert "验证重新打开" in detail
    assert "已完成" in detail
    assert reopened.manager.active_session is None
    assert reopened._btn_complete.isEnabled() is False
    reopened.shutdown()


def test_控制页快照自动冻结到实验档案(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr("pages.control_page.load_motor_info", lambda: {
        "rated": {"power_W": 78.0, "voltage_V": 24.0},
        "measured": {"Rs_ohm": 1.25},
        "description": "实验电机",
    })
    comm = CommManager()
    control = ControlPage(comm)
    control._motor_model.setText("PMSM-78W-A")
    control._pole_pairs.setValue(5)
    control._max_rpm.setValue(3200)
    control._target_speed.setValue(1800)
    control._load_type.setCurrentIndex(1)
    control._load_value.setValue(0.25)

    page = ExperimentPage(
        comm,
        storage_root=tmp_path / "records",
        snapshot_provider=control.experiment_snapshot,
    )
    assert page._device_name.text() == "PMSM-78W-A"
    assert page._rated_power.value() == 78.0
    page._name.setText("自动快照实验")
    page._bus_voltage.setValue(48.0)
    page._on_start()
    experiment_id = page.manager.active_session.experiment_id
    page._on_complete()

    loaded = page.manager.load(experiment_id)
    assert loaded.device.name == "PMSM-78W-A"
    assert loaded.device.rated_power_w == 78.0
    assert loaded.device.dc_bus_voltage_v == 48.0
    assert loaded.device.sensors == ["增量式编码器(QEP)"]
    assert loaded.device.extra["pole_pairs"] == 5
    assert loaded.controller_params["control_mode"] == "闭环PI控制"
    assert loaded.controller_params["target_speed_rpm"] == 1800
    assert loaded.controller_params["mechanical_load"]["load_value"] == 0.25
    assert loaded.protection_params["max_rpm"] == 3200
    assert "iq_max" in loaded.protection_params
    page.shutdown()


def test_不同控制方式导出各自参数(tmp_path):
    _app()
    control = ControlPage(CommManager())
    control._select_mode("模型预测控制(MPC)")
    panel = control._panels["模型预测控制(MPC)"]
    panel.N.setValue(17)
    panel.umax.setValue(30.0)

    snapshot = control.experiment_snapshot()
    assert snapshot["controller_params"]["control_mode"] == "模型预测控制(MPC)"
    assert snapshot["controller_params"]["mode_params"]["prediction_horizon"] == 17
    assert snapshot["protection_params"]["u_max"] == 30.0


def test_实验页按模板逐步引导并阻止提前正常结束(tmp_path):
    _app()
    comm = CommManager()
    page = ExperimentPage(comm, storage_root=tmp_path / "records")
    built_in_index = page._template_combo.findData("TPL-BUILTIN-78W-BASELINE")
    page._template_combo.setCurrentIndex(built_in_index)
    page._apply_selected_template()

    assert "执行运行预检" in page._template_steps.toPlainText()
    page._on_start()
    session = page.manager.active_session
    assert session.template_id == "TPL-BUILTIN-78W-BASELINE"
    assert "步骤 1/7" in page._workflow_step.text()

    page._on_complete()
    assert page.manager.active_session is not None
    assert "仍有必做步骤" in page._message.text()

    for index in range(7):
        page._workflow_note.setText(f"步骤{index + 1}验证完成")
        page._confirm_workflow_step()
    assert "7/7" in page._workflow_step.text()
    page._on_complete()

    loaded = page.manager.load(session.experiment_id)
    assert loaded.status is SessionStatus.COMPLETED
    assert loaded.workflow_completed_steps == [
        "S01", "S02", "S03", "S04", "S05", "S06", "S07"]
    assert loaded.event_count >= 9
    page.shutdown()


def test_引导步骤检查运行状态且运行中不能正常归档(tmp_path):
    _app()
    machine = RuntimeStateMachine()
    page = ExperimentPage(
        CommManager(), storage_root=tmp_path / "records", runtime_state=machine)
    index = page._template_combo.findData("TPL-BUILTIN-78W-BASELINE")
    page._template_combo.setCurrentIndex(index)
    page._apply_selected_template()
    page._on_start()

    page._confirm_workflow_step()  # S01
    page._confirm_workflow_step()  # S02
    page._confirm_workflow_step()  # S03要求READY
    assert "要求运行状态为 ready" in page._message.text()
    assert page.manager.active_session.workflow_current_index == 2

    machine.connection_changed(True, "测试连接")
    machine.begin_precheck()
    machine.pass_precheck()
    page._confirm_workflow_step()
    page._confirm_workflow_step()  # S04要求RUNNING，当前仍READY
    assert page.manager.active_session.workflow_current_index == 3
    machine.confirm_started()
    page._confirm_workflow_step()
    page._on_complete()
    assert "尚未安全停止" in page._message.text()
    assert page.manager.active_session is not None
    page.shutdown()
