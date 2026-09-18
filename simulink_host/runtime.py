"""ctypes binding and worker thread for the generated Simulink C++ model."""
from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from runtime_paths import app_base_dir, resource_path


@dataclass(frozen=True)
class SimulinkHostInputs:
    speed_ref_rpm: float = 500.0
    id_ref_a: float = 0.0
    load_torque_nm: float = 0.0


@dataclass(frozen=True)
class SimulinkHostParameters:
    speed_kp: float = 0.8
    speed_ki: float = 3.5
    iq_limit_a: float = 5.0
    current_noise_variance: float = 0.006


@dataclass(frozen=True)
class SimulinkHostOutputs:
    speed_rpm: float
    id_a: float
    iq_a: float
    id_ref_a: float
    iq_ref_a: float
    ud_v: float
    uq_v: float
    id_hat_a: float
    iq_hat_a: float
    theta_e_rad: float
    gate_abc: tuple[float, ...]
    rls_param_d: tuple[float, ...]
    rls_param_q: tuple[float, ...]
    torque_nm: float
    simulation_time_s: float


class _Inputs(ctypes.Structure):
    _fields_ = [
        ("speed_ref_rpm", ctypes.c_double),
        ("id_ref_a", ctypes.c_double),
        ("load_torque_nm", ctypes.c_double),
    ]


class _Parameters(ctypes.Structure):
    _fields_ = [
        ("speed_kp", ctypes.c_double),
        ("speed_ki", ctypes.c_double),
        ("iq_limit_a", ctypes.c_double),
        ("current_noise_variance", ctypes.c_double),
    ]


class _Outputs(ctypes.Structure):
    _fields_ = [
        ("speed_rpm", ctypes.c_double),
        ("id_a", ctypes.c_double),
        ("iq_a", ctypes.c_double),
        ("id_ref_a", ctypes.c_double),
        ("iq_ref_a", ctypes.c_double),
        ("ud_v", ctypes.c_double),
        ("uq_v", ctypes.c_double),
        ("id_hat_a", ctypes.c_double),
        ("iq_hat_a", ctypes.c_double),
        ("theta_e_rad", ctypes.c_double),
        ("gate_abc", ctypes.c_double * 6),
        ("rls_param_d", ctypes.c_double * 7),
        ("rls_param_q", ctypes.c_double * 7),
        ("torque_nm", ctypes.c_double),
        ("simulation_time_s", ctypes.c_double),
    ]


def resolve_simulink_dll() -> Path | None:
    """Resolve the generated-model DLL in source and packaged layouts."""
    configured = os.getenv("PMSM_SIMULINK_HOST_DLL", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        resource_path("pmsm_simulink_host.dll"),
        resource_path("simulink_host", "bin", "pmsm_simulink_host.dll"),
        app_base_dir() / "pmsm_simulink_host.dll",
        app_base_dir() / "build" / "simulink_host_bridge" / "Release"
        / "pmsm_simulink_host.dll",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


class SimulinkHostRuntime:
    """Single-instance binding for the generated C ABI."""

    def __init__(self, dll_path: str | os.PathLike[str]) -> None:
        self.dll_path = Path(dll_path).resolve()
        if not self.dll_path.is_file():
            raise FileNotFoundError(f"Simulink C++ DLL 不存在：{self.dll_path}")
        self._dll = ctypes.CDLL(str(self.dll_path))
        self._configure_signatures()
        self._initialized = False

    def _configure_signatures(self) -> None:
        dll = self._dll
        dll.pmsm_host_initialize.restype = ctypes.c_int
        dll.pmsm_host_terminate.restype = None
        dll.pmsm_host_set_inputs.argtypes = [ctypes.POINTER(_Inputs)]
        dll.pmsm_host_set_inputs.restype = ctypes.c_int
        dll.pmsm_host_set_parameters.argtypes = [ctypes.POINTER(_Parameters)]
        dll.pmsm_host_set_parameters.restype = ctypes.c_int
        dll.pmsm_host_get_parameters.argtypes = [ctypes.POINTER(_Parameters)]
        dll.pmsm_host_get_parameters.restype = ctypes.c_int
        dll.pmsm_host_step.argtypes = [ctypes.c_uint32]
        dll.pmsm_host_step.restype = ctypes.c_int
        dll.pmsm_host_get_outputs.argtypes = [ctypes.POINTER(_Outputs)]
        dll.pmsm_host_get_outputs.restype = ctypes.c_int
        dll.pmsm_host_error.restype = ctypes.c_char_p

    def initialize(self) -> None:
        result = self._dll.pmsm_host_initialize()
        if result != 0:
            raise RuntimeError(self.error() or "Simulink 模型初始化失败")
        self._initialized = True

    def close(self) -> None:
        if self._initialized:
            self._dll.pmsm_host_terminate()
            self._initialized = False

    def set_inputs(self, values: SimulinkHostInputs) -> None:
        raw = _Inputs(values.speed_ref_rpm, values.id_ref_a,
                      values.load_torque_nm)
        if self._dll.pmsm_host_set_inputs(ctypes.byref(raw)) != 0:
            raise ValueError("Simulink 模型输入包含无效数值")

    def set_parameters(self, values: SimulinkHostParameters) -> None:
        raw = _Parameters(values.speed_kp, values.speed_ki,
                          values.iq_limit_a,
                          values.current_noise_variance)
        if self._dll.pmsm_host_set_parameters(ctypes.byref(raw)) != 0:
            raise ValueError("Simulink 模型参数越界或包含无效数值")

    def get_parameters(self) -> SimulinkHostParameters:
        raw = _Parameters()
        if self._dll.pmsm_host_get_parameters(ctypes.byref(raw)) != 0:
            raise RuntimeError("读取 Simulink 模型参数失败")
        return SimulinkHostParameters(
            raw.speed_kp, raw.speed_ki, raw.iq_limit_a,
            raw.current_noise_variance)

    def step(self, step_count: int) -> None:
        if not self._initialized:
            raise RuntimeError("Simulink 模型尚未初始化")
        if not 0 < int(step_count) <= 0xFFFFFFFF:
            raise ValueError("step_count 必须处于 1..2^32-1")
        result = self._dll.pmsm_host_step(int(step_count))
        if result != 0:
            raise RuntimeError(self.error() or "Simulink 模型步进失败")

    def get_outputs(self) -> SimulinkHostOutputs:
        if not self._initialized:
            raise RuntimeError("Simulink 模型尚未初始化")
        raw = _Outputs()
        if self._dll.pmsm_host_get_outputs(ctypes.byref(raw)) != 0:
            raise RuntimeError("读取 Simulink 模型输出失败")
        return SimulinkHostOutputs(
            speed_rpm=raw.speed_rpm,
            id_a=raw.id_a,
            iq_a=raw.iq_a,
            id_ref_a=raw.id_ref_a,
            iq_ref_a=raw.iq_ref_a,
            ud_v=raw.ud_v,
            uq_v=raw.uq_v,
            id_hat_a=raw.id_hat_a,
            iq_hat_a=raw.iq_hat_a,
            theta_e_rad=raw.theta_e_rad,
            gate_abc=tuple(raw.gate_abc),
            rls_param_d=tuple(raw.rls_param_d),
            rls_param_q=tuple(raw.rls_param_q),
            torque_nm=raw.torque_nm,
            simulation_time_s=raw.simulation_time_s,
        )

    def error(self) -> str:
        raw = self._dll.pmsm_host_error()
        return raw.decode("utf-8", errors="replace") if raw else ""

    def __enter__(self) -> "SimulinkHostRuntime":
        self.initialize()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class SimulinkEngineThread(QThread):
    """Runs the 10 us generated model in paced batches off the UI thread."""

    sampleReady = Signal(object)
    stateChanged = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        dll_path: str | os.PathLike[str],
        inputs: SimulinkHostInputs,
        parameters: SimulinkHostParameters,
        *,
        batch_steps: int = 1000,
        emit_steps: int = 2000,
        runtime_factory: Callable[..., SimulinkHostRuntime] = SimulinkHostRuntime,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._dll_path = Path(dll_path)
        self._inputs = inputs
        self._parameters = parameters
        self._batch_steps = max(1, int(batch_steps))
        self._emit_steps = max(self._batch_steps, int(emit_steps))
        self._runtime_factory = runtime_factory
        self._lock = threading.Lock()
        self._stop_requested = False
        self._paused = False
        self._reset_requested = False
        self._parameters_dirty = True
        # UI 只需要最新状态。使用单槽邮箱而不是持续排队 Qt 信号，避免
        # 显卡重绘偶尔变慢时旧帧无限堆积并最终拖死主线程。
        self._latest_sample: dict | None = None

    def update_inputs(self, values: SimulinkHostInputs) -> None:
        with self._lock:
            self._inputs = values

    def update_parameters(self, values: SimulinkHostParameters) -> None:
        with self._lock:
            self._parameters = values
            self._parameters_dirty = True

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)

    def reset_model(self) -> None:
        with self._lock:
            self._reset_requested = True

    def stop_model(self) -> None:
        with self._lock:
            self._stop_requested = True

    def take_latest_sample(self) -> dict | None:
        """原子取走最新显示帧；期间产生的旧帧会被自然覆盖。"""
        with self._lock:
            sample = self._latest_sample
            self._latest_sample = None
            return sample

    def _store_latest_sample(self, sample: dict) -> None:
        with self._lock:
            self._latest_sample = sample

    def _command_snapshot(self):
        with self._lock:
            snapshot = (
                self._stop_requested,
                self._paused,
                self._reset_requested,
                self._parameters_dirty,
                self._inputs,
                self._parameters,
            )
            self._reset_requested = False
            self._parameters_dirty = False
            return snapshot

    def run(self) -> None:
        runtime = None
        try:
            runtime = self._runtime_factory(self._dll_path)
            runtime.initialize()
            runtime.set_inputs(self._inputs)
            runtime.set_parameters(self._parameters)
            self.stateChanged.emit("running")

            step_seconds = 1.0e-5
            emitted_at = 0
            wall_anchor = time.perf_counter()
            sim_anchor = 0.0
            was_paused = False

            while True:
                (stop_requested, paused, reset_requested, parameters_dirty,
                 inputs, parameters) = self._command_snapshot()
                if stop_requested:
                    break
                if reset_requested:
                    runtime.initialize()
                    runtime.set_inputs(inputs)
                    runtime.set_parameters(parameters)
                    emitted_at = 0
                    wall_anchor = time.perf_counter()
                    sim_anchor = 0.0
                    self.stateChanged.emit("running")
                if paused:
                    runtime.set_inputs(inputs)
                    if parameters_dirty:
                        runtime.set_parameters(parameters)
                    if not was_paused:
                        self.stateChanged.emit("paused")
                        was_paused = True
                    time.sleep(0.01)
                    wall_anchor = time.perf_counter()
                    current = runtime.get_outputs()
                    sim_anchor = current.simulation_time_s
                    continue
                if was_paused:
                    self.stateChanged.emit("running")
                    was_paused = False

                runtime.set_inputs(inputs)
                if parameters_dirty:
                    runtime.set_parameters(parameters)
                runtime.step(self._batch_steps)
                current = runtime.get_outputs()
                current_steps = int(round(current.simulation_time_s / step_seconds))
                if current_steps - emitted_at >= self._emit_steps:
                    sample = asdict(current)
                    sample["speed_ref_rpm"] = inputs.speed_ref_rpm
                    sample["load_torque_ref_nm"] = inputs.load_torque_nm
                    self._store_latest_sample(sample)
                    emitted_at = current_steps

                target_wall = wall_anchor + (
                    current.simulation_time_s - sim_anchor)
                delay = target_wall - time.perf_counter()
                if delay > 0.0:
                    time.sleep(min(delay, 0.02))
                elif delay < -0.25:
                    wall_anchor = time.perf_counter()
                    sim_anchor = current.simulation_time_s
        except Exception as exc:  # thread boundary: report to UI
            self.failed.emit(str(exc))
        finally:
            if runtime is not None:
                runtime.close()
            self.stateChanged.emit("stopped")
