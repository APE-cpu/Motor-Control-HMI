from pathlib import Path

import pytest


FIRMWARE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "PMSM-FOC-F407-V2"
    / "New_PMSM_FOC_V5.44_Encoder_speed_control_new_driver_board"
    / "PMSM_control"
)


def test_固件F1连续流由16kHz中断环形缓冲生产且主循环批量发送():
    if not FIRMWARE_ROOT.exists():
        pytest.skip("F407 firmware workspace is not checked out beside the host app")
    app = (FIRMWARE_ROOT / "Src" / "v2_foc_app.c").read_text(
        encoding="utf-8")
    tasks = (FIRMWARE_ROOT / "Src" / "mc_tasks.c").read_text(
        encoding="utf-8")

    assert "V2_F1_RING_SAMPLES" in app
    assert "v2_f1_capture_isr" in app
    assert "f1_ring_pop" in app
    assert "s_f1_stream_rate_hz" in app
    assert "f1_rate_hz" in app
    assert "v2_f1_capture_isr(" in tasks
    # TCP/IP must stay out of the 16 kHz ISR path.
    capture = app.split("void v2_f1_capture_isr(", 1)[1].split("\n}", 1)[0]
    assert "v2_proto_send" not in capture
