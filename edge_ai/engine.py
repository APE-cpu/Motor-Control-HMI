"""边缘AI推理引擎：基于 ONNX Runtime 的本地异常检测。

输入特征向量：[speed_actual, speed_target, current_actual, current_target,
               torque_actual, torque_target, angle_actual, temperature]
输出：异常分数 0.0~1.0（越高越异常）

若未提供 ONNX 模型文件，自动回退到基于规则的统计检测器。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    import onnxruntime as ort
    _ORT_OK = True
except ImportError:
    _ORT_OK = False


@dataclass
class InferenceResult:
    score: float          # 0.0 ~ 1.0
    label: str            # "正常" / "警告" / "异常"
    detail: str           # 简短说明


def _rule_based_score(feat: np.ndarray) -> float:
    """无模型时的统计规则检测（归一化偏差之和）。"""
    speed_actual, speed_target, current_actual, _, torque_actual, _, _, temperature = feat
    scores = []
    if speed_target != 0:
        scores.append(abs(speed_actual - speed_target) / (abs(speed_target) + 1e-6))
    if temperature > 0:
        scores.append(max(0.0, (temperature - 60.0) / 40.0))   # 60°C 开始计分
    if current_actual > 0:
        scores.append(max(0.0, (current_actual - 10.0) / 10.0))  # 10A 开始计分
    return float(np.clip(np.mean(scores) if scores else 0.0, 0.0, 1.0))


class EdgeAIEngine:
    def __init__(self, model_path: Optional[str] = None) -> None:
        self._session = None
        self._load_error: str = ""
        self._model_path = model_path
        if model_path and os.path.isfile(model_path):
            if not _ORT_OK:
                self._load_error = "未安装 onnxruntime"
            else:
                try:
                    self._session = ort.InferenceSession(model_path)
                    self._input_name = self._session.get_inputs()[0].name
                except Exception as e:
                    self._load_error = str(e)

    @property
    def using_onnx(self) -> bool:
        return self._session is not None

    def infer(self, features: list[float]) -> InferenceResult:
        feat = np.array(features, dtype=np.float32)
        if self._session is not None:
            out = self._session.run(None, {self._input_name: feat.reshape(1, -1)})
            score = float(np.clip(out[0].flatten()[0], 0.0, 1.0))
        else:
            score = _rule_based_score(feat)

        if score < 0.3:
            label, detail = "正常", "各项指标在正常范围内"
        elif score < 0.6:
            label, detail = "警告", "部分指标偏离给定值，请关注"
        else:
            label, detail = "异常", "检测到明显异常，建议停机检查"
        return InferenceResult(score=score, label=label, detail=detail)
