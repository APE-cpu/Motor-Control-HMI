"""电机上位机只读诊断工具集合。"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ai.harness import (
    ToolContext, ToolRegistry, ToolResult, ToolSpec, finite_number,
)
from communications.comm_manager import decode_motor_fault_code
from pages.fourier_page import compute_spectrum
from pages.frequency_response_page import firmware_current_loop_frequency_response


_CHANNELS = [
    "angle_deg", "speed_rpm", "iq_a", "iqref_a", "ia_a", "ib_a",
    "vd_raw", "vq_raw", "vbus_v",
]

_UNITS = {
    "angle_deg": "deg", "speed_rpm": "rpm", "iq_a": "A",
    "iqref_a": "A", "ia_a": "A", "ib_a": "A",
    "vd_raw": "digit", "vq_raw": "digit", "vbus_v": "V",
}


def _empty_schema() -> dict:
    return {"type": "object", "properties": {},
            "additionalProperties": False}


def _json_primitives(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_primitives(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_primitives(item) for item in value]
    return str(value)


def get_runtime_state(_arguments: dict, context: ToolContext) -> ToolResult:
    frame = context.comm.latest_frame()
    fault_code = int(getattr(frame, "fault_code", 0) or 0)
    history_code = int(getattr(frame, "fault_history_code", 0) or 0)
    protocol = _json_primitives(context.comm.protocol_status())
    return ToolResult(ok=True, data={
        "connected": bool(context.comm.is_connected()),
        "simulation_running": bool(context.comm.is_sim_running()),
        "telemetry_age_s": finite_number(context.comm.telemetry_age_s(), -1.0),
        "data_source": str(getattr(frame, "data_source", "unknown")),
        "mc_state": int(getattr(frame, "mc_state", 0) or 0),
        "speed_actual_rpm": finite_number(getattr(frame, "speed_actual", 0.0)),
        "speed_target_rpm": finite_number(getattr(frame, "speed_target", 0.0)),
        "iq_actual_a": finite_number(getattr(frame, "current_actual", 0.0)),
        "iq_target_a": finite_number(getattr(frame, "current_target", 0.0)),
        "vbus_v": finite_number(getattr(frame, "vdc", 0.0)),
        "temperature_c": finite_number(getattr(frame, "temperature", 0.0)),
        "fault_code": fault_code,
        "fault_text": (str(getattr(frame, "fault_text", "")) or
                       decode_motor_fault_code(fault_code)),
        "fault_history_code": history_code,
        "fault_history_text": (
            str(getattr(frame, "fault_history_text", "")) or
            decode_motor_fault_code(history_code)),
        "protocol": protocol,
    }, metadata={"source": "CommManager只读快照"})


def get_firmware_parameters(_arguments: dict,
                            context: ToolContext) -> ToolResult:
    config = (dict(context.firmware_config_provider())
              if callable(context.firmware_config_provider) else {})
    frame = context.comm.latest_frame()
    config.setdefault("kp_cur_digit", 2323)
    config.setdefault("ki_cur_digit", 2077)
    config.setdefault("sample_rate_hz", 16000)
    config.setdefault("vbus_v", finite_number(getattr(frame, "vdc", 0.0), 24.0))
    config.setdefault(
        "filter_enabled", bool(getattr(frame, "current_filter_enabled", False)))
    config.setdefault(
        "filter_alpha_q15",
        int(getattr(frame, "current_filter_alpha_q15", 0) or 20491))
    return ToolResult(
        ok=True,
        data=_json_primitives(config),
        metadata={"telemetry_config": _json_primitives(
            context.comm.telemetry_config())})


def capture_signal_window(arguments: dict,
                          context: ToolContext) -> ToolResult:
    snapshot = context.telemetry_store.capture(
        list(arguments["channels"]), float(arguments["duration_s"]))
    warnings = []
    if snapshot["truncated"]:
        warnings.append(
            "环形缓冲长度不足请求时长，已返回当前可用的最新同步数据")
    return ToolResult(ok=True, data={
        "snapshot_id": snapshot["snapshot_id"],
        "channels": list(snapshot["channels"]),
        "sample_rate_hz": snapshot["sample_rate_hz"],
        "sample_count": snapshot["sample_count"],
        "duration_s": snapshot["duration_s"],
        "truncated": snapshot["truncated"],
    }, warnings=warnings, metadata={
        "source": "raw_high_rate_telemetry",
        "filtering": "未应用上位机显示滤波",
        "units": {name: _UNITS[name] for name in snapshot["channels"]},
    })


def analyze_time_domain(arguments: dict,
                        context: ToolContext) -> ToolResult:
    snapshot = context.telemetry_store.get(arguments["snapshot_id"])
    requested = arguments.get("channels") or list(snapshot["channels"])
    missing = [name for name in requested if name not in snapshot["data"]]
    if missing:
        return ToolResult(ok=False, error=(
            "snapshot_missing_channels:" + ",".join(missing)))
    metrics = {}
    for name in requested:
        values = np.asarray(snapshot["data"][name], dtype=float)
        values = values[np.isfinite(values)]
        if values.size == 0:
            continue
        rms = float(np.sqrt(np.mean(values * values)))
        mean = float(np.mean(values))
        metrics[name] = {
            "unit": _UNITS[name],
            "mean": mean,
            "rms": rms,
            "ac_rms": float(np.sqrt(np.mean((values - mean) ** 2))),
            "std": float(np.std(values)),
            "minimum": float(np.min(values)),
            "maximum": float(np.max(values)),
            "peak_to_peak": float(np.ptp(values)),
        }
    return ToolResult(ok=True, data={
        "snapshot_id": snapshot["snapshot_id"],
        "sample_rate_hz": snapshot["sample_rate_hz"],
        "sample_count": snapshot["sample_count"],
        "metrics": metrics,
    }, metadata={"source": "raw_high_rate_telemetry"})


def analyze_fft(arguments: dict, context: ToolContext) -> ToolResult:
    snapshot = context.telemetry_store.get(arguments["snapshot_id"])
    channel = arguments["channel"]
    if channel not in snapshot["data"]:
        return ToolResult(
            ok=False, error=f"snapshot_missing_channel:{channel}")
    result = compute_spectrum(
        snapshot["data"][channel], snapshot["sample_rate_hz"],
        window_name="hann", remove_dc=bool(arguments["remove_dc"]))
    frequencies = result["frequencies"]
    amplitudes = result["amplitudes"]
    local = np.flatnonzero(
        (amplitudes[1:-1] > amplitudes[:-2]) &
        (amplitudes[1:-1] >= amplitudes[2:])) + 1
    local = local[frequencies[local] > 0.0]
    if local.size:
        ranked = local[np.argsort(amplitudes[local])[-6:]][::-1]
    else:
        ranked = np.asarray([], dtype=int)
    peaks = [{
        "frequency_hz": float(frequencies[index]),
        "amplitude": float(amplitudes[index]),
        "unit": _UNITS[channel],
    } for index in ranked]
    return ToolResult(ok=True, data={
        "snapshot_id": snapshot["snapshot_id"],
        "channel": channel,
        "sample_rate_hz": result["sample_rate_hz"],
        "sample_count": result["sample_count"],
        "duration_s": result["duration_s"],
        "resolution_hz": result["resolution_hz"],
        "dc": result["dc"],
        "ac_rms": result["rms"],
        "peak_to_peak": result["peak_to_peak"],
        "dominant_nonzero_hz": result["fundamental_hz"],
        "dominant_amplitude": result["fundamental_amplitude"],
        "peaks": peaks,
    }, metadata={
        "source": "raw_high_rate_telemetry",
        "window": "hann",
        "remove_dc": bool(arguments["remove_dc"]),
        "amplitude_definition": "单边峰值幅值",
    })


def calculate_current_loop(_arguments: dict,
                           context: ToolContext) -> ToolResult:
    config = (dict(context.firmware_config_provider())
              if callable(context.firmware_config_provider) else {})
    frame = context.comm.latest_frame()
    rate = finite_number(config.get("sample_rate_hz"), 16000.0)
    vbus = finite_number(config.get("vbus_v"),
                         finite_number(getattr(frame, "vdc", 0.0), 24.0))
    if vbus <= 0.0:
        vbus = 24.0
    model = firmware_current_loop_frequency_response(
        0.59, 0.66e-3, rate, vbus, 3.3, 0.01, 8.0,
        int(config.get("kp_cur_digit", 2323)),
        int(config.get("ki_cur_digit", 2077)),
        1024, 16384, 0.5,
        bool(config.get("filter_enabled", False)),
        int(config.get("filter_alpha_q15", 20491)))
    poles = [{
        "real": float(value.real), "imag": float(value.imag),
        "magnitude": float(abs(value)),
    } for value in model["poles"]]
    gain_margin = model["gain_margin_db"]
    return ToolResult(ok=True, data={
        "model": "F407固件等效q轴离散小信号模型",
        "sample_rate_hz": rate,
        "kp_digit": int(config.get("kp_cur_digit", 2323)),
        "ki_digit": int(config.get("ki_cur_digit", 2077)),
        "feedback_filter_enabled": bool(config.get("filter_enabled", False)),
        "gain_cross_hz": model["gain_cross_hz"],
        "phase_margin_deg": model["phase_margin_deg"],
        "gain_margin_db": (gain_margin if math.isfinite(gain_margin) else None),
        "closed_loop_bandwidth_hz": model["closed_bandwidth_hz"],
        "closed_loop_peak_db": model["resonance_db"],
        "closed_loop_peak_hz": model["resonance_hz"],
        "ms": model["ms"],
        "ms_frequency_hz": model["ms_frequency_hz"],
        "mt": model["mt"],
        "mt_frequency_hz": model["mt_frequency_hz"],
        "nyquist_min_distance": model["nyquist_min_distance"],
        "nyquist_min_distance_hz": model["nyquist_min_distance_hz"],
        "poles": poles,
        "maximum_pole_magnitude": max(
            (item["magnitude"] for item in poles), default=0.0),
        "pole_model_note": model["pole_model_note"],
    }, warnings=[
        "模型使用当前工程默认Rs=0.59Ω、Lq=0.66mH；参数失配会影响结论",
        "线性模型不能单独证明非线性极限环",
    ], metadata={"parameter_source": config.get("source", "工程默认值")})


def create_read_only_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(
        "get_runtime_state",
        "读取当前连接、运行、故障、转速、电流、母线和协议状态。"
        "当问题涉及当前是否运行、是否故障、通信是否正常时优先调用。",
        _empty_schema(), "read_only", 2.0, get_runtime_state))
    registry.register(ToolSpec(
        "get_firmware_parameters",
        "读取当前电流环PI数字量、16kHz采样率、反馈滤波和遥测配置。"
        "当问题涉及控制参数、采样率、滤波或固件配置时调用。",
        _empty_schema(), "read_only", 2.0, get_firmware_parameters))
    registry.register(ToolSpec(
        "capture_signal_window",
        "从上位机高速环形缓冲截取同步原始信号并返回snapshot_id。"
        "分析波形、噪声、振荡或频谱前必须先调用；不会返回显示滤波数据。",
        {
            "type": "object",
            "properties": {
                "channels": {"type": "array", "items": {"enum": _CHANNELS},
                             "minItems": 1, "maxItems": 6},
                "duration_s": {"type": "number", "minimum": 0.1,
                               "maximum": 30.0},
            },
            "required": ["channels", "duration_s"],
            "additionalProperties": False,
        }, "read_only", 3.0, capture_signal_window))
    registry.register(ToolSpec(
        "analyze_time_domain",
        "分析已捕获快照的均值、RMS、交流RMS、标准差、范围和峰峰值。"
        "用于判断偏置、噪声、波动和削顶；必须传入capture_signal_window返回的snapshot_id。",
        {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "channels": {"type": "array", "items": {"enum": _CHANNELS},
                             "minItems": 1, "maxItems": 6},
            },
            "required": ["snapshot_id"],
            "additionalProperties": False,
        }, "read_only", 4.0, analyze_time_domain))
    registry.register(ToolSpec(
        "analyze_fft",
        "对已捕获快照中的单个通道做Hann窗单边FFT，返回主要尖峰。"
        "用于周期振荡、谐波和边带；FFT存在尖峰不能单独证明闭环不稳定。",
        {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "channel": {"type": "string", "enum": _CHANNELS},
                "remove_dc": {"type": "boolean"},
            },
            "required": ["snapshot_id", "channel", "remove_dc"],
            "additionalProperties": False,
        }, "read_only", 5.0, analyze_fft))
    registry.register(ToolSpec(
        "calculate_current_loop",
        "使用当前F407电流PI、采样率和反馈滤波计算理论带宽、稳定裕度、"
        "Ms/Mt、奈奎斯特距离和闭环极点。用于判断频谱尖峰是否接近控制环特征频率；"
        "该工具不能单独证明极限环。",
        _empty_schema(), "read_only", 5.0, calculate_current_loop))
    return registry
