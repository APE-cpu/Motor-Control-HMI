"""实验日志用的只读 harness 工具：分析已保存的实验数据。

与 ai/diagnostic_tools（分析内存里的实时数据）并列。所有工具只接受“实验编号 + 波形批次”，
在该实验目录内解析文件，不接受任意路径；结果附带源文件 SHA-256 与样本数，
由 ToolExecutor 统一写审计日志，实验日志末尾据此列出“数据来源与处理”。
"""
from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from ai.harness import ToolContext, ToolRegistry, ToolResult, ToolSpec
from core.phasor_decomposition import (
    Capture, analyze, find_pairs, load_capture, steady_window,
)
from core.power_estimate import PowerParams, estimate_power_series, power_summary
from core.vector_distortion import diagnose, estimate_params
from core.vector_shape import trajectory_shape
from widgets.media_recorder import read_media_manifest

HIGH_RATE_NAMES = ("高速数据.csv", "RLS辨识数据.csv")
SPECTRUM_CHANNELS = ("iq_a", "speed_rpm", "ia_a", "ib_a", "current_complex")
_CACHE_SIZE = 4


# ─── 文件解析与缓存 ────────────────────────────────────────────
class CaptureCache:
    """按 (路径, 修改时间) 缓存已读取的高速数据，同一日志多次调用不重复解析 CSV。"""

    def __init__(self) -> None:
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path) -> tuple[Capture, str]:
        key = (str(path), path.stat().st_mtime_ns)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        capture = load_capture(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            self._items[key] = (capture, digest)
            while len(self._items) > _CACHE_SIZE:
                self._items.popitem(last=False)
        return capture, digest


def waveform_batches(session_dir: Path) -> list[dict]:
    """实验目录下的波形保存批次（按时间排序）。"""
    batches = []
    root = Path(session_dir) / "waveforms"
    if not root.is_dir():
        return batches
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        high_rate = next((folder / name for name in HIGH_RATE_NAMES
                          if (folder / name).exists()), None)
        batches.append({
            "batch": folder.name,
            "high_rate_file": high_rate.name if high_rate else None,
            "has_waveform_png": (folder / "波形.png").exists(),
            "has_curve_csv": (folder / "原始数据.csv").exists(),
            "files": sorted(p.name for p in folder.iterdir() if p.is_file()),
        })
    return batches


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


class ExperimentData:
    """把 (实验编号, 批次, 时间窗) 解析成数据窗口；工具共用。"""

    def __init__(self, repository, params_provider=None) -> None:
        self.repository = repository
        self.params_provider = params_provider
        self.cache = CaptureCache()

    def session_dir(self, experiment_id: str) -> Path:
        return self.repository.session_dir(experiment_id)

    def high_rate_path(self, experiment_id: str, batch: str | None) -> Path:
        batches = [b for b in waveform_batches(self.session_dir(experiment_id))
                   if b["high_rate_file"]]
        if not batches:
            raise ValueError("该实验没有保存高速数据（需在实验中点“保存所有波形”）")
        if batch:
            match = next((b for b in batches if b["batch"] == batch), None)
            if match is None:
                raise ValueError(f"找不到波形批次 {batch}")
        else:
            match = batches[-1]
        return self.session_dir(experiment_id) / "waveforms" / match["batch"] / match["high_rate_file"]

    def window(self, arguments: dict, prefer_steady: bool = False) -> tuple[Capture, dict]:
        """解析时间窗。未指定 t0/t1 且 prefer_steady 时自动选稳态运行段（频谱、矢量分解用），
        否则取整段（统计、功率用）。"""
        path = self.high_rate_path(arguments["experiment_id"], arguments.get("batch"))
        capture, digest = self.cache.get(path)
        selection = "指定时间窗"
        if "t0" in arguments or "t1" in arguments:
            t0 = float(arguments.get("t0", capture.time[0]))
            t1 = float(arguments.get("t1", capture.time[-1]))
        else:
            t0, t1 = float(capture.time[0]), float(capture.time[-1])
            selection = "整段"
            if prefer_steady:
                steady = steady_window(capture)
                if steady is not None:
                    t0, t1 = steady
                    selection = "自动选取的稳态运行段（转速在中位数±12%内的最长连续区间）"
        window = capture.window(t0, t1)
        if window.time.size < 64:
            raise ValueError("时间窗内的采样点太少")
        source = {"file": str(path.relative_to(self.session_dir(arguments["experiment_id"]))),
                  "sha256": digest, "t0": float(window.time[0]), "t1": float(window.time[-1]),
                  "samples": int(window.time.size), "rate_hz": capture.rate_hz,
                  "selection": selection}
        return window, source

    def motor_params(self):
        """电机参数（数字孪生 PMSMParams）；取不到时用铭牌默认值。"""
        if callable(self.params_provider):
            try:
                return self.params_provider()
            except Exception:  # noqa: BLE001
                pass
        from communications.motor_sim import PMSMParams
        return PMSMParams()

    def power_params(self) -> PowerParams:
        if callable(self.params_provider):
            try:
                return PowerParams.from_motor(self.params_provider())
            except Exception:  # noqa: BLE001
                pass
        return PowerParams()


# ─── 工具实现 ────────────────────────────────────────────────
def downsample_speed(time, speed, rate_hz: float = 1000.0):
    """转速测速只有 500 Hz 更新：降到 1 kHz 再做阶跃分析，避免 16 kHz 台阶放大导数噪声。"""
    t = np.asarray(time, float)
    y = np.asarray(speed, float)
    step = max(1, int(round((1.0 / rate_hz) / float(np.median(np.diff(t))))))
    return t[::step], y[::step]


def stft_for(time, values, rate_hz: float) -> dict:
    """STFT 窗长约 0.1 s（16 kHz 下 2048 点），与 analysis_dynamic 同一实现。"""
    import analysis_dynamic as dyn
    window = int(min(8192, max(256, 2 ** round(np.log2(max(rate_hz * 0.1, 32))))))
    return dyn.stft_map(np.asarray(time, float), np.asarray(values, float), rate_hz,
                        window=window, fmax=min(rate_hz / 2, 2000.0))



def _stats(values) -> dict:
    data = np.asarray(values, float)
    return {"mean": float(np.mean(data)), "rms": float(np.sqrt(np.mean(data ** 2))),
            "std": float(np.std(data)), "min": float(np.min(data)), "max": float(np.max(data)),
            "peak_to_peak": float(np.ptp(data))}


def _fe_hz(window: Capture) -> float | None:
    if window.theta is None or window.time.size < 2:
        return None
    return float(np.polyfit(window.time - window.time[0], window.theta, 1)[0] / (2 * np.pi))


def make_tools(data: ExperimentData) -> list[ToolSpec]:
    window_props = {
        "experiment_id": {"type": "string", "description": "实验编号"},
        "batch": {"type": "string", "description": "波形批次（省略则取最后一次保存）"},
        "t0": {"type": "number", "description": "起始时间 s（省略=整段）"},
        "t1": {"type": "number", "description": "结束时间 s（省略=整段）"},
    }

    def schema(extra: dict | None = None, required=("experiment_id",)) -> dict:
        props = dict(window_props)
        props.update(extra or {})
        return {"type": "object", "properties": props, "required": list(required),
                "additionalProperties": False}

    def list_experiment_data(arguments: dict, _context: ToolContext) -> ToolResult:
        experiment_id = arguments["experiment_id"]
        session = data.repository.load(experiment_id)
        folder = data.session_dir(experiment_id)
        media = read_media_manifest(folder / "media")
        return ToolResult(ok=True, data=_json({
            "experiment_id": experiment_id, "name": session.name,
            "status": session.status.value, "started_at": session.started_at,
            "ended_at": session.ended_at, "waveform_batches": waveform_batches(folder),
            "media": media}))

    def experiment_overview(arguments: dict, _context: ToolContext) -> ToolResult:
        from experiments.telemetry import summarize_telemetry
        experiment_id = arguments["experiment_id"]
        session = data.repository.load(experiment_id)
        telemetry = data.repository.read_telemetry(experiment_id)
        return ToolResult(ok=True, data=_json({
            "experiment_id": experiment_id, "name": session.name, "purpose": session.purpose,
            "operator": session.operator, "data_source": session.data_source,
            "status": session.status.value, "end_reason": session.end_reason,
            "controller_params": session.controller_params,
            "protection_params": session.protection_params,
            "device": session.device.to_dict() if session.device else {},
            "telemetry_summary": summarize_telemetry(telemetry),
            "user_conclusion": {k: v for k, v in session.conclusion.items() if k != "ai_draft"},
        }))

    def capture_statistics(arguments: dict, _context: ToolContext) -> ToolResult:
        window, source = data.window(arguments)
        result = {"duration_s": float(window.time[-1] - window.time[0]),
                  "ia_a": _stats(window.ia), "ib_a": _stats(window.ib),
                  "electrical_frequency_hz": _fe_hz(window)}
        if window.speed_rpm is not None:
            result["speed_rpm"] = _stats(window.speed_rpm)
        for name in ("iq_a", "iqref_a", "vbus_v"):
            if name in window.extra:
                result[name] = _stats(window.extra[name])
        if {"iq_a", "iqref_a"} <= set(window.extra):
            result["iq_tracking_error_rms_a"] = float(np.sqrt(np.mean(
                (window.extra["iqref_a"] - window.extra["iq_a"]) ** 2)))
        return ToolResult(ok=True, data=_json(result), metadata={"source": source})

    def capture_spectrum(arguments: dict, _context: ToolContext) -> ToolResult:
        from pages.fourier_page import compute_spectrum
        window, source = data.window(arguments, prefer_steady=True)
        channel = arguments["channel"]
        count = int(arguments.get("peaks", 8))
        if channel == "current_complex":
            alpha = window.ia
            beta = (window.ia + 2.0 * window.ib) / math.sqrt(3.0)
            z = alpha + 1j * beta
            hann = np.hanning(z.size)
            spectrum = np.fft.fftshift(np.fft.fft(z * hann)) / hann.sum()
            freqs = np.fft.fftshift(np.fft.fftfreq(z.size, 1.0 / window.rate_hz))
            amps = np.abs(spectrum)
            two_sided = True
        else:
            if channel in ("ia_a", "ib_a"):
                values = window.ia if channel == "ia_a" else window.ib
            elif channel == "speed_rpm":
                if window.speed_rpm is None:
                    return ToolResult(ok=False, error="capture_missing_channel:speed_rpm")
                values = window.speed_rpm
            else:
                if channel not in window.extra:
                    return ToolResult(ok=False, error=f"capture_missing_channel:{channel}")
                values = window.extra[channel]
            spec = compute_spectrum(values, window.rate_hz, window_name="hann",
                                    remove_dc=bool(arguments.get("remove_dc", True)))
            freqs, amps = spec["frequencies"], spec["amplitudes"]
            two_sided = False
        local = np.flatnonzero((amps[1:-1] > amps[:-2]) & (amps[1:-1] >= amps[2:])) + 1
        if not two_sided:
            local = local[freqs[local] > 0.0]
        ranked = local[np.argsort(amps[local])[::-1][:count]] if local.size else []
        fe = _fe_hz(window)
        peaks = []
        for index in ranked:
            f = float(freqs[index])
            peak = {"frequency_hz": f, "amplitude": float(amps[index])}
            if fe and abs(fe) > 1.0:
                peak["order_of_fe"] = round(f / fe, 2)
            peaks.append(peak)
        return ToolResult(ok=True, data=_json({
            "channel": channel, "two_sided": two_sided,
            "resolution_hz": float(window.rate_hz / window.time.size),
            "electrical_frequency_hz": fe, "peaks": peaks}),
            metadata={"source": source, "window": "hann",
                      "amplitude_definition": "峰值幅值" + ("（复数双边）" if two_sided else "（单边）")})

    def decompose_current_vector(arguments: dict, _context: ToolContext) -> ToolResult:
        window, source = data.window(arguments, prefer_steady=True)
        analysis = analyze(window, float(window.time[0]), float(window.time[-1]),
                           block_s=float(arguments.get("block_s", 0.25)))
        z = analysis.z
        shape = trajectory_shape(z.real, z.imag)
        estimate = estimate_params(shape)
        return ToolResult(ok=True, data=_json({
            "electrical_frequency_hz": analysis.fe_hz,
            "residual_pct_of_fundamental": analysis.residual_pct,
            "components": [{
                "key": c.key, "label": c.label, "stationary_freq_hz": c.freq_hz,
                "dq_ripple_freq_hz": c.dq_freq_hz, "amplitude_a": c.amp,
                "pct_of_fundamental": 100.0 * c.amp / max(analysis.components[0].amp, 1e-12),
            } for c in analysis.components],
            "oscillation_pairs": [{
                "freq_hz": p.freq_hz, "linearity": p.linearity, "axis_deg": p.axis_deg,
            } for p in find_pairs(analysis)],
            "roundness": None if shape is None else {
                "mean_radius_a": shape.mean_radius, "eccentric_pct": shape.eccentric_pct,
                "ellipse_pct": shape.ellipse_pct, "ellipse_axis_deg": shape.ellipse_axis_deg,
                "triangle_pct": shape.triangle_pct, "hexagon_pct": shape.hexagon_pct},
            "equivalent_distortion": None if estimate is None else estimate.__dict__,
            "possible_causes": diagnose(shape),
        }), metadata={"source": source, "method": "阶次锁相 + 残差峰值，分段最小二乘"})

    def step_response(arguments: dict, _context: ToolContext) -> ToolResult:
        """转速阶跃响应（复用 analysis_dynamic.suggest_step / response_metrics）。"""
        import analysis_dynamic as dyn
        window, source = data.window(arguments)
        if window.speed_rpm is None:
            return ToolResult(ok=False, error="capture_missing_channel:speed_rpm")
        t, y = downsample_speed(window.time, window.speed_rpm)
        step = dyn.suggest_step(t, y)
        if step is None:
            return ToolResult(ok=True, data={"step_found": False,
                                             "note": "记录里没有可识别的转速阶跃"},
                              metadata={"source": source})
        result = dyn.response_metrics(t, y, step["event"], step["target"])
        return ToolResult(ok=True, data=_json({
            "step_found": True, "event_s": step["event"], "event_basis": step["basis"],
            "step_valid": result["step_valid"], "message": result["message"],
            "metrics": result["metrics"]}), metadata={
            "source": source, "method": "analysis_dynamic.response_metrics（10%→90% 上升、2% 调节带）"})

    def time_frequency(arguments: dict, _context: ToolContext) -> ToolResult:
        """STFT 时频（复用 analysis_dynamic.stft_map），给出主峰频率随时间的变化。"""
        import analysis_dynamic as dyn
        window, source = data.window(arguments)
        channel = arguments.get("channel", "iq_a")
        values = window.speed_rpm if channel == "speed_rpm" else window.extra.get(channel)
        if values is None:
            return ToolResult(ok=False, error=f"capture_missing_channel:{channel}")
        stft = stft_for(window.time, values, window.rate_hz)
        power, freqs = stft["power"], stft["axis"]
        band = freqs > max(2.0, stft["resolution"] * 2)
        dominant = freqs[band][np.argmax(power[band], axis=0)] if band.any() else []
        picks = np.linspace(0, len(stft["time"]) - 1, min(12, len(stft["time"]))).astype(int)
        return ToolResult(ok=True, data=_json({
            "channel": channel, "resolution_hz": stft["resolution"], "window_s": stft["window_s"],
            "dominant_frequency_track": [
                {"time_s": float(stft["time"][i]), "frequency_hz": float(dominant[i])}
                for i in picks] if len(dominant) else []}),
            metadata={"source": source, "method": "analysis_dynamic.stft_map（Hann，PSD）"})

    def power_balance(arguments: dict, _context: ToolContext) -> ToolResult:
        window, source = data.window(arguments)
        if "iq_a" not in window.extra or "vq_raw" not in window.extra:
            return ToolResult(ok=False, error="capture_missing_channel:iq_a/vq_raw")
        session = data.repository.load(arguments["experiment_id"])
        recorded = ((session.device.extra if session.device else {}) or {}).get("dyno_load", {})
        mode = arguments.get("load_mode") or recorded.get("mode") or "unknown"
        params = data.power_params().with_dyno(mode, data.motor_params(),
                                               recorded.get("load_inertia_kgm2"))
        summary = power_summary(estimate_power_series(window.time, window.columns(), params))
        mean = summary["mean_w"]
        # 逆变器输入 − 铜损 − 电磁功率：铁损、死区电压误差、参数误差等未建模部分
        summary["unexplained_w"] = mean["inv"] - mean["cu"] - mean["em"]
        summary["parameters"] = {"rs_ohm": params.rs_ohm, "kt_nm_per_a": params.kt_nm_per_a,
                                 "inertia": params.inertia, "load_inertia": params.load_inertia,
                                 "total_inertia": params.total_inertia, "pole_pairs": params.pole_pairs,
                                 "load_mode": mode, "load_short_model": params.load_short is not None}
        return ToolResult(ok=True, data=_json(summary), metadata={
            "source": source, "note": "无母线电流：电源输入按逆变器输入计；开关损耗忽略"})

    def dyno_load_check(arguments: dict, _context: ToolContext) -> ToolResult:
        """对拖负载：负载驱动器上电未启动（绕组短接）时的短路制动模型与实测转矩对比。"""
        from core import dyno_load
        experiment_id = arguments["experiment_id"]
        session = data.repository.load(experiment_id)
        recorded = ((session.device.extra if session.device else {}) or {}).get("dyno_load", {})
        mode = arguments.get("load_mode") or recorded.get("mode") or "unknown"
        extra_r = float(arguments.get("extra_r_ohm", recorded.get("extra_r_ohm", 0.0)) or 0.0)
        motor_params = data.motor_params()
        kt = data.power_params().kt_nm_per_a
        motor = dyno_load.LoadMotor.from_motor(motor_params, kt, extra_r)
        nameplate = dyno_load.LoadMotor.from_motor(motor_params, None, extra_r)
        result = {"load_mode": mode, "load_mode_text": dyno_load.LOAD_MODES.get(mode, mode),
                  "mode_source": "生成日志时指定" if arguments.get("load_mode") else
                  ("实验记录" if recorded.get("mode") else "未记录"),
                  "assumption": "负载电机与驱动电机参数相同（同型号对拖）",
                  "motor": {"rs_ohm": motor.rs_ohm, "ls_h": motor.ls_h, "psi_wb": motor.psi_wb,
                            "psi_basis": f"ψ = kt/(1.5p)，kt = {kt} N·m/A（与转矩测量同一口径）",
                            "nameplate_psi_wb": nameplate.psi_wb,
                            "pole_pairs": motor.pole_pairs, "extra_r_ohm": extra_r},
                  "characteristics": dyno_load.brake_characteristics(motor)}
        window, source = data.window(arguments)
        if window.speed_rpm is None or "iq_a" not in window.extra:
            return ToolResult(ok=False, error="capture_missing_channel:speed_rpm/iq_a")
        try:
            compare = dyno_load.compare_with_measurement(
                window.time, window.speed_rpm, window.extra["iq_a"], kt, motor)
            alt = dyno_load.compare_with_measurement(
                window.time, window.speed_rpm, window.extra["iq_a"],
                1.5 * nameplate.pole_pairs * nameplate.psi_wb, nameplate)
        except ValueError as exc:
            result["levels"] = []
            result["note"] = str(exc)
        else:
            result["levels"] = compare["levels"]
            result["levels_nameplate_psi"] = alt["levels"]
        return ToolResult(ok=True, data=_json(result), metadata={
            "source": source, "method": "负载电机三相短接稳态解（Ld=Lq），实测转矩 = kt·iq（50 ms 块平均）"})

    def compare_parameters(arguments: dict, _context: ToolContext) -> ToolResult:
        baseline = data.repository.load(arguments["baseline_id"])
        other = data.repository.load(arguments["experiment_id"])

        def flat(prefix, value, out):
            if isinstance(value, dict):
                for key, item in value.items():
                    flat(f"{prefix}.{key}" if prefix else str(key), item, out)
            else:
                out[prefix] = value
            return out

        left = flat("", {"controller": baseline.controller_params,
                         "protection": baseline.protection_params,
                         "device": baseline.device.to_dict() if baseline.device else {}}, {})
        right = flat("", {"controller": other.controller_params,
                          "protection": other.protection_params,
                          "device": other.device.to_dict() if other.device else {}}, {})
        changes = [{"parameter": key, "baseline": left.get(key), "value": right.get(key)}
                   for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]
        return ToolResult(ok=True, data=_json({
            "baseline_id": baseline.experiment_id, "experiment_id": other.experiment_id,
            "changed": changes, "unchanged_count": len(set(left) & set(right)) - sum(
                1 for c in changes if c["parameter"] in left and c["parameter"] in right)}))

    tools = [
        ("list_experiment_data", "列出实验的波形保存批次、高速数据文件与音视频记录",
         {"type": "object", "properties": {"experiment_id": window_props["experiment_id"]},
          "required": ["experiment_id"], "additionalProperties": False},
         list_experiment_data, 10.0),
        ("experiment_overview", "实验元数据、控制/保护参数快照、慢遥测统计与操作者结论",
         {"type": "object", "properties": {"experiment_id": window_props["experiment_id"]},
          "required": ["experiment_id"], "additionalProperties": False},
         experiment_overview, 20.0),
        ("capture_statistics", "高速数据时域统计：转速、iq、Ia/Ib、母线电压、电频率",
         schema(), capture_statistics, 30.0),
        ("capture_spectrum", "高速数据频谱峰值（iq/转速/相电流单边谱，或电流复数双边谱）",
         schema({"channel": {"type": "string", "enum": list(SPECTRUM_CHANNELS)},
                 "peaks": {"type": "integer", "minimum": 1, "maximum": 20},
                 "remove_dc": {"type": "boolean"}}, ("experiment_id", "channel")),
         capture_spectrum, 30.0),
        ("decompose_current_vector", "电流矢量分量分解、圆度与畸变原因",
         schema({"block_s": {"type": "number", "minimum": 0.02, "maximum": 5.0}}),
         decompose_current_vector, 60.0),
        ("power_balance", "能量链路平均功率、能量、电机效率与未解释功率（对拖时计入负载侧惯量与短路铜损）",
         schema({"load_mode": {"type": "string", "enum": ["unknown", "none", "off", "shorted", "torque"]}}),
         power_balance, 30.0),
        ("step_response", "转速阶跃响应：上升时间、超调、调节时间、稳态误差（自动定位阶跃）",
         schema(), step_response, 30.0),
        ("time_frequency", "STFT 时频分析：主峰频率随时间的变化（iq 或转速）",
         schema({"channel": {"type": "string", "enum": ["iq_a", "speed_rpm", "ia_a"]}}),
         time_frequency, 60.0),
        ("dyno_load_check", "对拖负载：负载驱动器上电未启动（绕组短接）的制动转矩模型与实测对比",
         schema({"load_mode": {"type": "string", "enum": ["unknown", "none", "off", "shorted", "torque"]},
                 "extra_r_ohm": {"type": "number", "minimum": 0.0, "maximum": 2.0}}),
         dyno_load_check, 30.0),
        ("compare_parameters", "对比两个实验的控制/保护/设备参数，只列出不同项",
         {"type": "object", "properties": {
             "baseline_id": {"type": "string"}, "experiment_id": {"type": "string"}},
          "required": ["baseline_id", "experiment_id"], "additionalProperties": False},
         compare_parameters, 10.0),
    ]
    return [ToolSpec(name, description, input_schema, "read_only", timeout, handler)
            for name, description, input_schema, handler, timeout in tools]


def create_experiment_registry(repository, params_provider=None) -> tuple[ToolRegistry, ExperimentData]:
    data = ExperimentData(repository, params_provider)
    registry = ToolRegistry()
    for spec in make_tools(data):
        registry.register(spec)
    return registry, data
