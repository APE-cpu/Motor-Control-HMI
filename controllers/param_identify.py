"""参数辨识纯算法（无 GUI 依赖，供 identify_page 与测试共用）。

原理（只用转速+电流遥测，以 ψf 为转矩锚点）：
  稳态：Kt·iq = B·ω + Tc，两个转速点解出 B、Tc（Kt = 1.5·p·ψf）
  滑行：J·dω/dt = −(B·ω + Tc)，解析衰减区间拟合 J
"""
import math
import statistics
from typing import List, Sequence, Tuple


def torque_constant(psi_f: float, pole_pairs: int) -> float:
    """Kt = 1.5·p·ψf。"""
    return 1.5 * pole_pairs * psi_f


def solve_friction(w1: float, i1: float, w2: float, i2: float,
                   kt: float) -> Tuple[float, float]:
    """两点稳态解 (B, Tc)：Kt·iq = B·ω + Tc。ω 单位 rad/s。"""
    if abs(w2 - w1) < 1.0:
        raise ValueError("两个稳态转速点太接近")
    b_hat = kt * (i2 - i1) / (w2 - w1)
    tc_hat = kt * i1 - b_hat * w1
    return b_hat, tc_hat


def fit_inertia(coast: Sequence[Tuple[float, float]], b: float, tc: float,
                min_points: int = 1) -> Tuple[float, int]:
    """用滑行降速的解析衰减区间拟合 J。

    coast: [(t 秒, 转速 rpm), ...]；返回 (J, 有效点数)。
    对 ``J·dω/dt = -(B·ω + Tc)`` 在每个有效区间积分求解，避免
    小惯量电机在低采样率下用有限差分造成系统性误差。最后取区间估计
    的中位数抑制单点噪声。只使用明显降速且区间中点大于 5 rad/s 的数据。
    """
    if not math.isfinite(b) or not math.isfinite(tc) or b < 0.0 or tc < 0.0:
        raise ValueError("摩擦参数 B、Tc 必须为非负有限数")
    if min_points < 1:
        raise ValueError("min_points 必须至少为 1")
    ws: List[Tuple[float, float]] = [(t, rpm * math.pi / 30.0) for t, rpm in coast]
    estimates: List[float] = []
    for k in range(1, len(ws)):
        dt = ws[k][0] - ws[k - 1][0]
        if dt <= 0:
            continue
        w0, w1 = ws[k - 1][1], ws[k][1]
        dwdt = (w1 - w0) / dt
        w_mid = 0.5 * (w0 + w1)
        if dwdt > -1.0 or w_mid < 5.0:
            continue
        if b > 1e-12:
            offset = tc / b
            numerator = w1 + offset
            denominator = w0 + offset
            if numerator <= 0.0 or denominator <= 0.0:
                continue
            ratio = numerator / denominator
            if not 0.0 < ratio < 1.0:
                continue
            estimate = -b * dt / math.log(ratio)
        else:
            if tc <= 1e-12:
                continue
            estimate = -tc / dwdt
        if math.isfinite(estimate) and estimate > 0.0:
            estimates.append(estimate)
    used = len(estimates)
    if used < min_points:
        raise ValueError(f"滑行段有效数据太少（{used} 点），试试提高转速点 2")
    return float(statistics.median(estimates)), used
