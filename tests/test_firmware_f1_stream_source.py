import re
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
    project = (FIRMWARE_ROOT / "MDK-ARM" / "PMSM_control.uvprojx").read_text(
        encoding="utf-8")
    power_stage = (FIRMWARE_ROOT / "Inc" /
                   "power_stage_parameters.h").read_text(encoding="utf-8")
    host_parser = (Path(__file__).resolve().parents[1] / "native_core" / "src" /
                   "telemetry_processor.cpp").read_text(encoding="utf-8")

    assert "V2_F1_RING_SAMPLES" in app
    assert "v2_f1_capture_isr" in app
    assert "f1_ring_pop" in app
    assert "s_f1_stream_rate_hz" in app
    assert "f1_rate_hz" in app
    assert "v2_f1_capture_isr(" in tasks
    assert "V2_F1_FORMAT_TAG 0xF140u" in app
    assert "V2_HIGHSPEED_SAMPLE_BYTES 40u" in app
    assert "put_u16_le(&sample[36], s_vdda_mv)" in app
    assert "put_u16_le(&sample[28], duty_a)" in app
    assert "put_u16_le(&hs[28], PWM_Handle_M1._Super.CntPhA)" in app
    assert "put_u16_le(&hs[38], V2_F1_FORMAT_TAG)" in app
    assert "one 640-byte payload per millisecond" in app
    assert "pwmcHandle[M1]->CntPhA" in tasks
    assert "FOCVars[M1].hElAngle" in tasks
    assert "v2_vdda_poll(now);" in app
    assert "s_f1_sample_sequence++" in app
    assert "VBS_GetAvBusVoltage_d" in app
    assert "s_f1_cached_vbus_d" in app
    # 野火板用 yh_bus_voltage_sensor.c 覆盖了 MCSDK 的母线换算（1.65 V 偏置、×37），
    # 生成的 VBUS_PARTITIONING_FACTOR=0.027 在该板上不生效；主机两套解析必须与覆盖版一致。
    yh_bus = (FIRMWARE_ROOT / "USER" / "YH_MotorControl" / "Inc" /
              "yh_r_divider_bus_voltage_sensor.h").read_text(encoding="utf-8", errors="ignore")
    assert re.search(r"VBUS_MAGNIFICATION_TIMES\s+37\.0", yh_bus)
    assert re.search(r"VBUS_VBIAS\s+1\.65", yh_bus)
    assert "kBusBiasV = 1.65" in host_parser and "kBusGain = 37.0" in host_parser
    assert "kBusPartitioningFactor" not in host_parser
    assert "FOCVars[M1].Iqd.q, FOCVars[M1].Iqd.d" in tasks
    assert "s_v2_park_angle_m1" in tasks
    assert "static volatile uint8_t s_rls_probe_enabled" in app
    assert "s_rls_probe_enabled = 0u" in app
    assert "v2_probe_lfsr15_step" in app
    assert "v2_probe_lfsr16_step" in app
    assert "V2_RLS_PROBE_DEFAULT_AMPLITUDE_DIGITS 190u" in app
    assert "rls_probe_amplitude_a outside 0.02..0.15 A" in app
    assert "v2_foc_ident_probe_disable();" in app
    assert "s_v2_iqdref_used_m1" in tasks
    assert "v2_rls_update" not in tasks
    assert "v2_rls.c" not in project
    # TCP/IP must stay out of the 16 kHz ISR path.
    capture = app.split("void v2_f1_capture_isr(", 1)[1].split("\n}", 1)[0]
    assert "v2_proto_send" not in capture
