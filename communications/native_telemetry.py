"""F1/F4 原生数据处理器边界。"""
from __future__ import annotations

import os
import math


# These are the parameters stored in the R2024b source model
# ``MFPCC_DDM_coldstart_ESOrls.slx``.  They are intentionally kept separate
# from the current F407 motor configuration: exact numerical replay of that
# Simulink model is useful, but it is not a hardware observer for a different
# motor.
SIMULINK_ESORLS_REFERENCE = {
    "model": "MFPCC_DDM_coldstart_ESOrls.slx",
    "sample_rate_hz": 100000,
    "resistance_ohm": 2.34,
    "ld_mh": 19.36,
    "lq_mh": 19.36,
    "flux_wb": 0.402,
    "eso_bandwidth_rad_s": 4000.0,
    "b1_ts_over_l": 5.165289e-4,
}

F407_HARDWARE_REFERENCE = {
    "sample_rate_hz": 16000,
    "resistance_ohm": 0.59,
    "ld_mh": 0.66,
    "lq_mh": 0.66,
    "flux_wb": 0.00585,
}


def simulink_reference_model_metadata(rate_hz: int) -> dict:
    """Describe why the exact Simulink replay is not a hardware R/L result."""
    model = dict(SIMULINK_ESORLS_REFERENCE)
    hardware = dict(F407_HARDWARE_REFERENCE)
    return {
        "purpose": "simulation_replay_only",
        "physical_hardware_compatible": False,
        "capture_rate_hz": int(rate_hz),
        "simulink_model": model,
        "f407_hardware": hardware,
        "parameter_ratios_model_over_hardware": {
            "resistance": model["resistance_ohm"] /
            hardware["resistance_ohm"],
            "inductance": model["ld_mh"] / hardware["ld_mh"],
            "flux": model["flux_wb"] / hardware["flux_wb"],
        },
        "warning": (
            "R2024b ESO/RLS重放对应R=2.34Ω、Ld=Lq=19.36mH、"
            "磁链0.402Wb、100kHz的仿真对象；当前F407对象为"
            "R=0.59Ω、L=0.66mH、磁链约0.00585Wb、16kHz。"
            "重放系数只能验证模型同构，不能解释为真机参数。"),
    }


try:
    import motor_core_cpp as _native
except (ImportError, OSError) as exc:
    _native = None
    _native_import_error = str(exc)
else:
    _native_schema = int(getattr(_native, "telemetry_schema_version", 0))
    if _native_schema < 3:
        _native_import_error = (
            "motor_core_cpp版本过旧：当前原生遥测schema="
            f"{_native_schema}，F1/40需要schema>=3；请重新安装native_core")
        _native = None
    else:
        _native_import_error = ""


def native_telemetry_available() -> bool:
    return _native is not None and hasattr(_native, "TelemetryProcessor")


def native_telemetry_mode() -> str:
    mode = os.getenv("MOTOR_HMI_NATIVE_TELEMETRY", "auto").strip().lower()
    if mode not in {"auto", "native", "python"}:
        raise ValueError(
            "MOTOR_HMI_NATIVE_TELEMETRY 只能是 auto、native 或 python")
    if mode == "native" and not native_telemetry_available():
        raise RuntimeError(
            f"已强制使用 C++ 遥测解析器，但加载失败：{_native_import_error}")
    return mode


def native_telemetry_enabled() -> bool:
    return (native_telemetry_mode() != "python" and
            native_telemetry_available())


def _prepare_offline_rls_inputs(columns: dict):
    """把同步 F1 列换成真机 ESO/RLS 的 dq/SI 输入。"""
    from rls_offline import resolve_voltage_columns_si, validate_rls_columns

    count = validate_rls_columns(columns)
    if _native is None or not hasattr(_native, "run_simulink_rls"):
        raise RuntimeError(f"C++遥测核心不可用：{_native_import_error}")
    rate_hz = int(columns["rate_hz"])
    sqrt3 = math.sqrt(3.0)
    direct_id = columns.get("id_a") if bool(
        columns.get("id_source_direct", "id_a" in columns)) else None
    id_a: list[float] = []
    iq_a = [float(value) for value in columns["iq_a"]]
    resolved_ud, resolved_uq, voltage_source = \
        resolve_voltage_columns_si(columns)
    ud_v = [float(value) for value in resolved_ud]
    uq_v = [float(value) for value in resolved_uq]
    for index in range(count):
        if direct_id is not None:
            id_a.append(float(direct_id[index]))
        else:
            angle = math.radians(float(columns["angle_deg"][index]))
            ia = float(columns["ia_a"][index])
            ib = float(columns["ib_a"][index])
            beta = -(ia + 2.0 * ib) / sqrt3
            id_a.append(ia * math.sin(angle) + beta * math.cos(angle))
    return (count, int(columns["rate_hz"]), id_a, iq_a, ud_v, uq_v,
            voltage_source)


def _decorate_rls_results(results, columns: dict, count: int,
                          rate_hz: int) -> list[dict]:
    output = [dict(item) for item in results]
    sample_time = 1.0 / rate_hz
    for item in output:
        _append_equivalent_rl(item, sample_time)
        sample_index = min(int(item.get("sample_index", 0)), count - 1)
        item["tick_ms"] = int(columns["tick_ms"][sample_index])
    if not output:
        raise ValueError(
            "数据未产生 RLS 结果：请确保数据长度足够且包含有效激励")
    return output


def run_offline_rls(columns: dict) -> list[dict]:
    """按真机采样率与标称电感运行 C++ ESO 与三阶RLS。"""
    count, rate_hz, id_a, iq_a, ud_v, uq_v, _voltage_source = \
        _prepare_offline_rls_inputs(columns)

    report_every = max(rate_hz // 10, 1)
    results = _native.run_simulink_rls(
        id_a, iq_a, ud_v, uq_v, rate_hz, report_every,
        F407_HARDWARE_REFERENCE["ld_mh"] * 1e-3, False)
    return _decorate_rls_results(results, columns, count, rate_hz)


def run_offline_rls_analysis(columns: dict,
                             max_trace_points: int = 5000) -> dict:
    """使用真机匹配ESO辨识，并返回限长电流对照轨迹。"""
    from rls_offline import (
        assess_legacy_capture_evidence, assess_rls_identifiability,
        cross_check_arx7_motor_projection,
        derive_arx7_motor_parameter_diagnostic, identify_physical_dq_iv,
    )

    count, rate_hz, id_a, iq_a, ud_v, uq_v, voltage_source = \
        _prepare_offline_rls_inputs(columns)
    if not hasattr(_native, "run_simulink_rls_analysis"):
        native_output = {
            "results": run_offline_rls(columns), "trace": {},
        }
    else:
        native_output = dict(_native.run_simulink_rls_analysis(
            id_a, iq_a, ud_v, uq_v, rate_hz, max(rate_hz // 10, 1),
            max(1, int(max_trace_points)),
            F407_HARDWARE_REFERENCE["ld_mh"] * 1e-3, False))
        native_output["results"] = _decorate_rls_results(
            native_output.get("results", ()), columns, count, rate_hz)
    native_output["rate_hz"] = rate_hz
    native_output["voltage_source"] = voltage_source
    native_output["eso_profile"] = {
        "purpose": "hardware_matched",
        "sample_rate_hz": rate_hz,
        "nominal_ld_mh": F407_HARDWARE_REFERENCE["ld_mh"],
        "nominal_lq_mh": F407_HARDWARE_REFERENCE["lq_mh"],
        "bandwidth_rad_s": SIMULINK_ESORLS_REFERENCE["eso_bandwidth_rad_s"],
        "input_gain_ts_over_l": (
            1.0 / rate_hz /
            (F407_HARDWARE_REFERENCE["ld_mh"] * 1e-3)),
        "actuation_delay_samples": 1,
        "voltage_source": voltage_source,
    }
    native_output["diagnostics"] = assess_rls_identifiability(columns)
    native_output["physical_iv"] = identify_physical_dq_iv(columns)
    native_output["legacy_evidence"] = assess_legacy_capture_evidence(columns)
    native_output["simulink_reference"] = \
        simulink_reference_model_metadata(rate_hz)
    results = list(native_output.get("results", ()))
    if results:
        final = results[-1]
        projection = derive_arx7_motor_parameter_diagnostic(
            final.get("theta_d", ()), final.get("theta_q", ()),
            sample_time_s=1.0 / rate_hz)
        native_output["arx_motor_projection"] = projection
        native_output["arx_motor_cross_check"] = \
            cross_check_arx7_motor_projection(
                projection, physical_iv=native_output["physical_iv"],
                legacy_evidence=native_output["legacy_evidence"],
                flux_reference_wb=F407_HARDWARE_REFERENCE["flux_wb"])
    return native_output


def _append_equivalent_rl(item: dict, sample_time: float) -> None:
    """Add a legacy low-frequency-moment diagnostic.

    A general ARX(3, 2-input) fit is not a unique physical PMSM R/L model.
    Keep the historical fields for file/API compatibility, but mark them as
    unvalidated so callers cannot confuse this projection with the independent
    closed-loop IV result.
    """
    def convert(theta, own_index):
        values = [float(value) for value in theta]
        a_dc = 1.0 - sum(values[:3])
        b0, b1 = values[own_index:own_index + 2]
        b_dc = b0 + b1
        if abs(a_dc) <= 1e-15 or abs(b_dc) <= 1e-15:
            return float("nan"), float("nan")
        resistance = a_dc / b_dc
        moment = ((b0 + 2.0 * b1) / b_dc +
                  (values[0] + 2.0 * values[1] +
                   3.0 * values[2]) / a_dc)
        return resistance, resistance * sample_time * moment

    rd, ld = convert(item["theta_d"], 3)
    rq, lq = convert(item["theta_q"], 5)
    item.update({
        "rd_ohm": rd, "rq_ohm": rq,
        "ld_mh": ld * 1e3, "lq_mh": lq * 1e3,
        "rl_equivalent_method": "arx3_low_frequency_moment",
        "rl_physical_validated": False,
    })


class NativeTelemetryProcessor:
    backend = "cpp"

    def __init__(self, max_f1_samples: int = 131072,
                 max_bursts: int = 4) -> None:
        if not native_telemetry_available():
            raise RuntimeError(f"C++遥测解析器不可用：{_native_import_error}")
        self._processor = _native.TelemetryProcessor(
            max(1, int(max_f1_samples)), max(1, int(max_bursts)))

    def ingest(self, command: int, payload: bytes) -> bool:
        return bool(self._processor.ingest(int(command) & 0xFF, bytes(payload)))

    def set_f1_rate_hz(self, rate_hz: int) -> None:
        self._processor.set_f1_rate_hz(max(1, int(rate_hz)))

    def set_rls_coefficients_si(self, enabled: bool) -> None:
        self._processor.set_rls_coefficients_si(bool(enabled))

    def set_host_rls_enabled(self, enabled: bool, *, reset: bool = True) -> None:
        self._processor.set_host_rls_enabled(bool(enabled), bool(reset))

    @property
    def host_rls_enabled(self) -> bool:
        return bool(self._processor.host_rls_enabled)

    def drain_f1(self, max_samples: int = 8192) -> list[dict]:
        return list(self._processor.drain_f1(max(1, int(max_samples))))

    def drain_f1_columns(self, max_samples: int = 8192) -> dict:
        return dict(self._processor.drain_f1_columns(
            max(1, int(max_samples))))

    def drain_f2(self, max_samples: int = 512) -> list[dict]:
        return list(self._processor.drain_f2(max(1, int(max_samples))))

    def drain_f3(self, max_samples: int = 512) -> list[dict]:
        return list(self._processor.drain_f3(max(1, int(max_samples))))

    def drain_bursts(self, max_bursts: int = 1) -> list[dict]:
        return list(self._processor.drain_bursts(max(1, int(max_bursts))))

    def stats(self) -> dict[str, int]:
        return dict(self._processor.stats())

    def reset(self) -> None:
        self._processor.reset()

    def reset_burst(self) -> None:
        self._processor.reset_burst()
