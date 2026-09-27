"""JEV 原生 Decisions API 客户端测试（不访问公网）。"""
import json

import ai.jev_client as mod
from ai.jev_client import JevClient


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


def test_jev使用原生decisions端点和bearer鉴权(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["authorization"] = req.headers["Authorization"]
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"code": 0, "message": "ok", "data": {"answers": {}}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    client = JevClient(
        "https://api.typesafe.ai/v1", "apikey-test", "jev-latest")
    result = client.decide(
        {"speed_rpm": 500},
        {"status": {"type": "choice", "instructions": "判断状态",
                    "criteria": {"normal": "正常", "fault": "故障"}}},
    )

    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["authorization"] == "Bearer apikey-test"
    assert captured["payload"]["model"] == "jev-latest"
    assert captured["payload"]["state"] == {"speed_rpm": 500}
    assert result == {"answers": {}}


def test_jev诊断结果格式化为可读中文(monkeypatch):
    response = {
        "code": 0,
        "message": "ok",
        "data": {
            "answers": {
                "operating_state": {
                    "choice": "warning", "confidence": 0.82,
                    "probabilities": {"normal": 0.08, "warning": 0.82,
                                      "fault": 0.10},
                },
                "severity": {"score": 2.1, "confidence": 0.73},
                "needs_human_review": {"noul": 0.64},
                "next_action": {"choice": "inspect_signal", "confidence": 0.77},
            }
        },
    }
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout: _FakeResp(response))

    text = JevClient(
        "https://api.typesafe.ai/v1", "k", "jev-latest"
    ).diagnose("电流是否异常？", "转速=500 rpm，Iq=1.8 A")

    assert "JEV 结构化诊断" in text
    assert "需要关注" in text
    assert "82.0%" in text
    assert "严重度" in text
    assert "人工复核概率：64.0%" in text
    assert "检查波形与频谱" in text
    assert "JEV 不生成长文本解释" in text


def test_jev服务端错误不会泄露api_key(monkeypatch):
    monkeypatch.setattr(
        mod.urllib.request, "urlopen",
        lambda req, timeout: _FakeResp({"code": 401, "message": "invalid key"}),
    )
    secret = "apikey-super-secret"
    client = JevClient("https://api.typesafe.ai/v1", secret, "jev-latest")

    try:
        client.decide({}, {})
    except RuntimeError as exc:
        assert "invalid key" in str(exc)
        assert secret not in str(exc)
    else:
        raise AssertionError("服务端错误应抛出 RuntimeError")
