"""Laya本地原生/ONNX客户端测试（单元测试不加载1.7 GB权重）。"""
import json
from pathlib import Path

import pytest

import ai.laya_client as mod
from ai.laya_client import LayaClient


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


def test_laya拒绝任何非本机地址(tmp_path):
    with pytest.raises(ValueError, match="本机"):
        LayaClient("https://example.com", tmp_path)


def test_laya只访问本机system_one且不发送密钥(monkeypatch, tmp_path):
    captured = {}
    client = LayaClient("http://127.0.0.1:8765", tmp_path)
    monkeypatch.setattr(client, "ensure_ready", lambda timeout=None: None)

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"model": "laya", "answers": {}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    result = client.decide(
        {"speed_rpm": 500},
        {"safe": {"type": "noul", "instructions": "Is it safe?"}},
    )

    assert captured["url"] == "http://127.0.0.1:8765/system-one"
    assert "Authorization" not in captured["headers"]
    assert set(captured["payload"]) == {"state", "questions"}
    assert result == {"model": "laya", "answers": {}}


def test_laya按需启动给定目录里的本地服务(monkeypatch, tmp_path):
    (tmp_path / "server.mjs").write_text("// test", encoding="utf-8")
    model_dir = tmp_path / "model-cache" / "receptron--laya-onnx" / "main"
    (model_dir / "tokenizer").mkdir(parents=True)
    for relative in (
        "laya.onnx", "laya.onnx.data", "laya_config.json",
        "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json",
    ):
        (model_dir / relative).write_bytes(b"test")

    states = iter([None, {"status": "loading"}, {"status": "ready"}])
    client = LayaClient("http://127.0.0.1:8765", tmp_path)
    monkeypatch.setattr(client, "health", lambda timeout=2: next(states))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "C:/node/node.exe")
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    captured = {}

    class _Process:
        def poll(self):
            return None

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    client.ensure_ready(timeout=2)

    assert captured["args"][-1] == "server.mjs"
    assert Path(captured["kwargs"]["cwd"]) == tmp_path
    assert captured["kwargs"]["env"]["LAYA_CACHE"] == str(tmp_path / "model-cache")


def test_laya_cpp按需启动vulkan并使用原生端点(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "laya-cli.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test")
    model_dir = tmp_path / "model"
    for relative in (
        "model.safetensors", "rl_agent_config.json", "encoder/config.json",
        "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json",
    ):
        target = model_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test")

    states = iter([None, {"status": "loading"}, {"status": "ok"}])
    client = LayaClient(
        "http://127.0.0.1:8766", native_executable=executable,
        native_model_dir=model_dir, backend="vulkan", precision="bf16")
    monkeypatch.setattr(client, "health", lambda timeout=2: next(states))
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    captured = {}

    class _Process:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    client.ensure_ready(timeout=2)

    assert captured["args"] == [
        str(executable), "--vulkan", "--bf16", "--model", str(model_dir),
        "--server", "--host", "127.0.0.1", "--port", "8766",
        "--no-batching",
    ]
    assert captured["kwargs"]["cwd"] == str(executable.parent)
    assert captured["kwargs"]["env"]["GGML_VK_VISIBLE_DEVICES"] == "0"
    assert client.runtime_label == "laya.cpp / VULKAN / BF16"


def test_laya_cpp请求v1_systemone(monkeypatch, tmp_path):
    executable = tmp_path / "laya-cli.exe"
    executable.write_bytes(b"test")
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    client = LayaClient(
        "http://127.0.0.1:8766", native_executable=executable,
        native_model_dir=model_dir)
    monkeypatch.setattr(client, "ensure_ready", lambda timeout=None: None)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _FakeResp({"model": "laya-rl-agent", "answers": {}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    result = client.decide(
        {"speed_rpm": 500},
        {"safe": {"type": "noul", "instructions": "Is it safe?"}},
    )

    assert captured["url"] == "http://127.0.0.1:8766/v1/systemone"
    assert result["model"] == "laya-rl-agent"


def test_laya诊断复用结构化中文结果(monkeypatch, tmp_path):
    response = {
        "model": "laya",
        "answers": {
            "operating_state": {
                "type": "choice", "choice": "watch", "confidence": 0.61,
                "probabilities": {"normal": 0.30, "watch": 0.61,
                                  "warning": 0.07, "fault": 0.01,
                                  "insufficient_data": 0.01},
            },
            "severity": {"type": "score", "score": 1.2},
            "needs_human_review": {"type": "noul", "noul": 0.33},
            "next_action": {
                "type": "choice", "choice": "collect_more_data",
                "confidence": 0.70,
            },
        },
    }
    client = LayaClient("http://127.0.0.1:8765", tmp_path)
    monkeypatch.setattr(client, "ensure_ready", lambda timeout=None: None)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout: _FakeResp(response))

    text = client.diagnose("转速是否正常？", "speed=480, target=500")

    assert "Laya 本地结构化诊断" in text
    assert "留意" in text
    assert "61.0%" in text
    assert "采集更多稳态数据" in text
    assert "本次推理仅在本机完成" in text


def test_laya把中文遥测转换为英文数值特征(monkeypatch, tmp_path):
    client = LayaClient("http://127.0.0.1:8765", tmp_path)
    captured = {}

    def fake_decide(state, questions, timeout=30):
        captured["state"] = state
        captured["questions"] = questions
        return {
            "answers": {
                "operating_state": {"choice": "normal", "confidence": 0.8},
                "severity": {"score": 0.2},
                "needs_human_review": {"noul": 0.1},
                "next_action": {
                    "choice": "continue_monitoring", "confidence": 0.8},
            }
        }

    monkeypatch.setattr(client, "decide", fake_decide)
    client.diagnose(
        "当前转速是否正常？",
        "实际转速=498.0 rpm  给定转速=500.0 rpm\n"
        "实际电流=1.20 A   给定电流=1.18 A\n温度=35.0°C",
    )

    state = captured["state"]
    assert state["requested_focus"] == "speed_tracking"
    assert state["telemetry"]["actual_speed_rpm"] == 498.0
    assert state["telemetry"]["target_speed_rpm"] == 500.0
    assert state["derived"]["speed_error_percent"] == pytest.approx(0.4)
    assert state["telemetry"]["temperature_c"] == 35.0
    assert all(ord(ch) < 128 for ch in json.dumps(
        state, ensure_ascii=False, separators=(",", ":")))


def test_laya低置信度不会显示为确定故障动作(tmp_path):
    data = {
        "answers": {
            "operating_state": {"choice": "fault", "confidence": 0.054},
            "severity": {"score": 3.0},
            "needs_human_review": {"noul": 0.57},
            "next_action": {"choice": "stop_and_inspect", "confidence": 0.1},
        }
    }
    text = LayaClient.format_diagnosis(data)
    assert "模型不确定（置信度：5.4%）" in text
    assert "低置信度，不执行自动处置" in text
    assert "停机并进行人工检查" not in text
