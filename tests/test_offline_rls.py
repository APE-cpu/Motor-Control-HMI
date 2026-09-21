import math
import struct

import pytest

from communications import native_telemetry
from rls_offline import (
    assess_rls_identifiability, cross_check_arx7_motor_projection,
    derive_arx7_motor_parameter_diagnostic, identify_physical_dq_iv,
    resolve_voltage_columns_si, validate_rls_columns,
)
from tools.stress_physical_rls import simulate as simulate_physical_rls


def _columns(count=2):
    return {
        "count": count,
        "rate_hz": 16000,
        "tick_ms": [10] * count,
        "angle_deg": [90.0, 180.0][:count],
        "speed_rpm": [500.0, 501.0][:count],
        "iq_a": [1.0, -1.0][:count],
        "iqref_a": [1.1, -1.1][:count],
        "ia_a": [2.0, -2.0][:count],
        "ib_a": [-0.5, 0.5][:count],
        "vd_raw": [100.0, -100.0][:count],
        "vq_raw": [200.0, -200.0][:count],
        "vbus_v": [24.0, 25.0][:count],
    }


def test_物理辨识在真机级噪声量化与母线纹波下仍恢复标称参数():
    result = simulate_physical_rls(
        duration_s=5.0, amplitude_a=0.12, noise_std_a=0.20, seed=0)

    assert result["verdict"] == "usable", result["reasons"]
    assert result["nominal_match"] is True, result["nominal_reasons"]
    assert result["rd_ohm"] == pytest.approx(0.59, abs=0.07)
    assert result["rq_ohm"] == pytest.approx(0.59, abs=0.07)
    assert result["ld_mh"] == pytest.approx(0.66, abs=0.07)
    assert result["lq_mh"] == pytest.approx(0.66, abs=0.07)
    assert result["d_coherence"] > 0.6
    assert result["q_coherence"] > 0.6


def test_离线RLS拒绝缺少母线电压或错位的数据():
    missing = _columns()
    missing.pop("vbus_v")
    with pytest.raises(ValueError, match="vbus_v"):
        validate_rls_columns(missing)

    misaligned = _columns()
    misaligned["iq_a"] = [1.0]
    with pytest.raises(ValueError, match="iq_a"):
        validate_rls_columns(misaligned)


def test_Simulink重放明确标注与当前F407电机参数不兼容():
    metadata = native_telemetry.simulink_reference_model_metadata(16000)

    assert metadata["purpose"] == "simulation_replay_only"
    assert metadata["physical_hardware_compatible"] is False
    assert metadata["simulink_model"]["resistance_ohm"] == 2.34
    assert metadata["simulink_model"]["ld_mh"] == 19.36
    assert metadata["simulink_model"]["sample_rate_hz"] == 100000
    assert metadata["f407_hardware"]["resistance_ohm"] == 0.59
    assert metadata["f407_hardware"]["ld_mh"] == 0.66
    assert "不能解释为真机参数" in metadata["warning"]


def test_离线辨识优先使用PWM重构施加电压():
    columns = _columns()
    columns.update({
        "vd_applied_v": [1.25, -1.5],
        "vq_applied_v": [2.5, -2.75],
        "applied_voltage_source_direct": True,
    })

    vd_v, vq_v, source = resolve_voltage_columns_si(columns)

    assert source == "pwm_duty_reconstructed"
    assert list(vd_v) == pytest.approx([1.25, -1.5])
    assert list(vq_v) == pytest.approx([2.5, -2.75])


def test_七参数ARX一阶矩投影可恢复一阶RL但不能虚构磁链():
    sample_time = 1e-3
    resistance = 2.0
    inductance = 20e-3
    a = 1.0 - resistance * sample_time / inductance
    b = sample_time / inductance
    result = derive_arx7_motor_parameter_diagnostic(
        (a, 0.0, 0.0, b, 0.0, 0.0, 0.0),
        (a, 0.0, 0.0, 0.0, 0.0, b, 0.0),
        sample_time_s=sample_time)

    assert result["verdict"] == "diagnostic_only"
    assert result["d_axis"]["resistance_ohm"] == pytest.approx(resistance)
    assert result["q_axis"]["resistance_ohm"] == pytest.approx(resistance)
    assert result["d_axis"]["inductance_mh"] == pytest.approx(20.0)
    assert result["q_axis"]["inductance_mh"] == pytest.approx(20.0)
    assert result["flux"]["identifiable"] is False
    assert result["flux"]["value_wb"] is None


def test_七参数ARX投影与独立dq结果不一致时校验明确失败():
    projection = derive_arx7_motor_parameter_diagnostic(
        (0.9, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0),
        (0.9, 0.0, 0.0, 0.0, 0.0, 0.05, 0.0),
        sample_time_s=1e-3)
    check = cross_check_arx7_motor_projection(
        projection,
        legacy_evidence={
            "verdict": "diagnostic_only",
            "resistance_ohm_median": 0.59,
            "shared_inductance_mh_median": 0.66,
        })

    assert check["reference_source"] == \
        "legacy_quasi_steady_voltage_equation"
    assert check["rl_match"] is False
    assert check["full_three_parameter_validation"] is False


def test_q轴反电动势与电流相关时会被ARX吸收为附加电阻():
    projection = {
        "d_axis": {"valid": False},
        "q_axis": {"valid": True, "resistance_ohm": 1.305,
                   "inductance_mh": 0.913},
    }
    check = cross_check_arx7_motor_projection(
        projection,
        legacy_evidence={
            "verdict": "diagnostic_only",
            "resistance_ohm_median": 0.539,
            "shared_inductance_mh_median": 0.913,
            "flux_wb_median": 0.00585,
            "omega_iq_slope_rad_s_per_a_median": 130.94,
            "omega_iq_correlation_median": 0.997,
        }, flux_reference_wb=0.00585)

    absorption = check["back_emf_absorption"]
    assert absorption["available"] is True
    assert absorption["absorbed_resistance_ohm"] == pytest.approx(
        0.00585 * 130.94)
    assert absorption["predicted_apparent_resistance_ohm"] == \
        pytest.approx(1.305, abs=0.001)
    assert abs(absorption["relative_prediction_error"]) < 0.001


def test_离线RLS把原始采样换算成Simulink的dq输入后直接交给Cpp(monkeypatch):
    calls = []

    def fake_run(id_a, iq_a, ud_v, uq_v, rate_hz, report_every,
                 nominal_inductance_h, exact_simulink_reference):
        calls.append((id_a, iq_a, ud_v, uq_v, rate_hz, report_every))
        return [{"updates": 1, "sample_index": 1,
                 "theta_d": (0.9, 0, 0, 0.1, 0.01, 0, 0),
                 "theta_q": (0.9, 0, 0, 0, 0, 0.1, 0.01)}]

    class FakeNative:
        run_simulink_rls = staticmethod(fake_run)

    monkeypatch.setattr(native_telemetry, "_native", FakeNative())
    results = native_telemetry.run_offline_rls(_columns())

    assert results[0]["updates"] == 1
    assert results[0]["tick_ms"] == 10
    assert len(calls) == 1
    id_a, iq_a, ud_v, uq_v, rate_hz, report_every = calls[0]
    assert id_a[0] == pytest.approx(2.0)
    assert id_a[1] == pytest.approx(-1.0 / math.sqrt(3.0))
    assert iq_a == [1.0, -1.0]
    assert ud_v[0] == pytest.approx(100.0 * 24.0 /
                                    (math.sqrt(3.0) * 32768.0))
    assert uq_v[1] == pytest.approx(-200.0 * 25.0 /
                                    (math.sqrt(3.0) * 32768.0))
    assert rate_hz == 16000
    assert report_every == 1600


def test_离线RLS不再把浮点数据重新量化成F1整数(monkeypatch):
    seen = {}

    def fake_run(id_a, iq_a, ud_v, uq_v, rate_hz, report_every,
                 nominal_inductance_h, exact_simulink_reference):
        seen["iq"] = iq_a
        return [{"updates": 1, "sample_index": 1,
                 "theta_d": (0.9, 0, 0, 0.1, 0.01, 0, 0),
                 "theta_q": (0.9, 0, 0, 0, 0, 0.1, 0.01)}]

    monkeypatch.setattr(
        native_telemetry, "_native",
        type("FakeNative", (), {"run_simulink_rls": staticmethod(fake_run)})())
    columns = _columns()
    columns["iq_a"] = [0.123456789, -0.987654321]

    native_telemetry.run_offline_rls(columns)

    assert seen["iq"] == columns["iq_a"]


def test_离线分析同时返回ESO电流对照轨迹(monkeypatch):
    def fake_analysis(id_a, iq_a, ud_v, uq_v, rate_hz,
                      report_every, max_trace_points,
                      nominal_inductance_h, exact_simulink_reference):
        return {
            "results": [{
                "updates": 1, "sample_index": 1,
                "theta_d": (0.9, 0, 0, 0.1, 0.01, 0, 0),
                "theta_q": (0.9, 0, 0, 0, 0, 0.1, 0.01),
            }],
            "trace": {
                "sample_index": [0, 1],
                "id_a": id_a, "iq_a": iq_a,
                "id_hat_a": [0.1, 0.2], "iq_hat_a": [0.3, 0.4],
            },
            "trace_stride": 1,
        }

    monkeypatch.setattr(
        native_telemetry, "_native",
        type("FakeNative", (), {
            "run_simulink_rls": staticmethod(lambda *_args: ()),
            "run_simulink_rls_analysis": staticmethod(fake_analysis),
        })())

    analysis = native_telemetry.run_offline_rls_analysis(
        _columns(), max_trace_points=123)

    assert analysis["trace"]["sample_index"] == [0, 1]
    assert analysis["trace"]["id_hat_a"] == [0.1, 0.2]
    assert analysis["trace"]["iq_hat_a"] == [0.3, 0.4]
    assert analysis["results"][0]["tick_ms"] == 10
    assert analysis["diagnostics"]["verdict"] == "not_identifiable"
    assert "20 ms" in analysis["diagnostics"]["reasons"][0]
    assert analysis["eso_profile"]["purpose"] == "hardware_matched"
    assert analysis["eso_profile"]["nominal_ld_mh"] == pytest.approx(0.66)
    assert analysis["eso_profile"]["input_gain_ts_over_l"] == \
        pytest.approx(1.0 / 16000.0 / 0.00066)
    assert analysis["eso_profile"]["actuation_delay_samples"] == 1


def test_可辨识性检查拒绝过短数据():
    diagnostic = assess_rls_identifiability(_columns())

    assert diagnostic["verdict"] == "not_identifiable"
    assert diagnostic["sample_count"] == 2
    assert diagnostic["duration_s"] == pytest.approx(2 / 16000)


def test_可辨识性检查接受恒速且不产生无效相关系数告警():
    import numpy as np

    count = 16000
    phase = 2.0 * math.pi * np.arange(count) / 257.0
    columns = {
        "count": count, "rate_hz": 16000,
        "tick_ms": np.arange(count) // 16,
        "angle_deg": np.rad2deg(phase) % 360.0,
        "speed_rpm": np.full(count, 500.0),
        "id_a": 0.1 * np.sin(phase),
        "iq_a": 1.0 + 0.1 * np.cos(phase),
        "id_source_direct": True,
        "idref_a": 0.1 * np.sin(phase),
        "iqref_a": 1.0 + 0.1 * np.cos(phase),
        "ia_a": 0.1 * np.sin(phase),
        "ib_a": 0.1 * np.cos(phase),
        "vd_raw": 100.0 * np.sin(phase),
        "vq_raw": 100.0 * np.cos(phase),
        "vbus_v": np.full(count, 24.0),
    }

    with np.errstate(all="raise"):
        diagnostic = assess_rls_identifiability(columns)

    assert math.isnan(diagnostic["iq_speed_correlation"])


def test_真机物理辨识拒绝16kHz样本序号缺口():
    import numpy as np

    count = 48000
    sequence = np.arange(count, dtype=np.uint32) & 0xFFFF
    sequence[17000:] = (sequence[17000:] + 1) & 0xFFFF
    columns = {
        "count": count, "rate_hz": 16000,
        "tick_ms": np.arange(count) // 16,
        "sample_seq": sequence, "sequence_source_direct": True,
        "angle_deg": np.zeros(count), "speed_rpm": np.zeros(count),
        "id_a": np.zeros(count), "iq_a": np.zeros(count),
        "id_source_direct": True,
        "idref_a": np.zeros(count), "iqref_a": np.zeros(count),
        "ia_a": np.zeros(count), "ib_a": np.zeros(count),
        "vd_raw": np.zeros(count), "vq_raw": np.zeros(count),
        "vbus_v": np.full(count, 24.0),
    }

    result = identify_physical_dq_iv(columns)

    assert result["verdict"] == "not_identifiable"
    assert result["sequence_gap_count"] == 1
    assert "不连续" in result["reasons"][0]


def test_真机物理辨识拒绝跨停机但序号连续的数据():
    import numpy as np

    count = 48000
    ticks = np.arange(count, dtype=np.uint32) // 16
    ticks[17000:] += 100
    columns = {
        "count": count, "rate_hz": 16000,
        "tick_ms": ticks,
        "sample_seq": np.arange(count, dtype=np.uint32) & 0xFFFF,
        "sequence_source_direct": True,
        "angle_deg": np.zeros(count), "speed_rpm": np.zeros(count),
        "id_a": np.zeros(count), "iq_a": np.zeros(count),
        "id_source_direct": True,
        "idref_a": np.zeros(count), "iqref_a": np.zeros(count),
        "ia_a": np.zeros(count), "ib_a": np.zeros(count),
        "vd_raw": np.zeros(count), "vq_raw": np.zeros(count),
        "vbus_v": np.full(count, 24.0),
    }

    result = identify_physical_dq_iv(columns)

    assert result["verdict"] == "not_identifiable"
    assert result["tick_gap_count"] == 1
    assert "采集停顿" in result["reasons"][0]


def test_闭环工具变量辨识在PI与测量噪声下恢复真实RL():
    import numpy as np

    fs = 16000
    count = fs * 3
    resistance = 0.59
    inductance = 0.00066
    a = math.exp(-resistance / inductance / fs)
    b = (1.0 - a) / resistance
    kp = 1.524
    ki_step = 1362.7 / fs
    random = np.random.default_rng(7)

    def prbs(seed, offset=0.0, *, axis="d"):
        output = np.empty(count)
        state = seed
        divider = 0
        for index in range(count):
            divider += 1
            if divider >= 8:
                divider = 0
                if axis == "d":
                    feedback = ((state >> 14) ^ (state >> 13)) & 1
                    state = ((state << 1) | feedback) & 0x7FFF
                else:
                    feedback = ((state >> 0) ^ (state >> 2) ^
                                (state >> 3) ^ (state >> 5)) & 1
                    state = ((state >> 1) | (feedback << 15)) & 0xFFFF
                state = state or 1
            output[index] = offset + (0.08 if state & 1 else -0.08)
        return output

    def closed_loop(reference):
        actual = np.zeros(count)
        measured = np.zeros(count)
        voltage = np.zeros(count)
        integral = 0.0
        delayed = [0.0, 0.0]
        for index in range(1, count):
            actual[index] = a * actual[index - 1] + b * delayed.pop(0)
            measured[index] = actual[index] + random.normal(0.0, 0.02)
            error = reference[index] - measured[index]
            integral += ki_step * error
            voltage[index] = kp * error + integral
            delayed.append(voltage[index])
        return measured, voltage

    id_ref = prbs(0x0001)
    iq_ref = prbs(0x2345, 1.2, axis="q")
    id_a, ud_v = closed_loop(id_ref)
    iq_a, uq_v = closed_loop(iq_ref)
    volts_per_digit = 24.0 / (math.sqrt(3.0) * 32768.0)
    # Angle=0 in the MCSDK convention: Iq=alpha=Ia, Id=beta.  Put the
    # unfiltered plant current in Ia/Ib while exposing deliberately filtered
    # Id/Iq feedback fields.  The physical estimator must use the raw phase
    # samples so a firmware feedback filter cannot masquerade as motor L.
    ia_a = iq_a.copy()
    ib_a = (-math.sqrt(3.0) * id_a - ia_a) / 2.0
    filtered_id = np.empty_like(id_a)
    filtered_iq = np.empty_like(iq_a)
    filtered_id[0] = id_a[0]
    filtered_iq[0] = iq_a[0]
    for index in range(1, count):
        filtered_id[index] = 0.35 * id_a[index] + 0.65 * filtered_id[index - 1]
        filtered_iq[index] = 0.35 * iq_a[index] + 0.65 * filtered_iq[index - 1]
    columns = {
        "count": count, "rate_hz": fs,
        "tick_ms": np.arange(count) // 16,
        "sample_seq": np.arange(count, dtype=np.uint32) & 0xFFFF,
        "sequence_source_direct": True,
        "angle_deg": np.zeros(count), "speed_rpm": np.zeros(count),
        "id_a": filtered_id, "iq_a": filtered_iq, "id_source_direct": True,
        "idref_a": id_ref, "iqref_a": iq_ref,
        "ia_a": ia_a, "ib_a": ib_a,
        "vd_raw": ud_v / volts_per_digit,
        "vq_raw": uq_v / volts_per_digit,
        "vbus_v": np.full(count, 24.0),
    }

    result = identify_physical_dq_iv(columns)

    assert result["verdict"] == "usable"
    assert result["rd_ohm"] == pytest.approx(resistance, abs=0.04)
    assert result["rq_ohm"] == pytest.approx(resistance, abs=0.04)
    assert result["ld_mh"] == pytest.approx(inductance * 1e3, abs=0.04)
    assert result["lq_mh"] == pytest.approx(inductance * 1e3, abs=0.04)
    assert result["d_axis"]["delay_samples"] == pytest.approx(2.0)
    assert result["q_axis"]["delay_samples"] == pytest.approx(2.0)
    assert abs(result["probe_reference_correlation"]) < 0.1
    assert result["feedback_vs_raw_iq_rmse_a"] > 0.01


def test_闭环工具变量使用逐点Park角补偿旋转dq耦合():
    import numpy as np

    fs = 16000
    count = fs * 3
    ts = 1.0 / fs
    resistance = 0.59
    inductance = 0.00066
    flux_wb = 0.00585
    speed_rpm = 500.0
    pole_pairs = 4
    omega = speed_rpm * pole_pairs * 2.0 * math.pi / 60.0
    kp = 1.524
    ki_step = 1362.7 / fs

    def probe(seed, *, q_axis=False, offset=0.0):
        values = np.empty(count)
        state = seed
        divider = 0
        for index in range(count):
            divider += 1
            if divider >= 8:
                divider = 0
                if q_axis:
                    bit = ((state >> 0) ^ (state >> 2) ^
                           (state >> 3) ^ (state >> 5)) & 1
                    state = ((state >> 1) | (bit << 15)) & 0xFFFF
                else:
                    bit = ((state >> 14) ^ (state >> 13)) & 1
                    state = ((state << 1) | bit) & 0x7FFF
                state = state or 1
            values[index] = offset + (0.08 if state & 1 else -0.08)
        return values

    id_ref = probe(1)
    iq_ref = probe(0x2345, q_axis=True, offset=1.2)
    id_a = np.zeros(count)
    iq_a = np.zeros(count)
    ud_v = np.zeros(count)
    uq_v = np.zeros(count)
    int_d = int_q = 0.0
    delayed_d = [0.0, 0.0]
    delayed_q = [0.0, 0.0]
    for index in range(1, count):
        applied_d = delayed_d.pop(0)
        applied_q = delayed_q.pop(0)
        id_a[index] = id_a[index - 1] + ts / inductance * (
            applied_d - resistance * id_a[index - 1] +
            omega * inductance * iq_a[index - 1])
        iq_a[index] = iq_a[index - 1] + ts / inductance * (
            applied_q - resistance * iq_a[index - 1] -
            omega * (inductance * id_a[index - 1] + flux_wb))
        error_d = id_ref[index] - id_a[index]
        error_q = iq_ref[index] - iq_a[index]
        int_d += ki_step * error_d
        int_q += ki_step * error_q
        ud_v[index] = kp * error_d + int_d
        uq_v[index] = kp * error_q + int_q
        delayed_d.append(ud_v[index])
        delayed_q.append(uq_v[index])

    angle = np.arange(count) * omega / fs
    alpha = iq_a * np.cos(angle) + id_a * np.sin(angle)
    beta = -iq_a * np.sin(angle) + id_a * np.cos(angle)
    ia_a = alpha
    ib_a = (-math.sqrt(3.0) * beta - ia_a) / 2.0
    volts_per_digit = 24.0 / (math.sqrt(3.0) * 32768.0)
    # Match the actual tagged-F1 transport instead of feeding ideal floats:
    # phase/direct currents and references are s16 ADC/current digits, angle is
    # u16, and Vd/Vq are s16 modulation commands.  Use a non-nominal VDDA to
    # exercise the same scale used by F1/32.
    vdda_v = 3.15
    current_per_digit = vdda_v / (65536.0 * 0.01000 * 8.00)
    angle_raw = np.rint((angle % (2.0 * math.pi)) /
                        (2.0 * math.pi) * 65536.0) % 65536.0
    current_raw = lambda value: int(np.clip(
        np.rint(value / current_per_digit), -32767, 32767))
    voltage_raw = lambda value: int(np.clip(
        np.rint(value / volts_per_digit), -32767, 32767))
    bus_adc = round(24.0 * 0.0270 / vdda_v * 65536.0)
    sample_struct = struct.Struct("<IHHhhhhhhhhhHHH")
    processor = native_telemetry.NativeTelemetryProcessor(
        max_f1_samples=count + 1)
    processor.set_f1_rate_hz(fs)
    # Feed the same 24-sample/768-byte frames used by the firmware.  This makes
    # the regression cover the complete F1/32 decoder and its real VDDA/Vbus
    # scale instead of bypassing the transport with hand-built float columns.
    frame = bytearray()
    for index in range(count):
        frame.extend(sample_struct.pack(
            index // 16, index & 0xFFFF, int(angle_raw[index]),
            int(speed_rpm), current_raw(iq_a[index]),
            current_raw(id_a[index]), current_raw(iq_ref[index]),
            current_raw(id_ref[index]), current_raw(ia_a[index]),
            current_raw(ib_a[index]), voltage_raw(ud_v[index]),
            voltage_raw(uq_v[index]), bus_adc, round(vdda_v * 1000),
            0xF132))
        if (index + 1) % 24 == 0:
            assert processor.ingest(0xF1, frame)
            frame.clear()
    if frame:
        assert processor.ingest(0xF1, frame)
    columns = processor.drain_f1_columns(count + 1)

    result = identify_physical_dq_iv(columns)

    assert result["verdict"] == "usable", result["reasons"]
    assert result["nominal_match"] is True
    assert result["rd_ohm"] == pytest.approx(resistance, abs=0.06)
    assert result["rq_ohm"] == pytest.approx(resistance, abs=0.06)
    assert result["ld_mh"] == pytest.approx(0.641, abs=0.06)
    assert result["lq_mh"] == pytest.approx(0.641, abs=0.06)
    assert result["electrical_frequency_hz_from_angle"] == pytest.approx(
        speed_rpm * pole_pairs / 60.0, abs=0.1)

    # A coherent but globally wrong voltage scale must remain visible.  The
    # estimator may call it repeatable, but must not project it back onto the
    # configured motor parameters or present it as a nominal match.
    scaled_columns = dict(columns)
    scaled_columns["vbus_v"] = [value * 1.5 for value in columns["vbus_v"]]
    scaled = identify_physical_dq_iv(scaled_columns)
    assert scaled["verdict"] == "usable"
    assert scaled["nominal_match"] is False
    assert scaled["rd_ohm"] == pytest.approx(result["rd_ohm"] * 1.5,
                                               rel=0.02)
    assert scaled["ld_mh"] == pytest.approx(result["ld_mh"] * 1.5,
                                              rel=0.02)

    subtle_scale_error = dict(columns)
    subtle_scale_error["vbus_v"] = [value * 1.2
                                      for value in columns["vbus_v"]]
    subtle = identify_physical_dq_iv(subtle_scale_error)
    assert subtle["verdict"] == "usable"
    assert subtle["nominal_match"] is False
    assert any("ld_mh偏离标称" in reason or "lq_mh偏离标称" in reason
               for reason in subtle["nominal_reasons"])

    ramping = dict(columns)
    ramping["speed_rpm"] = np.linspace(0.0, 1000.0, count)
    ramp_result = identify_physical_dq_iv(ramping)
    assert ramp_result["verdict"] == "not_identifiable"
    assert any("转速未稳态" in reason for reason in ramp_result["reasons"])
