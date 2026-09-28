import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from core.phasor_decomposition import synthetic_capture
from core.power_estimate import park_currents
from widgets.media_recorder import MediaRecorder, append_media_manifest, read_media_manifest


def _app():
    return QApplication.instance() or QApplication([])


def test_media_manifest_round_trip(tmp_path):
    append_media_manifest(tmp_path, {"file": "录像_1.mp4", "start_offset_s": 1.0})
    append_media_manifest(tmp_path, {"file": "录音_2.m4a", "start_offset_s": 5.0})
    assert [e["file"] for e in read_media_manifest(tmp_path)] == ["录像_1.mp4", "录音_2.m4a"]
    (tmp_path / "media.json").write_text("损坏", encoding="utf-8")
    assert read_media_manifest(tmp_path) == []


def test_media_time_offsets_accept_session_timezone():
    # 实验会话时间带时区，录制时间也必须能与之相减（2026-09-28 现场异常）
    from widgets.media_recorder import _iso_now, _offset_s
    now = _iso_now()
    assert "+" in now or "-" in now[19:]
    assert _offset_s("2026-09-28T23:05:46.817+08:00", "2026-09-28T23:05:50.817+08:00") == 4.0
    assert _offset_s("2026-09-28T23:05:46.817+08:00", now) is not None
    assert _offset_s("2026-09-28T23:05:46", now) is not None          # 旧的无时区记录


def test_experiment_start_survives_media_failure(tmp_path, monkeypatch):
    """录像出错不能让实验显示“启动失败”，实验照常记录。"""
    _app()
    from communications.comm_manager import CommManager
    from pages.experiment_page import ExperimentPage
    page = ExperimentPage(CommManager(), storage_root=tmp_path)

    def boom(*_args):
        raise RuntimeError("camera busy")

    monkeypatch.setattr(page.media_panel, "start_for_session", boom)
    page._on_start()
    assert page.manager.active_session is not None
    page._on_complete()
    assert page.manager.repository.list_sessions()[0].status.value == "completed"
    page.shutdown()


def test_log_page_lists_experiments_created_after_it_was_built(tmp_path):
    _app()
    import os
    os.environ["HMI_NO_WEBENGINE"] = "1"
    from experiments.session_manager import ExperimentSessionManager
    from pages.experiment_log_page import ExperimentLogPage
    manager = ExperimentSessionManager(tmp_path / "records")
    page = ExperimentLogPage(manager.repository)
    assert page._experiments.count() == 0
    manager.create_session("后来做的实验")
    manager.start()
    manager.complete()
    page.show()                                          # 切到本页时刷新
    assert page._experiments.count() == 1
    page.close()


def test_recorder_without_devices_fails_gracefully(tmp_path):
    _app()
    recorder = MediaRecorder()
    errors = []
    recorder.errorOccurred.connect(errors.append)
    recorder.configure(None, None)                      # 不选设备：不打开摄像头/麦克风
    assert recorder.start(tmp_path) is None
    assert not recorder.recording and (not recorder.available or errors)
    assert recorder.stop() is None


def test_experiment_name_defaults_to_date_and_media_tab_exists(tmp_path):
    _app()
    from communications.comm_manager import CommManager
    from pages.experiment_page import ExperimentPage, _default_experiment_name
    page = ExperimentPage(CommManager(), storage_root=tmp_path)
    assert page._name.text() == _default_experiment_name()
    tabs = [page._workspace_tabs.tabText(i) for i in range(page._workspace_tabs.count())]
    assert "音视频" in tabs
    page._name.clear()
    page._on_start()                                    # 名称为空也能开始，自动用日期
    session = page.manager.active_session
    assert session is not None and session.name[:4].isdigit()
    page._on_complete()
    assert page._name.text() == _default_experiment_name()
    page.shutdown()


def test_power_flow_offline_playback_drives_sankey(tmp_path):
    _app()
    from communications.comm_manager import CommManager
    from pages.power_flow_page import PowerFlowPage
    capture = synthetic_capture(fe_hz=50.0, duration_s=1.0, amplitude_a=2.0)
    _i_d, iq = park_currents(capture.ia, capture.ib, capture.theta)
    angle = np.degrees(capture.theta) % 360.0
    rows = ["time_s,angle_deg,speed_rpm,iq_a,ia_a,ib_a,vd_raw,vq_raw,vbus_v"]
    for i in range(capture.time.size):
        rows.append(f"{capture.time[i]:.7f},{angle[i]:.3f},750,{iq[i]:.5f},{capture.ia[i]:.5f},"
                    f"{capture.ib[i]:.5f},0,6000,24")
    path = tmp_path / "高速数据.csv"
    path.write_text("\n".join(rows), encoding="utf-8")
    page = PowerFlowPage(CommManager())
    page._chk_offline.setChecked(True)
    assert not page._chk_enabled.isEnabled()
    assert page._playback.load_file(str(path))
    page._playback.seek(0.5)
    assert not page._diagram.idle
    assert page._diagram._powers["inv"] == pytest.approx(
        1.5 * 6000 / 32767 * 24 / np.sqrt(3) * np.mean(iq), rel=0.05)
    page._chk_offline.setChecked(False)
    assert page._diagram.idle and page._chk_enabled.isEnabled()
    page.close()
