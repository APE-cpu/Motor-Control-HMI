from datetime import datetime

from waveform_storage import category_for_control_mode, create_waveform_record_dir


def test_control_modes_have_stable_waveform_categories():
    assert category_for_control_mode("闭环PI控制") == "速度闭环PI"
    assert category_for_control_mode("位置三环控制") == "位置三环"
    assert category_for_control_mode("开环控制") == "电流环测试"
    assert category_for_control_mode(None) == "其他实验"


def test_each_save_gets_an_independent_record_directory(tmp_path):
    moment = datetime(2026, 9, 3, 10, 34, 48)
    first = create_waveform_record_dir("速度闭环PI", moment, tmp_path)
    second = create_waveform_record_dir("速度闭环PI", moment, tmp_path)

    assert first.name == "实验_20260903_103448"
    assert second.name == "实验_20260903_103448_2"
    assert first.parent == tmp_path / "速度闭环PI" / "2026-09-03"
    assert first.is_dir()
    assert second.is_dir()
