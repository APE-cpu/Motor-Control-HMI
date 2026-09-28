import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from communications.comm_manager import CommManager
from experiments import ExperimentSessionManager, SessionStatus
from experiments.exports import ExperimentExports
from pages.experiment_page import ExperimentPage


@pytest.fixture
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_operator_and_draft_survive_restart_without_starting_experiment(app, tmp_path):
    page = ExperimentPage(CommManager(), storage_root=tmp_path)
    page._name.setText("阶跃测试")
    page._operator.setText("张工")
    page._purpose.setPlainText("观察速度纹波")
    QTest.qWait(550)
    assert json.loads(page._draft_path.read_text(encoding="utf-8"))["operator"] == "张工"
    assert page.manager.active_session is None
    page.shutdown()
    page.deleteLater()
    reopened = ExperimentPage(CommManager(), storage_root=tmp_path)
    assert reopened._name.text() == "阶跃测试"
    assert reopened._operator.text() == "张工"
    assert reopened._purpose.toPlainText() == "观察速度纹波"
    reopened.shutdown()
    reopened.deleteLater()


def test_versions_are_received_identity_not_whitelist(app, tmp_path):
    comm = CommManager()
    page = ExperimentPage(comm, storage_root=tmp_path)
    original = page._expected_device_id.text()
    comm.protocolSessionChanged.emit({"session_state": "ready",
        "hardware_version": "HW-B", "firmware_version": "FW-2", "device_id": "test"})
    assert "HW-B" in page._actual_identity.text()
    assert "FW-2" in page._actual_identity.text()
    assert page._expected_device_id.text() == original
    comm.protocolSessionChanged.emit({"session_state": "idle"})
    assert "HW-B" not in page._actual_identity.text()
    page.shutdown()
    page.deleteLater()


def test_late_export_completion_stays_with_original_experiment(tmp_path):
    manager = ExperimentSessionManager(tmp_path)
    exports = ExperimentExports(manager)
    original = manager.create_session("原实验", operator="张工")
    manager.start()
    ticket = exports.prepare("速度", {}, {"gain": 1})
    (ticket.directory / "原始数据.csv").write_text("data", encoding="utf-8")
    manager.complete()
    newer = manager.create_session("新实验")
    manager.start()
    exports.finish(ticket, {})
    assert manager.active_session.experiment_id == newer.experiment_id
    events = manager.repository.read_events(original.experiment_id)
    assert events[-1]["type"] == "waveform_saved"
    assert not any(e["type"] == "waveform_saved" for e in manager.repository.read_events(newer.experiment_id))
    manifest = json.loads((ticket.directory / "export.json").read_text(encoding="utf-8"))
    assert manifest["files"] == ["原始数据.csv"]
    assert manifest["snapshot"] == {"gain": 1}


def test_export_failure_is_not_reported_as_completed_experiment(tmp_path):
    manager = ExperimentSessionManager(tmp_path)
    exports = ExperimentExports(manager)
    ticket = exports.prepare("波形", {"name": "快照", "operator": "张工"}, {})
    exports.finish(ticket, {"error": "disk full"})
    assert manager.active_session is None
    saved = manager.load(ticket.experiment_id)
    assert saved.status == SessionStatus.ABORTED
    assert saved.operator == "张工"
    manifest = json.loads((ticket.directory / "export.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_monitor_saves_waveforms_in_experiment_folder_without_overwrite(app, tmp_path, monkeypatch):
    from pages.monitor_page import MonitorPage
    comm = CommManager()
    archive = ExperimentPage(comm, storage_root=tmp_path)
    archive._name.setText("速度阶跃")
    archive._operator.setText("张工")
    archive._on_start()
    session_id = archive.manager.active_session.experiment_id
    monitor = MonitorPage(comm)
    monitor._timer.stop()
    monitor.export_archive = archive
    monitor._c_phase_current.append_columns({"Ia": [0.1, 0.2], "Ib": [-0.1, -0.2]}, 0.001)
    monkeypatch.setattr(monitor, "render_waveforms_png", lambda: b"test-image")
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    for _ in range(2):
        monitor._save_all_curves()
        assert monitor._save_worker.wait(5000)
        app.processEvents()
        assert monitor._export_ticket is None
    folders = list(archive.manager.repository.session_dir(session_id).glob("waveforms/*"))
    assert len(folders) == 2
    for folder in folders:
        assert (folder / "波形.png").read_bytes() == b"test-image"
        assert "phase_current" in (folder / "原始数据.csv").read_text(encoding="utf-8-sig")
        assert json.loads((folder / "export.json").read_text(encoding="utf-8"))["status"] == "saved"
    assert monitor._curve_tabs.count() == 5
    assert all(not monitor._curve_tabs.tabIcon(i).isNull() for i in range(5))
    monitor._angle_dial.feed(-450)
    assert monitor._angle_dial._disp == 270
    assert monitor._angle_dial._revs == -1.25
    monitor._angle_dial.grab()  # exercise the actual painter
    archive._on_complete()
    archive._refresh_history()
    assert archive._btn_history_folder.isEnabled()
    assert "已完成" in archive._history_detail.toPlainText()
    archive.shutdown()
    monitor.close()
    monitor.deleteLater()
    archive.deleteLater()


def test_sampling_export_creates_completed_snapshot_without_starting_recording(app, tmp_path, monkeypatch):
    from pages.current_sampling_page import CurrentSamplingPage
    comm = CommManager()
    archive = ExperimentPage(comm, storage_root=tmp_path)
    archive._operator.setText("李工")
    page = CurrentSamplingPage(comm)
    page.export_archive = archive
    page._history.append({"tick_ms": 10, "adc1_raw": 2048})
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    page._save_csv()
    assert archive.manager.active_session is None
    saved, = archive.manager.repository.list_sessions()
    assert saved.status == SessionStatus.COMPLETED
    assert saved.operator == "李工"
    assert "波形快照" in saved.name
    files = list(archive.manager.repository.session_dir(saved.experiment_id).glob("waveforms/*/采样诊断.csv"))
    assert len(files) == 1
    assert "2048" in files[0].read_text(encoding="utf-8-sig")
    assert archive._btn_history_folder.isEnabled()
    archive.shutdown()
    archive.deleteLater()
    page.close()
    page.deleteLater()


def test_hint_preference_persists_and_does_not_hide_runtime_status(app, tmp_path):
    from ui_theme import ThemeManager
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.IniFormat)
    manager = ThemeManager(app, settings)
    card = QLabel("说明")
    status = QLabel("运行状态")
    status.show()
    manager.register_hint(card)
    manager.set_hints_visible(False)
    assert card.isHidden()
    assert not status.isHidden()
    settings.sync()
    restored = ThemeManager(app, QSettings(str(tmp_path / "appearance.ini"), QSettings.IniFormat))
    assert restored.hints_visible is False
    manager.set_hints_visible(True)
    assert not card.isHidden()
    for item in (manager, restored):
        app.removeEventFilter(item)
        item.deleteLater()
    card.close()
    status.close()
    card.deleteLater()
    status.deleteLater()
