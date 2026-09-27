"""上位机侧电磁转矩估算：Te = Kt · Iq。

下位机不提供转矩测量值，转矩统一在上位机由 q 轴电流乘转矩常数得到。
F1 高速流按固定时长分块平均后再乘 Kt，避免把单点 Iq 纹波当作转矩：
16 kHz 下 2 ms 一块（32 点）→ 500 Hz 转矩，与 F1 速度曲线节拍一致。
"""
from __future__ import annotations

from config.config import TORQUE_CONSTANT_NM_PER_A

TORQUE_OUTPUT_RATE_HZ = 500.0


def torque_from_iq(iq_a: float, kt: float = TORQUE_CONSTANT_NM_PER_A) -> float:
    return float(iq_a) * kt


class F1TorqueEstimator:
    """把 F1 的 Iq 样本按块平均成转矩；跨批次保留不足一块的余数。"""

    def __init__(self, kt: float = TORQUE_CONSTANT_NM_PER_A,
                 output_rate_hz: float = TORQUE_OUTPUT_RATE_HZ) -> None:
        self.kt = float(kt)
        self.output_rate_hz = float(output_rate_hz)
        self._rate_hz = 0
        self._block = 1
        self._acc = 0.0
        self._count = 0

    @property
    def block_samples(self) -> int:
        return self._block

    @property
    def interval_s(self) -> float:
        """输出转矩样本间隔（块长 / 输入采样率）。"""
        return self._block / self._rate_hz if self._rate_hz else 0.0

    def reset(self) -> None:
        self._acc = 0.0
        self._count = 0

    def push(self, iq_a, rate_hz: int) -> list[float]:
        """送入一批 Iq（A），返回本批凑满的块对应的转矩（N·m）。"""
        rate_hz = int(rate_hz)
        if rate_hz <= 0:
            return []
        if rate_hz != self._rate_hz:
            # 采样率改变：旧余数与新样本时间间隔不同，不能混进同一块
            self._rate_hz = rate_hz
            self._block = max(1, round(rate_hz / self.output_rate_hz))
            self.reset()
        out = []
        for value in iq_a:
            self._acc += float(value)
            self._count += 1
            if self._count == self._block:
                out.append(self._acc / self._block * self.kt)
                self.reset()
        return out
