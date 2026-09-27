"""JEV 原生 Decisions API 客户端。

JEV 是结构化决策模型，不提供 OpenAI ``/chat/completions`` 接口。
本模块把电机状态快照转换为 choice / score / noul 问题，并将返回值
整理成适合上位机展示的简短诊断卡片。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


_STATE_LABELS = {
    "normal": "正常",
    "watch": "留意",
    "warning": "需要关注",
    "fault": "疑似故障",
    "insufficient_data": "数据不足",
}

_ACTION_LABELS = {
    "continue_monitoring": "继续监测",
    "inspect_signal": "检查波形与频谱",
    "check_control_parameters": "核对控制参数与限幅",
    "stop_and_inspect": "停机并进行人工检查",
    "collect_more_data": "采集更多稳态数据",
}


class JevClient:
    """调用 JEV 原生 Decisions API，不模拟聊天模型。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _request(self, payload: dict) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MotorControlHMI/2.0.0 (JEV decisions client)",
            },
            method="POST",
        )

    def _safe_error(self, message: object) -> RuntimeError:
        text = str(message or "JEV 请求失败")
        if self.api_key:
            text = text.replace(self.api_key, "<redacted>")
        return RuntimeError(text)

    def decide(self, state: object, questions: Mapping[str, object],
               timeout: int = 30) -> dict:
        payload = {
            "model": self.model,
            "state": state,
            "questions": dict(questions),
        }
        try:
            with urllib.request.urlopen(
                    self._request(payload), timeout=timeout) as resp:
                envelope = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body)
                message = detail.get("message") or detail.get("error") or body
            except Exception:
                message = body
            raise self._safe_error(f"HTTP {exc.code}: {message}") from None
        except urllib.error.URLError as exc:
            raise self._safe_error(f"JEV 网络请求失败：{exc.reason}") from None

        if not isinstance(envelope, dict):
            raise self._safe_error("JEV 响应格式无效")
        if envelope.get("code") not in (None, 0):
            raise self._safe_error(envelope.get("message") or "JEV 服务端返回错误")
        data = envelope.get("data", envelope)
        if not isinstance(data, dict):
            raise self._safe_error("JEV 响应缺少 data 对象")
        return data

    @staticmethod
    def _answer_value(answer: Any, *keys: str) -> Any:
        if isinstance(answer, Mapping):
            for key in keys:
                if key in answer:
                    return answer[key]
            # 兼容 {"value": ...} 及服务端将结果包在 answer/result 中的形式。
            for key in ("value", "answer", "result"):
                if key in answer:
                    return answer[key]
        return answer

    @staticmethod
    def _percent(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "未知"
        if number <= 1.0:
            number *= 100.0
        return f"{number:.1f}%"

    @classmethod
    def format_diagnosis(cls, data: Mapping[str, Any]) -> str:
        answers = data.get("answers", data)
        if not isinstance(answers, Mapping):
            raise RuntimeError("JEV 响应中没有可解析的 answers")

        state_answer = answers.get("operating_state", {})
        state = cls._answer_value(state_answer, "choice")
        state_label = _STATE_LABELS.get(str(state), str(state or "未知"))
        confidence = (state_answer.get("confidence")
                      if isinstance(state_answer, Mapping) else None)

        severity_answer = answers.get("severity", {})
        severity = cls._answer_value(severity_answer, "score")
        try:
            severity_text = f"{float(severity):.2f} / 4"
        except (TypeError, ValueError):
            severity_text = "未知"

        review_answer = answers.get("needs_human_review", {})
        review = cls._answer_value(
            review_answer, "noul", "probability", "confidence")

        action_answer = answers.get("next_action", {})
        action = cls._answer_value(action_answer, "choice")
        action_label = _ACTION_LABELS.get(str(action), str(action or "数据不足"))

        lines = [
            "JEV 结构化诊断",
            f"- 运行状态：{state_label}（置信度：{cls._percent(confidence)}）",
            f"- 严重度：{severity_text}",
            f"- 人工复核概率：{cls._percent(review)}",
            f"- 建议动作：{action_label}",
            "- 说明：JEV 不生成长文本解释；以上是快速结构化判定，"
            "关键结论仍需结合原始波形、FFT 与控制参数复核。",
        ]
        return "\n".join(lines)

    def diagnose(self, question: str, snapshot: str,
                 timeout: int = 30) -> str:
        state = {
            "task": "Evaluate the supplied PMSM telemetry for diagnostic triage.",
            "user_question": question,
            "telemetry_snapshot": snapshot,
            "constraints": [
                "Use only supplied evidence.",
                "Do not claim a fault when evidence is insufficient.",
                "This result is advisory and must not operate the motor.",
            ],
        }
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
                "type": "score",
                "instructions": "Score diagnostic severity.",
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
        return self.format_diagnosis(self.decide(state, questions, timeout=timeout))
