"""αβ 轨迹圆度分析：把"不圆"拆成偏心、椭圆、三角三种形变。

对轨迹上每个点取极坐标 (r, φ)，按 r(φ) = R0 + Σ a_k·cos(kφ − ψ_k) 做最小二乘，
各阶幅值 a_k 相对平均半径 R0 的百分比即形变程度：
  k=1 偏心：静止坐标系中不转的矢量（采样零偏、直流电流）
  k=2 椭圆：负序（三相增益/相位不对称），椭圆长轴方向 = ψ_2 / 2
  k=3 三角：负序 2 次谐波（静止坐标系 −2fe）
  k=6 六边形：5/7 次谐波（死区、反电势谐波）
纯几何量，与轨迹来自 iq 重建还是相电流 Clarke 无关。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_COVERAGE_BINS = 24
_ORDERS = (1, 2, 3, 6)


@dataclass(frozen=True)
class TrajectoryShape:
    mean_radius: float
    eccentric_pct: float
    ellipse_pct: float
    triangle_pct: float
    ellipse_axis_deg: float   # 椭圆长轴方向，0~180°，αβ 坐标系
    points: int
    hexagon_pct: float = 0.0
    eccentric_angle_deg: float = 0.0   # 圆心偏移方向，αβ 坐标系


def trajectory_shape(xs, ys, min_points: int = 200,
                     min_coverage: float = 0.75,
                     min_radius: float = 1e-3) -> TrajectoryShape | None:
    """轨迹点不足、角度覆盖不全或半径过小时返回 None（结果不可信）。"""
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if x.size < min_points or x.size != y.size:
        return None
    r = np.hypot(x, y)
    phi = np.arctan2(y, x)
    bins = np.floor((phi + np.pi) / (2 * np.pi) * _COVERAGE_BINS).astype(int)
    covered = np.unique(np.clip(bins, 0, _COVERAGE_BINS - 1)).size
    if covered < min_coverage * _COVERAGE_BINS:
        return None

    cols = [np.ones_like(phi)]
    for k in _ORDERS:
        cols += [np.cos(k * phi), np.sin(k * phi)]
    coef, *_ = np.linalg.lstsq(np.vstack(cols).T, r, rcond=None)
    r0 = float(coef[0])
    if r0 < min_radius:
        return None
    amp = [float(np.hypot(coef[2 * i + 1], coef[2 * i + 2])) for i in range(len(_ORDERS))]
    axis = float(np.degrees(np.arctan2(coef[4], coef[3])) / 2.0) % 180.0
    return TrajectoryShape(
        mean_radius=r0,
        eccentric_pct=100.0 * amp[0] / r0,
        ellipse_pct=100.0 * amp[1] / r0,
        triangle_pct=100.0 * amp[2] / r0,
        ellipse_axis_deg=axis,
        points=int(x.size),
        hexagon_pct=100.0 * amp[3] / r0,
        eccentric_angle_deg=float(np.degrees(np.arctan2(coef[2], coef[1]))) % 360.0,
    )


def clarke(ia, ib) -> tuple[np.ndarray, np.ndarray]:
    """两相采样的幅值不变 Clarke：iα = ia，iβ = (ia + 2·ib)/√3。"""
    a = np.asarray(ia, dtype=float)
    b = np.asarray(ib, dtype=float)
    return a, (a + 2.0 * b) / np.sqrt(3.0)
