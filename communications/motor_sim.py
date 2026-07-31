"""PMSM dq 轴物理仿真——数字孪生 L1（虚拟下位机）。

模型：dq 电压方程 + 电磁转矩方程 + 机械方程 + 一阶热模型，
内嵌转速/电流双闭环 PI（相当于下位机固件的控制环），
欧拉法 0.5 ms 步长积分，上层每 0.1 s 采样一次。

母线硬件层（L2）：电源不再是理想电压源——内阻导致重载下垂，
防反灌二极管使减速回馈时母线泵升，超过滞环阈值由制动斩波器
泄放，仍超过过压阈值则封管跳闸；可用电压 v_lim 随母线实时变化。

同时充当"虚拟下位机"：可接收启动/停止/急停/目标转速指令，
仿真模式下控制页的操作产生真实的动态响应（阶跃、超调、滑行停机）。

默认参数对齐野火 42JSF840AS-1000-8 PMSM（24V/4000rpm/4对极/0.59Ω/
0.66mH/反电动势2.95V/Krpm）。拿到实际铭牌参数后替换 PMSMParams 字段即可
未知参数（B/Tc/J_load/热模型）为同规格电机的估算值，建议用辨识页实测后回写。
"""
import math
from collections import deque
from dataclasses import dataclass


@dataclass
class PMSMParams:
    # ── 电机本体（铭牌：野火 42JSF840AS-1000-8，24V/4000rpm/4对极） ──
    Rs: float = 0.59         # 相电阻 Ω（铭牌 0.59±10%）
    Ld: float = 0.66e-3      # d 轴电感 H（铭牌相电感 0.66±20% mH，表贴式 Ld≈Lq）
    Lq: float = 0.66e-3      # q 轴电感 H（同上，凸极率≈1）
    psi_f: float = 7.04e-3  # 永磁磁链 Wb（由铭牌反电动势 2.95V/Krpm 换算：
                             # ψf = 2.95/(1000×2π/60)/pole_pairs ≈ 0.00704）
    pole_pairs: int = 4     # 极对数（铭牌）
    J: float = 1.85e-5       # 转动惯量 kg·m²（铭牌 1.85×10⁻⁵，仅转子不含负载）
    B: float = 1.5e-4        # 粘滞摩擦系数 N·m·s/rad（小电机估取 1e-4 量级）
    T_coulomb: float = 0.01 # 库仑摩擦 N·m（小电机估取 0.01 量级，约额定力矩5%）
    T_cogging: float = 5e-3 # 齿槽转矩幅值 N·m（6 倍电角频率脉动，小电机估取）
    # ── 母线与电流（24V 平台） ──
    Vdc: float = 24.0       # 电源空载电压 V（铭牌 24V DC）
    i_max: float = 8.0      # 电流限幅 A（额定电流约4.5A，留 1.8× 余量）
    # 直流母线硬件（数字孪生 L2：电源内阻 + 母线电容 + 制动斩波器）
    r_src: float = 0.2      # 电源内阻 Ω（24V 小功率平台估取 0.2）
    c_bus: float = 1.0e-3   # 母线电容 F（24V 平台估取 1mF）
    r_brake: float = 10.0   # 制动斩波电阻 Ω（24V 小电机通常无斩波，保留模型结构）
    v_brake_on: float = 27.0    # 斩波开启阈值 V（1.125×Vdc，滞回上限）
    v_brake_off: float = 25.5   # 斩波关闭阈值 V（1.0625×Vdc，滞回下限）
    v_ov_trip: float = 30.0      # 过压跳闸阈值 V（1.25×Vdc，封管保护）
    v_uv_warn: float = 21.0      # 欠压告警阈值 V（0.875×Vdc）
    # 一阶热模型（铜损发热）
    rth: float = 3.0        # 热阻 K/W（小电机散热面积小，估取 3 K/W）
    tau_th: float = 120.0   # 热时间常数 s（小电机热容小但散热慢，估取 120s）
    t_amb: float = 25.0     # 环境温度 °C


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
        # 负载扰动：在 load_ext 之上叠加，用于突加/突卸测试与周期扰动
        self._pulse_amp = 0.0     # 一次性阶跃扰动幅值 N·m
        self._pulse_left = 0.0    # 一次性阶跃剩余时间 s（>0 时生效）
        self._disturb_amp = 0.0   # 周期方波扰动幅值 N·m（0=关闭）
        self._disturb_period = 0.0  # 周期方波周期 s
        self._disturb_phase = 0.0   # 周期方波相位累加器 s
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

    def pulse_load(self, delta_nm: float, duration_s: float = 1.0) -> None:
        """一次性负载阶跃：在 load_ext 之上叠加 delta_nm，持续 duration_s 秒后自动撤除。

        经典的负载突加/突卸扰动测试——观察转速跌落深度与恢复时间，
        直接反映控制器的抗扰能力。delta_nm 可正（突加）可负（突卸）。
        """
        self._pulse_amp = float(delta_nm)
        self._pulse_left = max(0.0, float(duration_s))

    def set_load_disturbance(self, amplitude_nm: float, period_s: float = 2.0) -> None:
        """周期方波负载扰动：每半周期在 +/-amplitude 之间翻转，持续叠加。

        让运行曲线保持丰富（转速/电流随扰动持续起伏）。amplitude=0 关闭。
        """
        self._disturb_amp = float(amplitude_nm)
        self._disturb_period = max(0.0, float(period_s))
        self._disturb_phase = 0.0

    def _disturbance_torque(self, dt: float) -> float:
        """计算当前帧的扰动转矩（一次性阶跃 + 周期方波之和），并推进计时器。"""
        extra = 0.0
        # 一次性阶跃
        if self._pulse_left > 0.0:
            extra += self._pulse_amp
            self._pulse_left -= dt
        # 周期方波：前半周期 +amp，后半周期 -amp
        if self._disturb_amp != 0.0 and self._disturb_period > 0.0:
            self._disturb_phase = (self._disturb_phase + dt) % self._disturb_period
            extra += (self._disturb_amp if self._disturb_phase < self._disturb_period / 2.0
                      else -self._disturb_amp)
        return extra

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
            # PI 增益按 24V/0.59Ω/0.66mH/1.85e-5 kg·m² 电机重整：
            # Kt=1.5·p·ψf≈0.0423 N·m/A，J 比 48V 平台小 100×，
            # 速度环 Kp 维持 0.06（小 J 电机对增益不敏感，保持原值即可
            # 达到相近带宽）；Ki 适当上调以补偿小惯量下的稳态精度。
            spd_err = self.speed_ref_rpm * math.pi / 30.0 - self.omega
            self._int_spd += spd_err * dt
            iq_ref = 0.06 * spd_err + 2.0 * self._int_spd
            if abs(iq_ref) > p.i_max:
                iq_ref = math.copysign(p.i_max, iq_ref)
                self._int_spd -= spd_err * dt   # 饱和时冻结积分
            self.iq_ref = iq_ref

            # --- 电流环（PI + 交叉解耦前馈）---
            # 电流环带宽须 ≫ 速度环；24V 平台 Ld/Lq=0.66mH、Rs=0.59Ω，
            # 电气时间常数 L/R≈1.1ms，Kp/Ki 维持 1.2/300 在该 L/R 下仍
            # 稳定（原 48V 平台 L/R≈2ms 也用同值，覆盖范围足够）。
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
        # 有效外部负载 = 恒定负载 + 扰动（阶跃/周期方波），带符号（正=阻转，负=助力/回馈）
        load_eff = self.load_ext + self._disturbance_torque(dt)
        t_load = p.B * self.omega
        if abs(self.omega) > 0.5:
            t_load += math.copysign(p.T_coulomb, self.omega)
            # 外部负载按转向叠加：正值阻转（测功机加载），负值助力（对拖回馈）
            spin = 1.0 if self.omega > 0.0 else -1.0
            t_load += spin * load_eff
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
