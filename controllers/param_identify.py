"""参数辨识纯算法（无 GUI 依赖，供 identify_page 与测试共用）。

原理（只用转速+电流遥测，以 ψf 为转矩锚点）：
  稳态：Kt·iq = B·ω + Tc，两个转速点解出 B、Tc（Kt = 1.5·p·ψf）
  滑行：J·dω/dt = −(B·ω + Tc)，最小二乘拟合 J
"""
import math
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
                min_points: int = 3) -> Tuple[float, int]:
    """滑行降速曲线最小二乘拟合 J。

    coast: [(t 秒, 转速 rpm), ...]；返回 (J, 有效点数)。
    只使用明显降速（dω/dt < −1 rad/s²）且未停死（ω > 5 rad/s）的区段。
    """
    ws: List[Tuple[float, float]] = [(t, rpm * math.pi / 30.0) for t, rpm in coast]
    num = den = 0.0
    used = 0
    for k in range(1, len(ws)):
        dt = ws[k][0] - ws[k - 1][0]
        if dt <= 0:
            continue
        dwdt = (ws[k][1] - ws[k - 1][1]) / dt
        w_mid = 0.5 * (ws[k][1] + ws[k - 1][1])
        if dwdt > -1.0 or w_mid < 5.0:
            continue
        torque = -(b * w_mid + tc)
        num += torque * dwdt
        den += dwdt * dwdt
        used += 1
    if used < min_points:
        raise ValueError(f"滑行段有效数据太少（{used} 点），试试提高转速点 2")
    return num / den, used
