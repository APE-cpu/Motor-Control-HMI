"""Laya 本地决策模型客户端与原生/ONNX进程管理。"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Mapping

from ai.jev_client import JevClient


_BUNDLE_FILES = (
    "laya.onnx",
    "laya.onnx.data",
    "laya_config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)

_NATIVE_BUNDLE_FILES = (
    "model.safetensors",
    "rl_agent_config.json",
    "encoder/config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)


class LayaClient(JevClient):
    """只连接loopback，并按配置启动laya.cpp或兼容Node/ONNX运行时。"""

    def __init__(
            self, base_url: str, service_dir: str | Path | None = None,
            startup_timeout: int = 180, *,
            native_executable: str | Path | None = None,
            native_model_dir: str | Path | None = None,
            backend: str = "vulkan", precision: str = "bf16",
            device: int = 0) -> None:
        parsed = urlsplit(base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Laya 仅允许连接本机 loopback 地址")
        super().__init__(base_url, "", "laya")
        self.service_dir = Path(service_dir) if service_dir else None
        self.native_executable = (
            Path(native_executable) if native_executable else None)
        self.native_model_dir = (
            Path(native_model_dir) if native_model_dir else None)
        if (self.native_executable is None) != (self.native_model_dir is None):
            raise ValueError("laya.cpp可执行文件与模型目录必须同时配置")
        self.is_native = self.native_executable is not None
        self.backend = backend.strip().lower()
        self.precision = precision.strip().lower()
        if self.backend not in {"vulkan", "cpu"}:
            raise ValueError(f"不支持的Laya后端：{backend}")
        if self.precision not in {"fp32", "fp16", "bf16"}:
            raise ValueError(f"不支持的Laya精度：{precision}")
        if self.backend == "cpu" and self.precision != "fp32":
            raise ValueError("laya.cpp CPU后端当前仅使用FP32")
        self.device = max(0, int(device))
        self.startup_timeout = max(1, int(startup_timeout))
        self._process: subprocess.Popen | None = None
        atexit.register(self.close)

    @property
    def runtime_label(self) -> str:
        if self.is_native:
            return f"laya.cpp / {self.backend.upper()} / {self.precision.upper()}"
        return "Node / ONNX Runtime / CPU"

    def health(self, timeout: int = 2) -> dict | None:
        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None

    def _validate_runtime(self) -> None:
        if self.is_native:
            if not self.native_executable.is_file():
                raise RuntimeError(
                    f"laya.cpp程序不存在：{self.native_executable}")
            missing = [name for name in _NATIVE_BUNDLE_FILES
                       if not (self.native_model_dir / Path(name)).is_file()]
            if missing:
                raise RuntimeError(
                    "laya.cpp模型文件不完整：" + "、".join(missing))
            return
        if self.service_dir is None:
            raise RuntimeError("未配置Laya Node服务目录")
        if not (self.service_dir / "server.mjs").is_file():
            raise RuntimeError(f"Laya 服务脚本不存在：{self.service_dir / 'server.mjs'}")
        bundle = (self.service_dir / "model-cache" /
                  "receptron--laya-onnx" / "main")
        missing = [name for name in _BUNDLE_FILES
                   if not (bundle / Path(name)).is_file()]
        if missing:
            raise RuntimeError("Laya 模型文件不完整：" + "、".join(missing))
        if shutil.which("node") is None:
            raise RuntimeError("未找到 Node.js；Laya 需要 Node.js 20 或更高版本")

    def _start_service(self) -> None:
        self._validate_runtime()
        env = os.environ.copy()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if self.is_native:
            parsed = urlsplit(self.base_url)
            port = parsed.port or 80
            args = [
                str(self.native_executable), f"--{self.backend}",
                f"--{self.precision}", "--model", str(self.native_model_dir),
                "--server", "--host", parsed.hostname, "--port", str(port),
                "--no-batching",
            ]
            if self.backend == "vulkan":
                env["GGML_VK_VISIBLE_DEVICES"] = str(self.device)
            cwd = self.native_executable.parent
        else:
            env["LAYA_CACHE"] = str(self.service_dir / "model-cache")
            env.setdefault("LAYA_THREADS", "4")
            args = [shutil.which("node"), "--use-system-ca", "server.mjs"]
            cwd = self.service_dir
        self._process = subprocess.Popen(
            args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)

    def ensure_ready(self, timeout: int | None = None) -> None:
        wait_seconds = self.startup_timeout if timeout is None else max(1, timeout)
        status = self.health()
        # Node兼容服务返回ready，laya.cpp原生服务返回ok。
        if status and status.get("status") in {"ready", "ok"}:
            return
        if status and status.get("status") == "error":
            raise RuntimeError(f"Laya 本地服务加载失败：{status.get('error') or '未知错误'}")
        if status is None:
            self._start_service()

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(
                    f"Laya本地服务在模型加载期间异常退出"
                    f"（{self.runtime_label}，退出码{self._process.returncode}）")
            time.sleep(0.25)
            status = self.health()
            if status and status.get("status") in {"ready", "ok"}:
                return
            if status and status.get("status") == "error":
                raise RuntimeError(
                    f"Laya 本地服务加载失败：{status.get('error') or '未知错误'}")
        raise RuntimeError(
            f"Laya模型在{wait_seconds}s内未就绪（{self.runtime_label}）")

    def decide(self, state: object, questions: Mapping[str, object],
               timeout: int = 30) -> dict:
        self.ensure_ready()
        payload = {"state": state, "questions": dict(questions)}
        endpoint = "/v1/systemone" if self.is_native else "/system-one"
        req = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MotorControlHMI/2.0.0 (Laya local client)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error", body)
            except Exception:
                message = body
            raise RuntimeError(f"Laya HTTP {exc.code}: {message}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Laya 本地服务连接失败：{exc.reason}") from None
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise RuntimeError("Laya 响应格式无效")
        return result

    @classmethod
    def format_diagnosis(cls, data: Mapping) -> str:
        text = super().format_diagnosis(data)
        text = text.replace("JEV 结构化诊断", "Laya 本地结构化诊断", 1)
        old_note = ("JEV 不生成长文本解释；以上是快速结构化判定，"
                    "关键结论仍需结合原始波形、FFT 与控制参数复核。")
        new_note = ("Laya 不生成长文本解释；本次推理仅在本机完成。以上是快速"
                    "结构化判定，关键结论仍需结合原始波形、FFT 与控制参数复核。")
        text = text.replace(old_note, new_note, 1)
        answers = data.get("answers", data)
        if isinstance(answers, Mapping):
            state = answers.get("operating_state", {})
            confidence = state.get("confidence") if isinstance(state, Mapping) else None
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = 1.0
            if confidence_value < 0.5:
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    if line.startswith("- 运行状态："):
                        lines[index] = (
                            "- 运行状态：模型不确定（置信度："
                            f"{cls._percent(confidence_value)}）")
                        break
                text = "\n".join(lines)

            action = answers.get("next_action", {})
            action_confidence = (
                action.get("confidence") if isinstance(action, Mapping) else None)
            try:
                action_confidence_value = float(action_confidence)
            except (TypeError, ValueError):
                action_confidence_value = 1.0
            if action_confidence_value < 0.5:
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    if line.startswith("- 建议动作："):
                        lines[index] = "- 建议动作：低置信度，不执行自动处置"
                        break
                text = "\n".join(lines)
        return text

    @staticmethod
    def _number(snapshot: str, *labels: str) -> float | None:
        number = r"([-+]?\d+(?:\.\d+)?)"
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*[=:：]\s*{number}", snapshot,
                              flags=re.IGNORECASE)
            if match:
                return float(match.group(1))
        return None

    @classmethod
    def _diagnostic_state(cls, question: str, snapshot: str) -> dict:
        fields = {
            "actual_speed_rpm": cls._number(
                snapshot, "实际转速", "actual_speed", "speed_actual"),
            "target_speed_rpm": cls._number(
                snapshot, "给定转速", "目标转速", "target_speed", "speed_target"),
            "actual_current_a": cls._number(
                snapshot, "实际电流", "actual_current", "current_actual"),
            "target_current_a": cls._number(
                snapshot, "给定电流", "目标电流", "target_current", "current_target"),
            "actual_torque_nm": cls._number(
                snapshot, "实际转矩", "actual_torque", "torque_actual"),
            "target_torque_nm": cls._number(
                snapshot, "给定转矩", "目标转矩", "target_torque", "torque_target"),
            "actual_angle_deg": cls._number(
                snapshot, "实际角度", "actual_angle", "angle_actual"),
            "temperature_c": cls._number(
                snapshot, "温度", "temperature", "temperature_c"),
            "speed_kp": cls._number(snapshot, "Kp", "speed_kp"),
            "speed_ki": cls._number(snapshot, "Ki", "speed_ki"),
            "current_limit_a": cls._number(
                snapshot, "Iq限流", "current_limit", "current_limit_a"),
        }
        telemetry = {key: value for key, value in fields.items()
                     if value is not None}
        derived = {}
        actual_speed = telemetry.get("actual_speed_rpm")
        target_speed = telemetry.get("target_speed_rpm")
        if actual_speed is not None and target_speed is not None:
            derived["speed_error_rpm"] = target_speed - actual_speed
            if abs(target_speed) > 1e-9:
                derived["speed_error_percent"] = (
                    abs(target_speed - actual_speed) / abs(target_speed) * 100.0)
        actual_current = telemetry.get("actual_current_a")
        target_current = telemetry.get("target_current_a")
        if actual_current is not None and target_current is not None:
            derived["current_error_a"] = target_current - actual_current
            if abs(target_current) > 1e-9:
                derived["current_error_percent"] = (
                    abs(target_current - actual_current) / abs(target_current) * 100.0)

        if any(word in question for word in ("转速", "速度", "speed")):
            focus = "speed_tracking"
        elif any(word in question for word in ("电流", "current")):
            focus = "current_tracking"
        elif any(word in question for word in ("温度", "temperature")):
            focus = "temperature"
        else:
            focus = "general_diagnosis"
        return {
            "task": "Evaluate PMSM telemetry for diagnostic triage.",
            "requested_focus": focus,
            "telemetry": telemetry,
            "derived": derived,
            "constraints": [
                "Use only supplied numeric evidence.",
                "Do not claim a fault when evidence is insufficient.",
                "This advisory result must not operate the motor.",
            ],
        }

    def diagnose(self, question: str, snapshot: str,
                 timeout: int = 30) -> str:
        questions = {
            "operating_state": {
                "type": "choice",
                "instructions": "Classify the current motor operating state.",
                "criteria": {
                    "normal": "Telemetry supports normal operation.",
                    "watch": "Minor deviation; continue monitoring.",
                    "warning": "Meaningful anomaly requiring investigation.",
                    "fault": "Evidence supports a likely fault or unsafe state.",
                    "insufficient_data": "Evidence is insufficient for a judgment.",
                },
            },
            "severity": {
                "type": "score", "instructions": "Score diagnostic severity.",
                "criteria": ["none", "minor", "moderate", "high", "critical"],
            },
            "needs_human_review": {
                "type": "noul",
                "instructions": "Does this case require review by a control engineer?",
            },
            "next_action": {
                "type": "choice",
                "instructions": "Choose the safest useful next diagnostic action.",
                "criteria": {
                    "continue_monitoring": "Continue monitoring without intervention.",
                    "inspect_signal": "Inspect waveform and spectrum evidence.",
                    "check_control_parameters": "Verify controller parameters and limits.",
                    "stop_and_inspect": "Stop operation and perform a manual inspection.",
                    "collect_more_data": "Collect a longer representative data window.",
                },
            },
        }
        data = self.decide(
            self._diagnostic_state(question, snapshot), questions,
            timeout=timeout)
        return self.format_diagnosis(data)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
