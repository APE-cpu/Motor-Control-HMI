"""PMSM dq 轴物理仿真——数字孪生 L1（虚拟下位机）。

模型：dq 电压方程 + 电磁转矩方程 + 机械方程 + 一阶热模型，
内嵌转速/电流双闭环 PI（相当于下位机固件的控制环），
欧拉法 0.5 ms 步长积分，上层每 0.1 s 采样一次。

母线硬件层（L2）：电源不再是理想电压源——内阻导致重载下垂，
防反灌二极管使减速回馈时母线泵升，超过滞环阈值由制动斩波器
泄放，仍超过过压阈值则封管跳闸；可用电压 v_lim 随母线实时变化。

同时充当"虚拟下位机"：可接收启动/停止/急停/目标转速指令，
仿真模式下控制页的操作产生真实的动态响应（阶跃、超调、滑行停机）。

注意：默认参数是 48V 小功率 PMSM 的占位值，拿到实际电机铭牌参数后
替换 PMSMParams 即可。
"""
import math
from collections import deque
from dataclasses import dataclass


@dataclass
class PMSMParams:
    Rs: float = 0.5          # 定子电阻 Ω
    Ld: float = 1.0e-3       # d 轴电感 H
    Lq: float = 1.2e-3       # q 轴电感 H
    psi_f: float = 0.016     # 永磁磁链 Wb（基速约 4000 rpm > 上位机默认最高 3000）
    pole_pairs: int = 4
    J: float = 2.0e-3        # 转动惯量 kg·m²（含负载/联轴器，滑行时间约数秒）
    B: float = 1.5e-3        # 粘滞摩擦系数 N·m·s/rad
    T_coulomb: float = 0.1   # 库仑摩擦 N·m
    T_cogging: float = 0.03  # 齿槽转矩幅值 N·m（6 倍电角频率脉动）
    Vdc: float = 48.0        # 电源空载电压 V（母线额定）
    i_max: float = 8.0       # 电流限幅 A
    # 直流母线硬件（数字孪生 L2：电源内阻 + 母线电容 + 制动斩波器）
    r_src: float = 0.3       # 电源内阻 Ω（重载时母线下垂的来源）
    c_bus: float = 2.0e-3    # 母线电容 F
    r_brake: float = 10.0    # 制动斩波电阻 Ω
    v_brake_on: float = 54.0     # 斩波开启阈值 V（滞环上限）
    v_brake_off: float = 51.0    # 斩波关闭阈值 V（滞环下限）
    v_ov_trip: float = 60.0      # 过压跳闸阈值 V（封管保护）
    v_uv_warn: float = 42.0      # 欠压告警阈值 V
    # 一阶热模型（铜损发热）
    rth: float = 1.5         # 热阻 K/W
    tau_th: float = 60.0     # 热时间常数 s
    t_amb: float = 25.0      # 环境温度 °C


class MotorSim:
    """PMSM 物理模型 + 虚拟固件（转速/电流双闭环 PI）。"""

    DT = 5.0e-4  # 积分步长 s

    def __init__(self, params: PMSMParams = None) -> None:
        self.p = params or PMSMParams()
        # 高速轨迹缓冲（模拟下位机突发快照）：1 kHz 的 (θe, id, iq)
        self.trace: deque = deque(maxlen=2000)
        self._trace_n = 0
        self.reset()

    def reset(self) -> None:
        self.i_d = 0.0
        self.i_q = 0.0
        self.trace.clear()
        self._trace_n = 0
        self.omega = 0.0          # 机械角速度 rad/s
        self.theta = 0.0          # 机械角 rad（0~2π）
        self.temp = self.p.t_amb
        self.enabled = False      # 逆变器使能
        self.vdc = self.p.Vdc     # 母线电压状态量 V
        self.brake_on = False     # 制动斩波器导通中
        self.ov_trip = False      # 过压跳闸锁存（start 复位）
        self.uv_warn = False      # 欠压告警
        # 功率流各级快照 W（功率流页消费）
        self.p_supply = 0.0       # 电源发出（含内阻损耗）
        self.p_loss_src = 0.0     # 电源内阻损耗
        self.p_brake = 0.0        # 制动电阻泄放
        self.p_inv = 0.0          # 逆变器直流侧输入（回馈时为负）
        self.p_cu = 0.0           # 定子铜损
        self.p_em = 0.0           # 电磁功率（气隙→机械）
        self.p_fric = 0.0         # 摩擦/负载耗散
        self.p_kinetic = 0.0      # 动能变化率（加速为正）
        self.speed_ref_rpm = 0.0
        self.iq_ref = 0.0
        self.load_ext = 0.0       # 外部负载转矩 N·m（测功机/扫频注入）
        self._int_spd = 0.0       # PI 积分器
        self._int_d = 0.0
        self._int_q = 0.0

    # ---------- 虚拟下位机指令接口 ----------
    def start(self, target_rpm: float = None) -> None:
        if target_rpm is not None:
            self.speed_ref_rpm = float(target_rpm)
        self.ov_trip = False     # 重新使能视为故障复位
        self.enabled = True

    def set_load(self, torque_nm: float) -> None:
        """设置外部负载转矩（数字孪生 L2：模拟测功机加载）。"""
        self.load_ext = float(torque_nm)

    def stop(self) -> None:
        """封管停机：切断驱动，靠负载转矩自然滑行到零。"""
        self.enabled = False
        self.speed_ref_rpm = 0.0

    def emergency_stop(self) -> None:
        self.stop()
        self._int_spd = self._int_d = self._int_q = 0.0

    def set_speed_target(self, rpm: float) -> None:
        self.speed_ref_rpm = float(rpm)

    # ---------- 仿真步进 ----------
    def step(self, duration: float) -> None:
        n = max(1, round(duration / self.DT))
        for _ in range(n):
            self._step_once(self.DT)

    def _step_once(self, dt: float) -> None:
        p = self.p
        we = self.omega * p.pole_pairs          # 电角速度
        theta_e = self.theta * p.pole_pairs
        v_lim = self.vdc / math.sqrt(3.0)   # 可用电压随母线实时变化
        vd = vq = 0.0

        if self.enabled:
            # --- 转速环（输出 iq 给定，带限幅抗饱和）---
            spd_err = self.speed_ref_rpm * math.pi / 30.0 - self.omega
            self._int_spd += spd_err * dt
            iq_ref = 0.06 * spd_err + 1.0 * self._int_spd
            if abs(iq_ref) > p.i_max:
                iq_ref = math.copysign(p.i_max, iq_ref)
                self._int_spd -= spd_err * dt   # 饱和时冻结积分
            self.iq_ref = iq_ref

            # --- 电流环（PI + 交叉解耦前馈）---
            ed = 0.0 - self.i_d
            eq = iq_ref - self.i_q
            self._int_d += ed * dt
            self._int_q += eq * dt
            vd = 1.2 * ed + 300.0 * self._int_d - we * p.Lq * self.i_q
            vq = 1.2 * eq + 300.0 * self._int_q + we * (p.Ld * self.i_d + p.psi_f)
            v_mag = math.hypot(vd, vq)
            if v_mag > v_lim:                    # 电压限幅（过调制截断）
                scale = v_lim / v_mag
                vd *= scale
                vq *= scale
                self._int_d -= ed * dt
                self._int_q -= eq * dt

            # --- 电磁方程 ---
            did = (vd - p.Rs * self.i_d + we * p.Lq * self.i_q) / p.Ld
            diq = (vq - p.Rs * self.i_q - we * (p.Ld * self.i_d + p.psi_f)) / p.Lq
            self.i_d += did * dt
            self.i_q += diq * dt
        else:
            # 封管后忽略续流，电流视为快速衰减到零
            self.i_d = self.i_q = 0.0
            self.iq_ref = 0.0

        # --- 转矩与机械方程 ---
        te = 1.5 * p.pole_pairs * (p.psi_f * self.i_q
                                   + (p.Ld - p.Lq) * self.i_d * self.i_q)
        te += p.T_cogging * math.sin(6.0 * theta_e)
        t_load = p.B * self.omega
        if abs(self.omega) > 0.5:
            t_load += math.copysign(p.T_coulomb, self.omega)
            # 外部负载：转动时按转向阻转（测功机加载）
            t_load += math.copysign(self.load_ext, self.omega)
        elif not self.enabled:
            self.omega = 0.0     # 低速滑行时库仑摩擦直接锁死，避免过零抖动
        domega = (te - t_load) / p.J
        self.omega += domega * dt
        self.theta = (self.theta + self.omega * dt) % (2.0 * math.pi)

        # --- 直流母线动力学：电源(内阻+防反灌二极管) + 电容 + 制动斩波器 ---
        # 逆变器直流侧电流 = 电机电功率 / 母线电压（忽略开关损耗）
        p_inv = 1.5 * (vd * self.i_d + vq * self.i_q) if self.enabled else 0.0
        i_inv = p_inv / max(self.vdc, 1.0)
        if self.vdc >= p.v_brake_on:        # 斩波器滞环
            self.brake_on = True
        elif self.vdc <= p.v_brake_off:
            self.brake_on = False
        i_brk = self.vdc / p.r_brake if self.brake_on else 0.0
        self.p_brake = i_brk * self.vdc
        if self.vdc >= p.Vdc:
            # 回馈泵升段：二极管截止，电源不吸收能量，电容独自充/放电
            self.vdc += (-i_inv - i_brk) * dt / p.c_bus
            i_src = 0.0
        else:
            # 电源支路隐式欧拉（r_src·c_bus 远小于 dt，显式积分会发散）
            self.vdc = ((self.vdc + dt / p.c_bus * (p.Vdc / p.r_src - i_inv - i_brk))
                        / (1.0 + dt / (p.r_src * p.c_bus)))
            i_src = max(0.0, (p.Vdc - self.vdc) / p.r_src)
        self.vdc = max(self.vdc, 0.0)
        self.p_supply = p.Vdc * i_src
        self.p_loss_src = i_src * i_src * p.r_src
        self.p_inv = p_inv
        self.uv_warn = self.vdc < p.v_uv_warn
        if self.vdc > p.v_ov_trip and not self.ov_trip:
            self.ov_trip = True
            self.stop()                     # 过压保护：封管滑行

        # --- 一阶热模型（铜损）---
        p_cu = 1.5 * p.Rs * (self.i_d ** 2 + self.i_q ** 2)
        t_ss = p.t_amb + p_cu * p.rth
        self.temp += (t_ss - self.temp) * dt / p.tau_th

        # --- 功率流快照（机械侧）---
        self.p_cu = p_cu
        self.p_em = te * self.omega
        self.p_fric = t_load * self.omega
        self.p_kinetic = self.p_em - self.p_fric   # 剩余功率进入/取自转动动能

        # --- 高速轨迹（每 2 个积分步记一点 = 1 kHz）---
        self._trace_n += 1
        if self._trace_n >= 2:
            self._trace_n = 0
            self.trace.append((self.theta * p.pole_pairs, self.i_d, self.i_q))

    # ---------- 采样输出 ----------
    @property
    def speed_rpm(self) -> float:
        return self.omega * 30.0 / math.pi

    @property
    def torque(self) -> float:
        p = self.p
        return 1.5 * p.pole_pairs * (p.psi_f * self.i_q
                                     + (p.Ld - p.Lq) * self.i_d * self.i_q)

    @property
    def torque_ref(self) -> float:
        return 1.5 * self.p.pole_pairs * self.p.psi_f * self.iq_ref

    @property
    def angle_deg(self) -> float:
        return math.degrees(self.theta)

    @property
    def bus_state(self) -> str:
        """母线状态："ov"跳闸 > "brake"斩波 > "uv"欠压 > "normal"。"""
        if self.ov_trip:
            return "ov"
        if self.brake_on:
            return "brake"
        if self.uv_warn:
            return "uv"
        return "normal"
