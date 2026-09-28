"""由 F1 高速数据估算能量链路各级功率（实时功率流页与离线回放/实验日志共用）。

  逆变器电气输入 P_inv = 3/2·(vd·id + vq·iq)        （幅值不变 Park，峰值量）
  定子铜损       P_Cu  = 3/2·Rs·(id² + iq²)
  电磁功率       P_em  = Te·ωm，Te = Kt·iq
  转子动能变化   P_kin = J·ωm·dωm/dt
  负载与摩擦     P_load = P_em − P_kin
协议没有母线电流，电源输入按逆变器输入计、电源内阻与制动泄放记 0；逆变器开关损耗忽略。
电压由占空比原始值换算：v = raw / 32767 · Vbus / √3。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from config.config import TORQUE_CONSTANT_NM_PER_A

POWER_KEYS = ("supply", "loss_src", "inv", "brake", "cu", "em", "fric", "kinetic")


@dataclass(frozen=True)
class PowerParams:
    rs_ohm: float = 0.59
    kt_nm_per_a: float = TORQUE_CONSTANT_NM_PER_A
    inertia: float = 1.85e-5
    pole_pairs: int = 4

    @classmethod
    def from_motor(cls, params) -> "PowerParams":
        return cls(rs_ohm=float(params.Rs), inertia=float(params.J),
                   pole_pairs=int(params.pole_pairs))


def voltage_from_raw(raw, vbus):
    """占空比原始值 → 相电压峰值（V）。"""
    return np.asarray(raw, float) / 32767.0 * np.asarray(vbus, float) / math.sqrt(3.0)


def inverter_power(vd, i_d, vq, iq):
    return 1.5 * (np.asarray(vd) * np.asarray(i_d) + np.asarray(vq) * np.asarray(iq))


def park_currents(ia, ib, theta):
    """两相采样 → (id, iq)，按固件的角度约定：F1 电角度指向 q 轴。

    用 2026-09-26 实测记录核对：iq = iα·cosθ + iβ·sinθ 与固件上报的 iq 一致
    （均值 2.55 A 对 2.55 A），此时 id ≈ 0。
    """
    alpha = np.asarray(ia, float)
    beta = (alpha + 2.0 * np.asarray(ib, float)) / math.sqrt(3.0)
    c, s = np.cos(theta), np.sin(theta)
    return alpha * s - beta * c, alpha * c + beta * s


def estimate_power_series(time, columns: dict, params: PowerParams = PowerParams(),
                          block_s: float = 0.02) -> dict:
    """按 block_s 分块平均，返回 {"time": 块中心时间, 各功率键: 数组}。

    columns 需要 iq_a、vq_raw、vbus_v、speed_rpm；有 vd_raw、id_a 更好，
    没有 id_a 但有 ia_a/ib_a/theta 时用 Park 求 id。
    """
    t = np.asarray(time, float)
    if t.size < 2:
        raise ValueError("数据太短，无法估算功率")
    n = t.size
    iq = np.asarray(columns["iq_a"], float)
    vbus = np.asarray(columns.get("vbus_v", np.full(n, 24.0)), float)
    vq = voltage_from_raw(columns["vq_raw"], vbus)
    vd = voltage_from_raw(columns["vd_raw"], vbus) if "vd_raw" in columns else np.zeros(n)
    if "id_a" in columns:
        i_d = np.asarray(columns["id_a"], float)
    elif {"ia_a", "ib_a", "theta"} <= set(columns):
        i_d, _ = park_currents(columns["ia_a"], columns["ib_a"], columns["theta"])
    else:
        i_d = np.zeros(n)
    speed = np.asarray(columns.get("speed_rpm", np.zeros(n)), float)

    rate = 1.0 / float(np.median(np.diff(t)))
    size = max(1, int(round(block_s * rate)))
    blocks = n // size
    if blocks < 2:
        size, blocks = 1, n
    cut = blocks * size

    def mean(values):
        return np.asarray(values[:cut], float).reshape(blocks, size).mean(axis=1)

    centers = mean(t)
    inv = mean(inverter_power(vd, i_d, vq, iq))
    cu = mean(1.5 * params.rs_ohm * (i_d ** 2 + iq ** 2))
    omega = mean(speed) * math.pi / 30.0
    em = params.kt_nm_per_a * mean(iq) * omega
    kinetic = params.inertia * omega * np.gradient(omega, centers)
    zeros = np.zeros(blocks)
    return {"time": centers, "supply": inv.copy(), "loss_src": zeros, "inv": inv,
            "brake": zeros.copy(), "cu": cu, "em": em, "fric": em - kinetic,
            "kinetic": kinetic}


def powers_at(series: dict, t: float) -> dict:
    """某一时刻的功率快照（最近的块），供桑基图显示。"""
    index = int(np.clip(np.searchsorted(series["time"], t), 0, series["time"].size - 1))
    return {key: float(series[key][index]) for key in POWER_KEYS}


def power_summary(series: dict) -> dict:
    """整段平均功率、能量与效率（实验日志用）。"""
    duration = float(series["time"][-1] - series["time"][0]) if series["time"].size > 1 else 0.0
    means = {key: float(np.mean(series[key])) for key in POWER_KEYS}
    energy = {key: means[key] * duration for key in POWER_KEYS}
    efficiency = (means["em"] / means["inv"]) if means["inv"] > 1e-6 and means["em"] > 0 else None
    return {"duration_s": duration, "mean_w": means, "energy_j": energy,
            "motor_efficiency": efficiency,
            "peak_inv_w": float(np.max(np.abs(series["inv"]))) if series["inv"].size else 0.0}
