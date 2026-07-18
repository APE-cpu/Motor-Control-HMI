"""AIClient SSE 流式解析测试（伪造 HTTP 响应，不联网）。"""
import json

import pytest

import ai.ai_client as mod
from ai.ai_client import AIClient
from pages.ai_page import _normalize_url


class _FakeResp:
    """模拟 urlopen 返回的可迭代 SSE 响应。"""

    def __init__(self, lines):
        self._lines = [ln.encode("utf-8") for ln in lines]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def _sse(content: str) -> str:
    return "data: " + json.dumps(
        {"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)


def test_base_url只对主机根路径补v1():
    assert _normalize_url("https://example.com") == "https://example.com/v1"
    assert _normalize_url("https://example.com/v1") == "https://example.com/v1"
    assert (_normalize_url("https://api.z.ai/api/paas/v4") ==
            "https://api.z.ai/api/paas/v4")


def test_流式解析与回调(monkeypatch):
    lines = [
        _sse("电机"), "", ": keep-alive 注释行",
        _sse("运行"),
        "data: " + json.dumps({"choices": [{"delta": {}}]}),  # 无 content 的块
        _sse("正常"),
        "data: [DONE]",
        _sse("DONE后不应再读到"),
    ]
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout: _FakeResp(lines))
    got = []
    reply = AIClient("http://x/v1", "sk-test", "m").chat_stream(
        [{"role": "user", "content": "hi"}], on_delta=got.append)
    assert reply == "电机运行正常"
    assert got == ["电机", "运行", "正常"]


def test_流式请求带stream标志(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(["data: [DONE]"])

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    AIClient("http://x/v1", "k", "m").chat_stream([])
    assert captured["payload"]["stream"] is True


def test_非流式接口不受影响(monkeypatch):
    class _R(_FakeResp):
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "答"}}]}
                              ).encode("utf-8")

    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout: _R([]))
    assert AIClient("http://x/v1", "k", "m").chat([]) == "答"
