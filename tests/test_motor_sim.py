"""MotorSim 物理合理性测试：闭环调速、滑行停机、母线与热模型行为。"""
import math

import pytest

from communications.motor_sim import MotorSim, PMSMParams


@pytest.fixture
def sim():
    return MotorSim()


def test_初始状态静止(sim):
    assert sim.speed_rpm == 0.0
    assert not sim.enabled
    assert sim.bus_state == "normal"


def test_启动后收敛到目标转速(sim):
    sim.start(1500.0)
    sim.step(3.0)
    assert sim.speed_rpm == pytest.approx(1500.0, rel=0.02)


def test_变更目标转速能跟随(sim):
    sim.start(1500.0)
    sim.step(3.0)
    sim.set_speed_target(2500.0)
    sim.step(3.0)
    assert sim.speed_rpm == pytest.approx(2500.0, rel=0.02)


def test_电流不超过限幅(sim):
    sim.start(3000.0)   # 大阶跃，转速环必然饱和
    peak = 0.0
    for _ in range(60):
        sim.step(0.05)
        peak = max(peak, math.hypot(sim.i_d, sim.i_q))
    assert peak <= sim.p.i_max * 1.05   # 允许积分步进带来的轻微过冲


def test_封管滑行最终停到零(sim):
    sim.start(1500.0)
    sim.step(3.0)
    sim.stop()
    sim.step(15.0)
    assert abs(sim.speed_rpm) < 1.0
    assert sim.i_d == 0.0 and sim.i_q == 0.0


def test_稳态转矩平衡摩擦(sim):
    """稳态时电磁转矩 ≈ B·ω + Tc（转速环收敛的物理体现）。"""
    sim.start(2000.0)
    sim.step(4.0)
    t_load = sim.p.B * sim.omega + sim.p.T_coulomb
    assert sim.torque == pytest.approx(t_load, rel=0.1)


def test_负载运行温度上升且不超稳态值(sim):
    sim.start(2500.0)
    sim.step(5.0)
    p_cu = 1.5 * sim.p.Rs * (sim.i_d ** 2 + sim.i_q ** 2)
    t_ss = sim.p.t_amb + p_cu * sim.p.rth
    assert sim.temp > sim.p.t_amb
    assert sim.temp <= t_ss + 0.5


def test_稳态母线电压低于空载电压(sim):
    """带载时电源内阻分压，母线必然低于空载电压且高于欠压阈值。"""
    sim.start(2000.0)
    sim.step(3.0)
    assert sim.vdc < sim.p.Vdc
    assert sim.vdc > sim.p.v_uv_warn
    assert sim.bus_state == "normal"


def test_急停复位积分器(sim):
    sim.start(1500.0)
    sim.step(1.0)
    sim.emergency_stop()
    assert not sim.enabled
    assert sim._int_spd == 0.0 and sim._int_d == 0.0 and sim._int_q == 0.0


def test_reset恢复初始状态(sim):
    sim.start(1500.0)
    sim.step(2.0)
    sim.reset()
    assert sim.speed_rpm == 0.0
    assert sim.temp == sim.p.t_amb
    assert sim.vdc == sim.p.Vdc
    assert len(sim.trace) == 0


def test_高速轨迹缓冲以1kHz记录(sim):
    sim.start(1000.0)
    sim.step(0.1)   # 0.1 s → 约 100 个 1 kHz 采样点
    assert 95 <= len(sim.trace) <= 105
    theta_e, i_d, i_q = sim.trace[-1]
    assert 0.0 <= theta_e < 2.0 * math.pi * sim.p.pole_pairs


def test_自定义参数生效():
    p = PMSMParams(J=5.0e-3)
    sim = MotorSim(p)
    sim.start(1500.0)
    sim.step(0.5)
    slow = sim.speed_rpm
    sim2 = MotorSim()
    sim2.start(1500.0)
    sim2.step(0.5)
    assert slow < sim2.speed_rpm   # 惯量大 2.5 倍，加速必然更慢
