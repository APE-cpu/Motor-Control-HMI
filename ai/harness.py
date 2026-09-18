"""只读AI工具Harness：注册、校验、执行、审计与多轮工具调用。"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    permission: str
    timeout_s: float
    handler: Callable[[dict, "ToolContext"], "ToolResult"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "warnings": self.warnings,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class ToolContext:
    comm: Any
    telemetry_store: "DiagnosticTelemetryStore"
    firmware_config_provider: Callable[[], dict] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def model_tools(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        } for spec in self._tools.values()]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


def _validate_arguments(arguments: Any, schema: dict) -> str | None:
    """校验当前工具用到的JSON Schema子集，拒绝模型生成的多余字段。"""
    if not isinstance(arguments, dict):
        return "参数必须是对象"
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments:
            return f"缺少必需参数 {name}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"包含未知参数 {', '.join(unknown)}"
    for name, value in arguments.items():
        rule = properties.get(name)
        if not rule:
            continue
        expected = rule.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"{name} 必须是字符串"
        if expected == "number" and (not isinstance(value, (int, float)) or
                                      isinstance(value, bool)):
            return f"{name} 必须是数值"
        if expected == "integer" and (not isinstance(value, int) or
                                       isinstance(value, bool)):
            return f"{name} 必须是整数"
        if expected == "boolean" and not isinstance(value, bool):
            return f"{name} 必须是布尔值"
        if expected == "array" and not isinstance(value, list):
            return f"{name} 必须是数组"
        if "enum" in rule and value not in rule["enum"]:
            return f"{name} 不在允许范围内"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                return f"{name} 小于最小值 {rule['minimum']}"
            if "maximum" in rule and value > rule["maximum"]:
                return f"{name} 大于最大值 {rule['maximum']}"
        if isinstance(value, list):
            if "minItems" in rule and len(value) < rule["minItems"]:
                return f"{name} 数量不足"
            if "maxItems" in rule and len(value) > rule["maxItems"]:
                return f"{name} 数量过多"
            item_enum = rule.get("items", {}).get("enum")
            if item_enum and any(item not in item_enum for item in value):
                return f"{name} 包含不支持的项目"
    return None


class ReadOnlyPolicy:
    """MVP只允许只读工具；模型无法自行升级权限。"""

    def check(self, spec: ToolSpec, _arguments: dict,
              _context: ToolContext) -> tuple[bool, str]:
        if spec.permission != "read_only":
            return False, "MVP仅允许只读诊断工具"
        return True, ""


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: deque[dict] = deque(maxlen=500)
        self._lock = threading.Lock()

    def record(self, call: ToolCall, result: ToolResult,
               elapsed_ms: float) -> None:
        record = {
            "timestamp": time.time(),
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": call.arguments,
            "ok": result.ok,
            "error": result.error,
            "elapsed_ms": round(elapsed_ms, 3),
        }
        with self._lock:
            self.records.append(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, context: ToolContext,
                 policy: ReadOnlyPolicy | None = None,
                 audit_log: AuditLog | None = None) -> None:
        self.registry = registry
        self.context = context
        self.policy = policy or ReadOnlyPolicy()
        self.audit_log = audit_log or AuditLog()
        self._pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ai-readonly-tool")

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        spec = self.registry.get(call.name)
        if spec is None:
            result = ToolResult(ok=False, error=f"unknown_tool:{call.name}")
        else:
            validation_error = _validate_arguments(
                call.arguments, spec.input_schema)
            if validation_error:
                result = ToolResult(
                    ok=False, error=f"invalid_arguments:{validation_error}")
            else:
                allowed, reason = self.policy.check(
                    spec, call.arguments, self.context)
                if not allowed:
                    result = ToolResult(ok=False, error=reason)
                else:
                    try:
                        future = self._pool.submit(
                            spec.handler, call.arguments, self.context)
                        result = future.result(timeout=spec.timeout_s)
                    except TimeoutError:
                        future.cancel()
                        result = ToolResult(ok=False, error="tool_timeout")
                    except Exception as exc:  # 工具异常不能冲出Agent循环
                        result = ToolResult(
                            ok=False,
                            error=f"tool_exception:{type(exc).__name__}:{exc}")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.audit_log.record(call, result, elapsed_ms)
        result.metadata.setdefault("elapsed_ms", round(elapsed_ms, 3))
        return result


class DiagnosticTelemetryStore:
    """线程安全的高速遥测快照库；工具只能读取不可变副本。"""

    CHANNELS = (
        "angle_deg", "speed_rpm", "iq_a", "iqref_a", "ia_a", "ib_a",
        "vd_raw", "vq_raw", "vbus_v",
    )

    def __init__(self, max_samples: int = 5000,
                 max_snapshots: int = 8) -> None:
        self._columns = {
            name: deque(maxlen=max_samples) for name in self.CHANNELS}
        self._rate_hz = 0
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[str, dict] = OrderedDict()
        self._max_snapshots = max_snapshots

    def feed_sample(self, sample: dict) -> None:
        with self._lock:
            for name in self.CHANNELS:
                if name in sample:
                    self._columns[name].append(float(sample[name]))
            self._rate_hz = max(1, int(sample.get("rate_hz", self._rate_hz or 200)))

    def feed_columns(self, columns: dict) -> None:
        count = max(0, int(columns.get("count", 0)))
        if count <= 0:
            return
        with self._lock:
            for name in self.CHANNELS:
                values = columns.get(name, ())
                if values:
                    self._columns[name].extend(
                        float(value) for value in values[:count])
            self._rate_hz = max(1, int(columns.get("rate_hz", self._rate_hz or 200)))

    def capture(self, channels: list[str], duration_s: float) -> dict:
        unsupported = [name for name in channels if name not in self.CHANNELS]
        if unsupported:
            raise ValueError(f"不支持的通道：{', '.join(unsupported)}")
        with self._lock:
            rate = self._rate_hz
            available = min((len(self._columns[name]) for name in channels),
                            default=0)
            requested = max(1, int(round(float(duration_s) * max(rate, 1))))
            count = min(available, requested)
            if rate <= 0 or count <= 0:
                raise ValueError("没有可用的高速遥测数据")
            data = {
                name: list(self._columns[name])[-count:] for name in channels}
            snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
            snapshot = {
                "snapshot_id": snapshot_id,
                "channels": tuple(channels),
                "sample_rate_hz": rate,
                "sample_count": count,
                "duration_s": count / rate,
                "requested_duration_s": float(duration_s),
                "truncated": count < requested,
                "data": data,
                "created_at": time.time(),
            }
            self._snapshots[snapshot_id] = snapshot
            while len(self._snapshots) > self._max_snapshots:
                self._snapshots.popitem(last=False)
            return snapshot

    def get(self, snapshot_id: str) -> dict:
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise KeyError(f"快照不存在或已过期：{snapshot_id}")
            return snapshot


class AgentRuntime:
    """OpenAI兼容tool_calls循环；工具结果只作为JSON证据回传模型。"""

    def __init__(self, client: Any, registry: ToolRegistry,
                 executor: ToolExecutor, max_rounds: int = 8) -> None:
        self.client = client
        self.registry = registry
        self.executor = executor
        self.max_rounds = max(1, int(max_rounds))

    def run(self, messages: list[dict], on_event: Callable[[str], None] | None = None) -> str:
        conversation = list(messages)
        tools = self.registry.model_tools()
        for _round in range(self.max_rounds):
            message = self.client.complete(conversation, tools=tools)
            raw_calls = message.get("tool_calls") or []
            if not raw_calls:
                return str(message.get("content") or "[模型返回了空回答]")
            conversation.append({
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": raw_calls,
            })
            for raw in raw_calls:
                function = raw.get("function") or {}
                name = str(function.get("name") or "")
                call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:10]}")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    arguments = {}
                    result = ToolResult(
                        ok=False, error=f"invalid_arguments_json:{exc}")
                else:
                    if on_event:
                        on_event(f"调用 {name}({json.dumps(arguments, ensure_ascii=False)})")
                    result = self.executor.execute(ToolCall(
                        id=call_id, name=name, arguments=arguments))
                if on_event:
                    state = "完成" if result.ok else f"失败：{result.error}"
                    on_event(f"{name} {state}")
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(
                        result.to_dict(), ensure_ascii=False,
                        separators=(",", ":")),
                })
        return "诊断工具调用达到上限，已停止自动分析。请缩小问题范围后重试。"


def finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback
