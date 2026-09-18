import math

import pytest

from ai.diagnostic_tools import create_read_only_registry
from ai.harness import (
    AgentRuntime, AuditLog, DiagnosticTelemetryStore, ToolCall, ToolContext,
    ToolExecutor,
)
from communications.comm_manager import CommManager


def _harness(tmp_path):
    comm = CommManager()
    store = DiagnosticTelemetryStore(max_samples=4096)
    count = 2048
    rate = 16000
    time_s = [index / rate for index in range(count)]
    iq = [1.8 + 0.2 * math.sin(2 * math.pi * 640 * t) for t in time_s]
    store.feed_columns({
        "count": count,
        "rate_hz": rate,
        "iq_a": iq,
        "iqref_a": [1.8] * count,
        "speed_rpm": [500.0] * count,
    })
    registry = create_read_only_registry()
    context = ToolContext(
        comm=comm,
        telemetry_store=store,
        firmware_config_provider=lambda: {
            "kp_cur_digit": 2323,
            "ki_cur_digit": 2077,
            "sample_rate_hz": 16000,
            "vbus_v": 24.0,
            "filter_enabled": False,
            "filter_alpha_q15": 20491,
            "source": "测试",
        },
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    return registry, ToolExecutor(registry, context, audit_log=audit), audit


def test_read_only_tools_capture_analyze_and_model(tmp_path):
    registry, executor, audit = _harness(tmp_path)
    assert set(registry.names) == {
        "get_runtime_state", "get_firmware_parameters",
        "capture_signal_window", "analyze_time_domain", "analyze_fft",
        "calculate_current_loop",
    }

    capture = executor.execute(ToolCall("1", "capture_signal_window", {
        "channels": ["iq_a", "iqref_a"], "duration_s": 0.1,
    }))
    assert capture.ok
    snapshot_id = capture.data["snapshot_id"]
    assert capture.data["sample_count"] == 1600

    time_result = executor.execute(ToolCall("2", "analyze_time_domain", {
        "snapshot_id": snapshot_id, "channels": ["iq_a"],
    }))
    assert time_result.ok
    assert time_result.data["metrics"]["iq_a"]["mean"] == pytest.approx(1.8)

    fft = executor.execute(ToolCall("3", "analyze_fft", {
        "snapshot_id": snapshot_id, "channel": "iq_a", "remove_dc": True,
    }))
    assert fft.ok
    assert fft.data["dominant_nonzero_hz"] == pytest.approx(640.0, abs=10.1)

    loop = executor.execute(ToolCall("4", "calculate_current_loop", {}))
    assert loop.ok
    assert loop.data["maximum_pole_magnitude"] < 1.0
    assert loop.data["ms"] >= 1.0
    assert len(audit.records) == 4
    assert (tmp_path / "audit.jsonl").exists()


def test_executor_rejects_unknown_or_extra_arguments(tmp_path):
    _registry, executor, _audit = _harness(tmp_path)
    unknown = executor.execute(ToolCall("x", "run_shell", {}))
    assert not unknown.ok
    assert unknown.error == "unknown_tool:run_shell"

    invalid = executor.execute(ToolCall("y", "get_runtime_state", {
        "write_motor": True,
    }))
    assert not invalid.ok
    assert invalid.error.startswith("invalid_arguments:")


def test_agent_runtime_executes_model_tool_call(tmp_path):
    registry, executor, _audit = _harness(tmp_path)

    class FakeClient:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=None):
            self.calls.append((list(messages), list(tools or [])))
            if len(self.calls) == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_state",
                        "type": "function",
                        "function": {
                            "name": "get_runtime_state",
                            "arguments": "{}",
                        },
                    }],
                }
            return {"role": "assistant", "content": "当前未连接。"}

    client = FakeClient()
    events = []
    runtime = AgentRuntime(client, registry, executor)
    reply = runtime.run(
        [{"role": "user", "content": "当前连接了吗？"}],
        on_event=events.append)

    assert reply == "当前未连接。"
    assert len(client.calls) == 2
    assert any(item["function"]["name"] == "get_runtime_state"
               for item in client.calls[0][1])
    assert client.calls[1][0][-1]["role"] == "tool"
    assert events == ["调用 get_runtime_state({})", "get_runtime_state 完成"]
