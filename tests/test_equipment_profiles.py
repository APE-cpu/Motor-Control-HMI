import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from experiments import EquipmentProfileRepository
from pages.experiment_page import ExperimentPage


def _app():
    return QApplication.instance() or QApplication([])


def test_设备档案以新修订保存且不覆盖历史(tmp_path):
    repository = EquipmentProfileRepository(tmp_path)
    built_in = repository.load("DEV-BUILTIN-78W-R001")

    revision2 = repository.create_revision(
        built_in.name, family_id=built_in.family_id,
        motor_type="PMSM", rated_power_w=78,
        nominal_bus_voltage_v=48, inverter="INV-A", controller="CTRL-A",
        sensors=["QEP"], expected_device_id="BOARD-001",
        expected_hardware_version="HW-A", expected_firmware_prefix="fw-2.",
        safety_limits={"max_rpm": 2500}, notes="实测修订")
    revision3 = repository.create_revision(
        built_in.name, family_id=built_in.family_id,
        motor_type="PMSM", expected_device_id="BOARD-002")

    assert revision2.profile_id == "DEV-BUILTIN-78W-R002"
    assert revision3.profile_id == "DEV-BUILTIN-78W-R003"
    assert repository.load(revision2.profile_id).expected_device_id == "BOARD-001"
    assert (repository.load(built_in.profile_id).expected_device_id ==
            "EBF-F407-JIAOYANG-PMSM-001")


def test_实验开始时冻结设备档案修订和期望身份(tmp_path):
    _app()
    comm = CommManager()
    page = ExperimentPage(comm, storage_root=tmp_path / "records")
    built_in_index = page._equipment_combo.findData("DEV-BUILTIN-78W-R001")
    page._equipment_combo.setCurrentIndex(built_in_index)
    page._apply_selected_equipment()
    page._expected_device_id.setText("BOARD-78W-001")
    page._expected_hardware.setText("CTRL-REV-A")
    page._expected_firmware_prefix.setText("pmsm-2.")
    page._save_equipment_revision()

    profile_id = page._equipment_combo.currentData()
    assert profile_id == "DEV-BUILTIN-78W-R002"
    expected = comm.protocol_status()["expected_identity"]
    assert expected["device_id"] == "BOARD-78W-001"

    page._name.setText("设备档案冻结测试")
    page._on_start()
    session = page.manager.active_session
    assert session is not None
    assert session.device.extra["equipment_profile_id"] == profile_id
    assert session.device.extra["equipment_revision"] == 2
    assert session.device.extra["expected_device_identity"]["device_id"] == "BOARD-78W-001"
    page.shutdown()
    page.close()
    page.deleteLater()
