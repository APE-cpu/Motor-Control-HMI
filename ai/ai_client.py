"""OpenAI 兼容 HTTP 客户端（纯标准库）。"""
import json
import urllib.error
import urllib.request
from typing import List, Dict


class AIClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _request(self, payload: dict) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MotorControlHMI/1.9.1 (OpenAI-compatible client)",
            },
            method="POST",
        )

    @staticmethod
    def _http_error(e: urllib.error.HTTPError) -> RuntimeError:
        body = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            msg = body
        return RuntimeError(f"HTTP {e.code}: {msg}")

    def complete(self, messages: List[Dict], tools: list | None = None,
                 timeout: int = 30) -> dict:
        """返回完整assistant message，保留兼容接口的tool_calls字段。"""
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = self._request(payload)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise RuntimeError("模型响应中的message格式无效")
            return message
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None

    def chat(self, messages: List[Dict], timeout: int = 30) -> str:
        message = self.complete(messages, timeout=timeout)
        return str(message.get("content") or "")

    def chat_stream(self, messages: List[Dict], timeout: int = 30,
                    on_delta=None) -> str:
        """SSE 流式对话：每收到一段增量文本回调 on_delta(str)，返回完整回答。

        timeout 作用于单次网络读（两个数据块之间的最长等待），
        而非整段回答的总时长——流式天然不怕长回答。
        """
        req = self._request({"model": self.model, "messages": messages,
                             "stream": True})
        parts: List[str] = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = (json.loads(data)["choices"][0]["delta"]
                                 .get("content") or "")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        parts.append(delta)
                        if on_delta is not None:
                            on_delta(delta)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        return "".join(parts)
