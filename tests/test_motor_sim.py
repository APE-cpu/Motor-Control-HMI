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


def test_外部负载降低稳态转速裕度并增大电流(sim):
    """加外部负载后，稳态电流（转矩电流）应显著增大。"""
    sim.start(2000.0)
    sim.step(4.0)
    iq_noload = sim.i_q
    # 0.30 N·m 在当前 Kt/摩擦参数下需约 8.08 A，超过 8 A 限流；
    # 用 0.28 N·m 验证“未饱和时闭环守速”的真实测试意图。
    sim.set_load(0.28)
    sim.step(4.0)
    assert sim.i_q > iq_noload + 0.5   # 负载靠更大 q 轴电流克服
    assert abs(sim.iq_ref) < sim.p.i_max
    assert sim.speed_rpm == pytest.approx(2000.0, rel=0.03)  # 闭环仍守住转速


def test_reset清零外部负载(sim):
    sim.set_load(0.5)
    sim.reset()
    assert sim.load_ext == 0.0


def test_一次性负载阶跃到时自动撤除(sim):
    """突加负载持续 duration 后应自动恢复：电流先升后回落。"""
    sim.start(2000.0)
    sim.step(4.0)
    iq_base = sim.i_q
    sim.pulse_load(0.4, duration_s=1.0)   # 突加 0.4 N·m，持续 1s
    sim.step(0.5)                          # 阶跃仍在
    iq_during = sim.i_q
    assert iq_during > iq_base + 0.3       # 扰动期间电流增大
    sim.step(2.0)                          # 阶跃早已到时撤除
    assert sim.i_q == pytest.approx(iq_base, abs=0.4)  # 回落到基线附近


def test_突卸负载电流下降(sim):
    """在已有恒定负载上突卸（负扰动），电流应短暂下降。"""
    sim.start(2000.0)
    sim.set_load(0.28)         # 不饱和的基础负载（0.5 会顶到 8A 限幅）
    sim.step(4.0)
    iq_base = sim.i_q
    sim.pulse_load(-0.25, duration_s=1.0)  # 突卸 0.25 N·m（负扰动=助力）
    sim.step(0.4)
    assert sim.i_q < iq_base - 0.2


def test_周期方波扰动使电流持续起伏(sim):
    """开启周期扰动后，采样一串电流应有明显波动（非恒定）。"""
    sim.start(2000.0)
    sim.set_load(0.3)
    sim.step(4.0)
    sim.set_load_disturbance(0.3, period_s=1.0)
    samples = []
    for _ in range(20):
        sim.step(0.1)
        samples.append(sim.i_q)
    assert max(samples) - min(samples) > 0.3   # 方波激励下电流起伏明显


def test_reset清零负载扰动(sim):
    sim.pulse_load(0.5, 2.0)
    sim.set_load_disturbance(0.4, 1.0)
    sim.reset()
    assert sim._pulse_left == 0.0
    assert sim._disturb_amp == 0.0


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


def test_位置三环阶跃收敛到机械角目标():
    """位置P输出速度给定，最终由速度/电流内环把角度拉到目标。"""
    sim = MotorSim()
    sim.start(position_mode=True, position_target_deg=5.0,
              kp_pos=8.0, kpf_pos=0.0,
              position_speed_limit_rpm=300.0)
    sim.step(1.0)
    assert sim.position_actual_deg == pytest.approx(5.0, abs=0.05)
    assert abs(sim.position_error_deg) < 0.05
    assert abs(sim.position_speed_cmd_rpm) < 5.0


def test_位置速度阻尼抵消同方向机械速度():
    """Kdθ 应从位置环速度指令中扣除实际机械速度，形成主动制动。"""
    plain = MotorSim()
    damped = MotorSim()
    common = dict(position_mode=True, position_target_deg=90.0,
                  kp_pos=8.0, kpf_pos=0.0,
                  position_speed_limit_rpm=300.0)
    plain.start(kd_pos=0.0, **common)
    damped.start(kd_pos=0.20, **common)
    omega_100rpm = 100.0 * math.pi / 30.0
    plain.omega = damped.omega = omega_100rpm
    plain._update_position_loop(0.005)
    damped._update_position_loop(0.005)
    assert plain.position_speed_cmd_rpm - damped.position_speed_cmd_rpm == pytest.approx(20.0)


def test_位置速度指令确认换向时卸载速度积分():
    """跨过±10 rpm确认带后只在真实换向时清除旧方向速度积分。"""
    sim = MotorSim()
    sim.start(position_mode=True, position_target_deg=90.0,
              kp_pos=8.0, kd_pos=0.0,
              position_speed_limit_rpm=300.0)
    sim.position_trajectory_deg = 90.0
    sim.position_trajectory_speed_rpm = 0.0
    sim._update_position_loop(0.005)
    assert sim._position_last_motion_sign == 1
    sim._int_spd = 123.0
    sim.position_ref_deg = -90.0
    sim.position_trajectory_deg = -90.0
    sim._update_position_loop(0.005)
    assert sim._position_last_motion_sign == -1
    assert sim._int_spd == 0.0


def test_旧ki_pos字段不再改变位置环输出():
    """兼容旧配置但不得恢复积分；ki_pos不同不得改变同一轨迹的输出。"""
    a = MotorSim()
    b = MotorSim()
    common = dict(kp_pos=2.0, kpf_pos=0.0,
                  position_speed_limit_rpm=300.0,
                  position_accel_limit_rpm_s=60.0)
    a.start(position_mode=True, position_target_deg=10.0,
            ki_pos=0.0, **common)
    b.start(position_mode=True, position_target_deg=10.0,
            ki_pos=999.0, **common)
    a.step(0.5)
    b.step(0.5)
    assert a.position_speed_cmd_rpm == pytest.approx(b.position_speed_cmd_rpm)
    assert a.position_actual_deg == pytest.approx(b.position_actual_deg)


def test_位置轨迹限制启动速度并产生有效前馈():
    """最终角度仍是阶跃输入，但内部轨迹应从零加速，Kpf不再恒为零。"""
    sim = MotorSim()
    sim.start(position_mode=True, position_target_deg=90.0,
              kp_pos=8.0, kpf_pos=0.1,
              position_speed_limit_rpm=60.0,
              position_accel_limit_rpm_s=30.0)
    sim.step(0.005)
    assert 0.0 < sim.position_trajectory_deg < 0.01
    assert sim.position_trajectory_speed_rpm == pytest.approx(0.15, rel=0.02)
    assert 0.0 < sim._position_ff_filtered < 1.0
    assert abs(sim.position_speed_cmd_rpm) < 1.0
    sim.step(0.495)
    assert sim.position_trajectory_deg < sim.position_ref_deg
    assert sim._position_ff_filtered > 1.0


def test_位置三环支持多圈目标():
    """连续机械角不能在±180°处折返，360°目标应完成一整圈。"""
    sim = MotorSim()
    sim.start(position_mode=True, position_target_deg=360.0,
              position_speed_limit_rpm=300.0)
    sim.step(4.0)
    assert sim.position_actual_deg == pytest.approx(360.0, abs=1.0)
