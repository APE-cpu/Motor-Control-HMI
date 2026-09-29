import math

import numpy as np
import pytest

from core.dyno_load import (
    LoadMotor, brake_characteristics, compare_with_measurement, short_circuit_brake,
)


def test_短路制动满足电压方程且功率等于铜损():
    motor = LoadMotor()
    omega = 52.0
    out = short_circuit_brake(omega, motor)
    we = motor.pole_pairs * omega
    i_d, i_q = float(out["id_a"]), float(out["iq_a"])
    # 负载电机 dq 电压方程（端电压为零）
    assert abs(motor.r_loop * i_d - we * motor.ls_h * i_q) < 1e-9
    assert abs(motor.r_loop * i_q + we * motor.ls_h * i_d + we * motor.psi_wb) < 1e-9
    assert math.isclose(float(out["power_w"]),
                        1.5 * motor.r_loop * (i_d ** 2 + i_q ** 2), rel_tol=1e-9)
    assert math.isclose(float(out["torque_nm"]), -1.5 * motor.pole_pairs * motor.psi_wb * i_q,
                        rel_tol=1e-9)


def test_制动转矩在R除以L处最大且高速电流趋近ψ除以L():
    motor = LoadMotor()
    ch = brake_characteristics(motor)
    rpm = np.linspace(10, 20000, 4000)
    torque = short_circuit_brake(rpm * 2 * math.pi / 60, motor)["torque_nm"]
    assert abs(rpm[np.argmax(torque)] - ch["peak_speed_rpm"]) < 10
    assert math.isclose(ch["peak_torque_nm"],
                        1.5 * motor.pole_pairs * motor.psi_wb ** 2 / (2 * motor.ls_h), rel_tol=1e-6)
    high = short_circuit_brake(1e5, motor)["current_peak_a"]
    assert abs(float(high) - ch["limit_current_a"]) / ch["limit_current_a"] < 0.01


def test_对拖功率估算计入负载侧惯量并单列负载短路铜损():
    from core.power_estimate import PowerParams, estimate_power_series, power_summary
    rate = 2000.0
    t = np.arange(0, 4.0, 1 / rate)
    rpm = np.clip(t * 250.0, 0, 500.0)               # 2 s 加速到 500 rpm 再保持
    columns = {"iq_a": np.full(t.size, 1.9), "vq_raw": np.full(t.size, 8000.0),
               "vbus_v": np.full(t.size, 24.0), "speed_rpm": rpm}
    single = PowerParams()
    shorted = single.with_dyno("shorted")
    assert shorted.total_inertia == 2 * single.inertia        # 同型号负载电机
    a = estimate_power_series(t, columns, single)
    b = estimate_power_series(t, columns, shorted)
    accel = (a["time"] > 0.5) & (a["time"] < 1.5)
    assert np.allclose(b["kinetic"][accel], 2 * a["kinetic"][accel])
    assert b["load_cu"][-1] > 0 and np.all(a["load_cu"] == 0)
    # 电磁功率 = 动能 + 负载短路铜损 + 摩擦与其余负载（守恒）
    assert np.allclose(b["em"], b["kinetic"] + b["load_cu"] + b["fric"])
    assert b["stored_j"][-1] == pytest.approx(0.5 * shorted.total_inertia * (500 * np.pi / 30) ** 2, rel=1e-3)
    assert power_summary(b)["peak_stored_j"] > power_summary(a)["peak_stored_j"]
    assert single.with_dyno("none").load_inertia == 0.0


def test_实测转矩等于短路制动时比值接近1():
    motor = LoadMotor()
    kt = 1.5 * motor.pole_pairs * motor.psi_wb
    rate = 2000.0
    t = np.arange(0, 12.0, 1 / rate)
    rpm = np.where(t < 2.0, 150.0, np.where(t < 4.0, 150.0 + (t - 2.0) * 175.0, 500.0))
    omega = rpm * 2 * math.pi / 60
    iq = short_circuit_brake(omega, motor)["torque_nm"] / kt
    result = compare_with_measurement(t, rpm, iq, kt, motor)
    top = max(result["levels"], key=lambda row: row["speed_rpm"])
    assert abs(top["speed_rpm"] - 500.0) < 1.0
    assert abs(top["ratio"] - 1.0) < 0.01
