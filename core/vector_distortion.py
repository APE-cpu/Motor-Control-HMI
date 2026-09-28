"""电流矢量圆畸变模型：由已知的非理想因素合成两相采样，再看 αβ 轨迹、dq 波形与复数频谱。

所有量以基波幅值为 1 归一化（百分比参数 = 相对基波）。采样链路与固件一致：
只采 Ia、Ib，Ic 由 −(Ia+Ib) 推出，再做幅值不变 Clarke 与理想角度 Park。

各因素在复数频谱（i_α + j·i_β 对电角度的谐波次数 k）里的落点：
  采样零偏        → k = 0   （静止矢量，圆心偏移；dq 中为 1 倍电频率纹波）
  增益/相位不对称 → k = −1  （负序，椭圆；dq 中为 2 倍电频率纹波）
  5 次谐波        → k = −5  （负序；dq 中为 6 倍电频率纹波）
  7 次谐波        → k = +7  （正序；dq 中为 6 倍电频率纹波）
  死区            → k = −5, +7, −11, +13 …（6k±1 次，六边形）
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.vector_shape import TrajectoryShape, clarke, trajectory_shape

SAMPLES = 720                       # 一个电周期的采样点数
SPECTRUM_ORDERS = tuple(range(-13, 14))
_DEAD_TIME_ORDERS = (5, 7, 11, 13, 17, 19)


@dataclass(frozen=True)
class DistortionParams:
    offset_a_pct: float = 0.0       # Ia 采样零偏
    offset_b_pct: float = 0.0       # Ib 采样零偏
    gain_b_pct: float = 0.0         # Ib 相对 Ia 的增益误差
    phase_b_deg: float = 0.0        # Ib 采样相位误差
    h5_pct: float = 0.0             # 5 次谐波（负序）
    h7_pct: float = 0.0             # 7 次谐波（正序）
    dead_time_pct: float = 0.0      # 死区畸变，按其 5 次分量幅值计


PRESETS: dict[str, DistortionParams] = {
    "理想正圆": DistortionParams(),
    "采样零偏": DistortionParams(offset_a_pct=8.0, offset_b_pct=-3.0),
    "增益不对称": DistortionParams(gain_b_pct=10.0),
    "相位不对称": DistortionParams(phase_b_deg=6.0),
    "5/7 次谐波": DistortionParams(h5_pct=6.0, h7_pct=3.0),
    "死区效应": DistortionParams(dead_time_pct=6.0),
    "综合（实机常见）": DistortionParams(offset_a_pct=3.0, gain_b_pct=4.0,
                                   phase_b_deg=1.5, dead_time_pct=3.0),
}


@dataclass(frozen=True)
class DistortionResult:
    theta: np.ndarray               # 电角度 [rad]
    alpha: np.ndarray
    beta: np.ndarray
    i_d: np.ndarray
    i_q: np.ndarray
    spectrum_pct: dict[int, float]  # 谐波次数 k → 幅值（% 基波）
    shape: TrajectoryShape | None


def _dead_time_wave(theta_x: np.ndarray) -> np.ndarray:
    """死区电压误差 −ΔV·sign(i) 经绕组电感积分后的电流畸变（三角波去掉基波与 3 的倍数次）。

    归一化到 5 次分量幅值 = 1；n 次分量 ∝ 1/n²，符号沿三角波级数。
    """
    wave = np.zeros_like(theta_x)
    for n in _DEAD_TIME_ORDERS:
        sign = -1.0 if ((n - 1) // 2) % 2 else 1.0
        wave += sign * (25.0 / n ** 2) * np.sin(n * theta_x)
    return wave


def phase_currents(params: DistortionParams, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """合成采样得到的 Ia、Ib（归一化，基波幅值 1）。"""
    p = params
    angles = (theta, theta - 2.0 * np.pi / 3.0)   # A、B 两相（C 相由采样链路推出）
    currents = []
    for theta_x in angles:
        i = np.cos(theta_x)
        # 各相自身角度的 5、7 次：5×(−120°) ≡ +120° 自然成负序，7×(−120°) ≡ −120° 成正序
        i = i + p.h5_pct / 100.0 * np.cos(5.0 * theta_x)
        i = i + p.h7_pct / 100.0 * np.cos(7.0 * theta_x)
        i = i + p.dead_time_pct / 100.0 * _dead_time_wave(theta_x)
        currents.append(i)
    ia, ib = currents
    # 采样链路：Ib 通道的增益与相位误差、两路零偏
    ib_true_phase = theta - 2.0 * np.pi / 3.0 + np.radians(p.phase_b_deg)
    ib = ib + (np.cos(ib_true_phase) - np.cos(theta - 2.0 * np.pi / 3.0))
    ib = ib * (1.0 + p.gain_b_pct / 100.0)
    ia = ia + p.offset_a_pct / 100.0
    ib = ib + p.offset_b_pct / 100.0
    return ia, ib


def complex_spectrum(alpha, beta, orders=SPECTRUM_ORDERS) -> dict[int, float]:
    """均匀一周采样的 i_α + j·i_β 在各谐波次数上的幅值（% 基波 = 100）。"""
    z = np.asarray(alpha) + 1j * np.asarray(beta)
    n = z.size
    theta = 2.0 * np.pi * np.arange(n) / n
    return {k: float(100.0 * abs(np.mean(z * np.exp(-1j * k * theta)))) for k in orders}


def simulate(params: DistortionParams, samples: int = SAMPLES) -> DistortionResult:
    theta = 2.0 * np.pi * np.arange(samples) / samples
    ia, ib = phase_currents(params, theta)
    alpha, beta = clarke(ia, ib)
    # 按 id=0 控制理解：电流矢量落在 q 轴，Park 角取 θ − 90°，理想时 id=0、iq=1
    c, s = np.cos(theta), np.sin(theta)
    i_d = alpha * s - beta * c
    i_q = alpha * c + beta * s
    return DistortionResult(
        theta=theta, alpha=alpha, beta=beta, i_d=i_d, i_q=i_q,
        spectrum_pct=complex_spectrum(alpha, beta),
        shape=trajectory_shape(alpha, beta, min_points=50))


# ─── 实测轨迹反推 ──────────────────────────────────────────────
# 实测只有 αβ 轨迹几何（没有逐点电角度），因此按几何分解反推"等效"参数：
#   偏心矢量  → Ia、Ib 零偏（精确，零偏只产生偏心）
#   椭圆      → Ib 增益误差 + Ib 相位误差（两者的椭圆长轴相差约 44°，可解 2×2 线性方程）
#   六边形    → 等效死区（死区的 −5/+7 分量部分相互抵消，径向只剩约一半）
# 两相采样下椭圆长轴并不沿某相轴：Ib 增益误差约 104°，相位误差约 60°。
_NOTICEABLE_PCT = 1.5


def _ellipse_vector(shape: TrajectoryShape) -> complex:
    """椭圆（负序）用 幅值·e^{j2·长轴} 表示，才能线性叠加。"""
    return shape.ellipse_pct * np.exp(2j * np.radians(shape.ellipse_axis_deg))


def _unit_responses() -> tuple[complex, complex, float]:
    gain = simulate(DistortionParams(gain_b_pct=1.0)).shape
    phase = simulate(DistortionParams(phase_b_deg=1.0)).shape
    dead = simulate(DistortionParams(dead_time_pct=1.0)).shape
    return _ellipse_vector(gain), _ellipse_vector(phase), dead.hexagon_pct


_UNITS: tuple[complex, complex, float] | None = None


def estimate_params(shape: TrajectoryShape | None) -> DistortionParams | None:
    """由实测圆度分解反推等效的畸变参数（小畸变线性近似）。"""
    global _UNITS
    if shape is None:
        return None
    if _UNITS is None:
        _UNITS = _unit_responses()
    unit_gain, unit_phase, unit_dead = _UNITS
    # 零偏：偏心矢量 d = (dα, dβ)；iα = ia，iβ = (ia + 2 ib)/√3
    angle = np.radians(shape.eccentric_angle_deg)
    d_alpha = shape.eccentric_pct * np.cos(angle)
    d_beta = shape.eccentric_pct * np.sin(angle)
    offset_a = d_alpha
    offset_b = (np.sqrt(3.0) * d_beta - d_alpha) / 2.0
    # 负序：e = g·u_g + δ·u_δ（g、δ 为实数）
    e = _ellipse_vector(shape)
    matrix = np.array([[unit_gain.real, unit_phase.real],
                       [unit_gain.imag, unit_phase.imag]])
    gain, phase = np.linalg.solve(matrix, [e.real, e.imag])
    dead = shape.hexagon_pct / unit_dead if unit_dead > 1e-9 else 0.0
    return DistortionParams(
        offset_a_pct=float(offset_a), offset_b_pct=float(offset_b),
        gain_b_pct=float(gain), phase_b_deg=float(phase),
        dead_time_pct=float(dead))


def diagnose(shape: TrajectoryShape | None) -> list[str]:
    """把圆度分解翻译成可能原因（按显著程度排序）；给出方向与等效量，不作定论。"""
    if shape is None:
        return ["轨迹不足约 3/4 圈或半径过小，暂时无法分解。"]
    est = estimate_params(shape)
    items = [
        (shape.eccentric_pct,
         f"偏心 {shape.eccentric_pct:.1f}%：圆心偏移，最常见是采样零偏"
         f"（等效 Ia {est.offset_a_pct:+.1f}%、Ib {est.offset_b_pct:+.1f}%），"
         "建议停机重新做零偏校准。"),
        (shape.ellipse_pct,
         f"椭圆 {shape.ellipse_pct:.1f}%（长轴 {shape.ellipse_axis_deg:.0f}°）：负序分量，"
         f"等效 Ib 增益误差 {est.gain_b_pct:+.1f}%、相位误差 {est.phase_b_deg:+.1f}°；"
         "来源可能是两路采样增益/采样时刻不一致，或三相绕组、驱动不对称。"),
        (shape.hexagon_pct,
         f"六边形 {shape.hexagon_pct:.1f}%：5/7 次谐波（等效死区 {est.dead_time_pct:.1f}%），"
         "典型来源是死区与管压降、反电势谐波；电流越小越明显的多半是死区。"),
        (shape.triangle_pct,
         f"三角 {shape.triangle_pct:.1f}%：负序 2 次（静止坐标 −2fe），较少见，"
         "可查采样同步与 PWM 中心对齐。"),
    ]
    lines = [text for pct, text in sorted(items, key=lambda item: -item[0])
             if pct >= _NOTICEABLE_PCT]
    return lines or ["各项形变都小于 1.5%，轨迹接近正圆。"]
