"""参数辨识算法端到端测试：用 MotorSim 生成已知真值数据，验证辨识精度。

复现 identify_page 的实验流程（两点稳态 + 滑行），但不经过 GUI：
数据由虚拟电机产生，辨识结果与 PMSMParams 真值对比。
"""
import pytest

from communications.motor_sim import MotorSim
from controllers.param_identify import fit_inertia, solve_friction, torque_constant


def _steady_point(sim: MotorSim, rpm: float):
    """升速并等稳态，返回 (ω rad/s, iq A)——对应页面的稳态均值采集。"""
    sim.start(rpm)
    sim.step(5.0)
    return sim.omega, sim.i_q


def _coast_records(sim: MotorSim, duration: float = 6.0, ts: float = 0.1):
    """封管滑行，按遥测周期 0.1 s 记录 (t, rpm)。"""
    sim.stop()
    records = []
    t = 0.0
    n = int(duration / ts)
    for _ in range(n):
        sim.step(ts)
        t += ts
        records.append((t, sim.speed_rpm))
    return records


def test_辨识精度_两点稳态加滑行():
    sim = MotorSim()
    kt = torque_constant(sim.p.psi_f, sim.p.pole_pairs)

    w1, i1 = _steady_point(sim, 1500.0)
    w2, i2 = _steady_point(sim, 2800.0)
    coast = _coast_records(sim)

    b_hat, tc_hat = solve_friction(w1, i1, w2, i2, kt)
    j_hat, used = fit_inertia(coast, b_hat, tc_hat)

    assert used >= 3
    assert b_hat == pytest.approx(sim.p.B, rel=0.05)
    assert tc_hat == pytest.approx(sim.p.T_coulomb, rel=0.05)
    assert j_hat == pytest.approx(sim.p.J, rel=0.05)


def test_转速点太接近报错():
    with pytest.raises(ValueError, match="太接近"):
        solve_friction(100.0, 1.0, 100.5, 1.01, kt=0.1)


def test_滑行数据不足报错():
    with pytest.raises(ValueError, match="太少"):
        fit_inertia([(0.0, 1000.0), (0.1, 1000.0)], b=1e-3, tc=0.1)


def test_滑行数据乱序时间被跳过():
    """dt<=0 的坏点应被剔除而不是导致除零/负 J。"""
    sim = MotorSim()
    sim.start(2500.0)
    sim.step(5.0)
    coast = _coast_records(sim)
    coast.insert(5, coast[4])   # 重复时间戳 → dt=0
    b, tc = sim.p.B, sim.p.T_coulomb
    j_hat, _ = fit_inertia(coast, b, tc)
    assert j_hat == pytest.approx(sim.p.J, rel=0.05)
