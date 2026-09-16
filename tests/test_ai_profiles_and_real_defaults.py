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
    assert page._max_rpm.minimum() == 1
    assert page._max_rpm.maximum() == 4000
    assert page._target_position.minimum() == -36000.0
    assert page._target_position.maximum() == 36000.0
    assert abs(page._current_limit.value() - 1.887) < 0.001
    assert abs(page._current_limit.maximum() - 4.49) < 0.001
    assert abs(pi.iq_max.value() - 1.887) < 0.001
    assert pi.kp_spd.value() == 1752
    assert pi.ki_spd.value() == 121
    assert pi.kp_cur.value() == 2323
    assert pi.ki_cur.value() == 2077
    assert pi.kp_spd.maximum() == 10000
    assert pi.ki_spd.maximum() == 10000
    assert pi.kp_cur.maximum() == 10000
    assert pi.ki_cur.maximum() == 10000
    position = page._panels["位置三环控制"]
    assert position.kp_pos.maximum() == 1000
    assert position.kd_pos.maximum() == 10
    assert position.kpf_pos.minimum() == -100
    assert position.kpf_pos.maximum() == 100
    assert position.ff_lpf_hz.maximum() == 200
    assert position.position_speed_limit_rpm.maximum() == 4000
    assert position.position_accel_limit_rpm_s.maximum() == 10000
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
    # iq_max 与顶部电流限幅已双向同步；加载时以 max_current_a 为准统一两者
    assert abs(pi.iq_max.value() - 1.6) < 0.001
    assert abs(page._current_limit.value() - 1.6) < 0.001
    assert page._max_rpm.value() == 4000


def test_位置三环方案保存并完整恢复对应模式(tmp_path, monkeypatch):
    _app()
    profile_file = tmp_path / "pi_parameter_profiles.json"
    monkeypatch.setattr(
        "pages.control_page.writable_path", lambda *_parts: profile_file)
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *args, **kwargs: ("三环可用参数", True))
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    page = ControlPage(CommManager())
    page._select_mode("位置三环控制")
    position = page._panels["位置三环控制"]
    position.kp_spd.setValue(1700)
    position.ki_spd.setValue(110)
    position.kp_cur.setValue(2300)
    position.ki_cur.setValue(2000)
    position.kp_pos.setValue(12.0)
    position.kd_pos.setValue(1.2)
    position.kpf_pos.setValue(1.5)
    position.ff_lpf_hz.setValue(20.0)
    position.position_speed_limit_rpm.setValue(2500.0)
    position.position_accel_limit_rpm_s.setValue(1200.0)
    page._target_position.setValue(1080.0)
    page._current_limit.setValue(4.0)

    page._on_save_pi_profile()
    saved = json.loads(profile_file.read_text(encoding="utf-8"))["三环可用参数"]
    assert saved["control_mode"] == "位置三环控制"
    assert saved["kp_pos"] == 12.0
    assert saved["target_position_deg"] == 1080.0

    page._select_mode("闭环PI控制")
    position.kp_pos.setValue(1.0)
    position.kd_pos.setValue(0.0)
    position.kp_spd.setValue(1.0)
    page._on_load_pi_profile()

    assert page._current_mode() == "位置三环控制"
    assert position.kp_spd.value() == 1700
    assert position.ki_spd.value() == 110
    assert position.kp_cur.value() == 2300
    assert position.ki_cur.value() == 2000
    assert position.kp_pos.value() == 12.0
    assert position.kd_pos.value() == 1.2
    assert position.kpf_pos.value() == 1.5
    assert position.ff_lpf_hz.value() == 20.0
    assert position.position_speed_limit_rpm.value() == 2500.0
    assert position.position_accel_limit_rpm_s.value() == 1200.0
    assert page._target_position.value() == 1080.0
    assert position.iq_max.value() == 4.0


def test_旧版方案加载时保持当前三环模式(tmp_path, monkeypatch):
    _app()
    profile_file = tmp_path / "pi_parameter_profiles.json"
    profile_file.write_text(json.dumps({
        "旧位置方案": {
            "kp_spd": 1650.0,
            "ki_spd": 105.0,
            "kp_cur": 2250.0,
            "ki_cur": 1950.0,
            "max_current_a": 3.0,
        }
    }), encoding="utf-8")
    monkeypatch.setattr(
        "pages.control_page.writable_path", lambda *_parts: profile_file)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    page = ControlPage(CommManager())
    page._select_mode("位置三环控制")
    page._pi_profile_combo.setCurrentText("旧位置方案")

    page._on_load_pi_profile()

    position = page._panels["位置三环控制"]
    assert page._current_mode() == "位置三环控制"
    assert position.kp_spd.value() == 1650.0
    assert position.ki_spd.value() == 105.0
    assert position.kp_cur.value() == 2250.0
    assert position.ki_cur.value() == 1950.0
    assert position.iq_max.value() == 3.0
