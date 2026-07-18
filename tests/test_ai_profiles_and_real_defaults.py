import json

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from pages.ai_page import AIPage
from pages.control_page import ControlPage
from PySide6.QtWidgets import QInputDialog, QMessageBox


def _app():
    return QApplication.instance() or QApplication([])


def test_ai模型档案可分别保存和切换(tmp_path, monkeypatch):
    _app()
    config = tmp_path / "ai_config.json"
    monkeypatch.setattr("pages.ai_page._CONFIG_FILE", config)
    page = AIPage(CommManager())
    page._profile.setCurrentText("Kimi")
    page._api_key.setText("sk-kimi-test")
    page._model.setText("kimi-k2.6")
    page._on_save_config()
    page._profile.setCurrentText("Qwen")
    page._api_key.setText("sk-qwen-test")
    page._on_save_config()

    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["selected_profile"] == "Qwen"
    assert saved["profiles"]["Kimi"]["api_key"] == "sk-kimi-test"
    assert saved["profiles"]["Qwen"]["model"] == "qwen3.7-plus"

    page._profile.setCurrentText("Kimi")
    assert page._api_key.text() == "sk-kimi-test"
    assert page._model.text() == "kimi-k2.6"


def test_控制页默认参数与当前真机固件一致(monkeypatch):
    _app()
    monkeypatch.setattr("pages.control_page.load_motor_info", lambda: {})
    page = ControlPage(CommManager())
    pi = page._panels["闭环PI控制"]

    assert page._motor_model.text() == "野火 78W PMSM"
    assert page._max_rpm.value() == 4000
    assert abs(page._current_limit.value() - 1.887) < 0.001
    assert pi.kp_spd.value() == 1752
    assert pi.ki_spd.value() == 121
    assert pi.kp_cur.value() == 2323
    assert pi.ki_cur.value() == 2077
    qep = page._sensor_panels["增量式编码器(QEP)"]
    assert qep.lines.value() == 1000
    assert qep.dir.currentIndex() == 1
    assert qep.idx.isChecked() is False


def test_PI参数方案可保存并重新加载(tmp_path, monkeypatch):
    _app()
    profile_file = tmp_path / "pi_parameter_profiles.json"
    monkeypatch.setattr(
        "pages.control_page.writable_path", lambda *_parts: profile_file)
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *args, **kwargs: ("实验可用参数", True))
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    page = ControlPage(CommManager())
    pi = page._panels["闭环PI控制"]
    pi.kp_spd.setValue(1600)
    pi.ki_spd.setValue(100)
    pi.kp_cur.setValue(2200)
    pi.ki_cur.setValue(1900)
    pi.iq_max.setValue(1.5)
    page._current_limit.setValue(1.6)
    page._max_rpm.setValue(4000)

    page._on_save_pi_profile()
    assert profile_file.exists()

    pi.kp_spd.setValue(1)
    pi.ki_spd.setValue(1)
    page._on_load_pi_profile()
    assert pi.kp_spd.value() == 1600
    assert pi.ki_spd.value() == 100
    assert pi.kp_cur.value() == 2200
    assert pi.ki_cur.value() == 1900
    assert pi.iq_max.value() == 1.5
    assert page._current_limit.value() == 1.6
    assert page._max_rpm.value() == 4000
