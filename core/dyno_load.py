"""对拖负载模型：负载电机驱动器“上电未启动”时三相桥臂停在零电压矢量（50% 占空比），
等效三相绕组短接，负载电机成为一台短路发电机，产生随转速变化的制动转矩。

稳态（负载电机自身 dq 系，表贴式 Ld = Lq = L）：
    0 = R·id − ωe·L·iq
    0 = R·iq + ωe·L·id + ωe·ψ
解得
    iq = −ωe·ψ·R / (R² + ωe²L²)，  id = −ωe²·L·ψ / (R² + ωe²L²)
    |I| = ωe·ψ / √(R² + ωe²L²)
    T  = 1.5·p·ψ·iq = −1.5·p·ψ²·ωe·R / (R² + ωe²L²)
    P  = 1.5·R·|I|² = T·ωm（全部变成负载电机绕组与开关管的铜损）
低速时 T ≈ 1.5·p²·ψ²·ωm / R（等效粘滞），在 ωe = R/L 处最大：T_max = 1.5·p·ψ² / (2L)；
高速时电流趋近 ψ/L（与转速无关，不受任何控制器限制）。

理想短路制动只产生恒定转矩；转矩脉动来自负载电机磁链的每转不均（偏心、磁钢不一致）、
反电动势谐波和齿槽转矩，它们与驱动电机同轴同极对数，频率都锁定在同一电角度上。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

LOAD_MODES = {
    "unknown": "未记录",
    "none": "无对拖负载",
    "off": "负载驱动器断电（绕组开路，几乎无电磁制动）",
    "shorted": "负载驱动器上电未启动（50% 占空比，绕组等效短接）",
    "torque": "负载电机转矩控制运行",
}


_SETTINGS = ("MotorControlHMI", "dyno")


def dyno_setting() -> tuple[str, float | None]:
    """当前台架的对拖设置（实验管理页与功率流页共用）：(工况, 负载侧转动惯量或 None=同型号)。"""
    from PySide6.QtCore import QSettings
    settings = QSettings(*_SETTINGS)
    mode = str(settings.value("mode", "none"))
    value = settings.value("load_inertia", None)
    try:
        inertia = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        inertia = None
    return (mode if mode in LOAD_MODES else "none"), inertia


def save_dyno_setting(mode: str | None = None, load_inertia: float | None = None) -> None:
    from PySide6.QtCore import QSettings
    settings = QSettings(*_SETTINGS)
    if mode is not None:
        settings.setValue("mode", mode)
    if load_inertia is not None:
        settings.setValue("load_inertia", float(load_inertia))


@dataclass(frozen=True)
class LoadMotor:
    rs_ohm: float = 0.59            # 相电阻
    ls_h: float = 0.66e-3           # 相电感（Ld ≈ Lq）
    psi_wb: float = 0.035 / 6.0     # 永磁磁链；默认与上位机转矩常数 0.035 N·m/A 一致
    pole_pairs: int = 4
    extra_r_ohm: float = 0.0        # 短接回路附加电阻（MOSFET 导通电阻、线缆）

    @property
    def r_loop(self) -> float:
        return self.rs_ohm + self.extra_r_ohm

    @classmethod
    def from_motor(cls, params, kt_nm_per_a: float | None = None,
                   extra_r_ohm: float = 0.0) -> "LoadMotor":
        """params 为数字孪生 PMSMParams；kt 给出时按 ψ = kt/(1.5p) 取磁链，与转矩测量同一口径。"""
        p = int(getattr(params, "pole_pairs", 4))
        psi = (kt_nm_per_a / (1.5 * p) if kt_nm_per_a else float(getattr(params, "psi_f", 0.035 / 6)))
        ls = 0.5 * (float(getattr(params, "Ld", 0.66e-3)) + float(getattr(params, "Lq", 0.66e-3)))
        return cls(float(getattr(params, "Rs", 0.59)), ls, psi, p, extra_r_ohm)


def short_circuit_brake(omega_m, motor: LoadMotor) -> dict:
    """转速 ωm（rad/s，可为数组）→ 制动转矩（正值，N·m）、相电流峰值（A）、铜损（W）。"""
    w = np.abs(np.asarray(omega_m, dtype=float))
    we = motor.pole_pairs * w
    r, x = motor.r_loop, we * motor.ls_h
    z2 = r * r + x * x
    torque = 1.5 * motor.pole_pairs * motor.psi_wb ** 2 * we * r / z2
    current = we * motor.psi_wb / np.sqrt(z2)
    return {"torque_nm": torque, "current_peak_a": current, "power_w": torque * w,
            "id_a": -we * we * motor.ls_h * motor.psi_wb / z2,
            "iq_a": -we * motor.psi_wb * r / z2}


def brake_characteristics(motor: LoadMotor) -> dict:
    """最大制动转矩点与高速电流极限。"""
    we_peak = motor.r_loop / motor.ls_h
    rpm_peak = we_peak / motor.pole_pairs * 60.0 / (2 * math.pi)
    peak = short_circuit_brake(we_peak / motor.pole_pairs, motor)
    return {"peak_speed_rpm": rpm_peak, "peak_torque_nm": float(peak["torque_nm"]),
            "peak_current_a": float(peak["current_peak_a"]),
            "peak_power_w": float(peak["power_w"]),
            "limit_current_a": motor.psi_wb / motor.ls_h,
            "viscous_nm_s": 1.5 * motor.pole_pairs ** 2 * motor.psi_wb ** 2 / motor.r_loop}


def measured_torque_speed(time, speed_rpm, iq, kt: float, block_s: float = 0.05) -> dict:
    """按块平均得到实测 (ωm, Te = kt·iq, dω/dt)；dω/dt 用 4 块滑动平滑后求导。"""
    t = np.asarray(time, float)
    rate = (t.size - 1) / max(t[-1] - t[0], 1e-9)
    blk = max(1, int(round(block_s * rate)))
    n = t.size // blk
    if n < 8:
        raise ValueError("数据太短，无法按块统计转矩-转速关系")

    def mean(x):
        return np.asarray(x, float)[:n * blk].reshape(n, blk).mean(axis=1)

    tb = mean(t)
    w = mean(speed_rpm) * 2 * math.pi / 60.0
    te = kt * mean(iq)
    smooth = np.convolve(w, np.ones(4) / 4, mode="same")
    dw = np.gradient(smooth, tb)
    valid = np.zeros(n, bool)
    valid[4:n - 4] = True
    return {"time": tb, "omega": w, "torque": te, "domega": dw, "valid": valid}


def compare_with_measurement(time, speed_rpm, iq, kt: float, motor: LoadMotor) -> dict:
    """稳态段（|dω/dt| 小、转速高于 5 rad/s）实测转矩与短路制动预测对比。"""
    m = measured_torque_speed(time, speed_rpm, iq, kt)
    steady = m["valid"] & (np.abs(m["domega"]) < 2.0) & (m["omega"] > 5.0)
    if not steady.any():
        raise ValueError("没有稳速段，无法与短路制动模型对比")
    # 按转速分档：排序后相邻值相差超过 3% 就断开，不同稳速平台各给一行
    omegas = np.sort(m["omega"][steady])
    levels, start = [], 0
    for i in range(1, omegas.size + 1):
        if i == omegas.size or omegas[i] > omegas[i - 1] * 1.03:
            if i - start >= 10:                     # 至少 0.5 s
                levels.append(float(np.median(omegas[start:i])))
            start = i
    rows = []
    for level in levels:
        sel = steady & (np.abs(m["omega"] - level) <= 0.05 * level)
        w, te = float(m["omega"][sel].mean()), float(m["torque"][sel].mean())
        pred = short_circuit_brake(w, motor)
        rows.append({"speed_rpm": w * 60 / (2 * math.pi), "measured_torque_nm": te,
                     "predicted_brake_nm": float(pred["torque_nm"]),
                     "residual_nm": te - float(pred["torque_nm"]),
                     "ratio": te / float(pred["torque_nm"]) if pred["torque_nm"] > 0 else None,
                     "measured_power_w": te * w, "predicted_load_loss_w": float(pred["power_w"]),
                     "predicted_load_current_a": float(pred["current_peak_a"]),
                     "seconds": float(sel.sum() * (m["time"][1] - m["time"][0]))})
    return {"levels": rows, "blocks": m, "steady_mask": steady}
