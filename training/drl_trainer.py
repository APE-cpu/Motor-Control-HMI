"""深度强化学习训练器：PPO / SAC / TD3，可选以 MPC 为参考策略做模仿预热。

环境：简化一阶电机模型  speed[k+1] = a*speed[k] + b*u[k]
状态：[speed, speed_target, u_prev]  (3维)
动作：u ∈ [-u_max, u_max]  (连续1维)
奖励：-|speed - speed_target| - 0.01*u²
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PySide6.QtCore import QObject, Signal


# ── 环境 ──────────────────────────────────────────────────────
class _MotorEnv:
    OBS_DIM = 3
    ACT_DIM = 1

    def __init__(self, u_max: float = 24.0, a: float = 0.9, b: float = 0.1) -> None:
        self.u_max = u_max
        self.a = a
        self.b = b
        self._speed = 0.0
        self._target = 1500.0
        self._u_prev = 0.0
        self._step = 0

    def reset(self) -> np.ndarray:
        self._speed = float(np.random.uniform(-500, 500))
        self._target = float(np.random.choice([500, 1000, 1500, 2000, -500]))
        self._u_prev = 0.0
        self._step = 0
        return self._obs()

    def step(self, action: float):
        u = float(np.clip(action, -self.u_max, self.u_max))
        self._speed = self.a * self._speed + self.b * u
        reward = -abs(self._speed - self._target) / 1500.0 - 0.01 * (u / self.u_max) ** 2
        self._u_prev = u
        self._step += 1
        done = self._step >= 200
        return self._obs(), reward, done

    def _obs(self) -> np.ndarray:
        return np.array([self._speed / 1500.0,
                         self._target / 1500.0,
                         self._u_prev / self.u_max], dtype=np.float32)


# ── 网络 ──────────────────────────────────────────────────────
class _Actor(nn.Module):
    def __init__(self, obs_dim: int, hidden: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1), nn.Tanh(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class _Critic(nn.Module):
    def __init__(self, obs_dim: int, hidden: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class _QNet(nn.Module):
    def __init__(self, obs_dim: int, act_dim: int, hidden: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_dim + act_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1),
        )

    def forward(self, obs: torch.Tensor, act: torch.Tensor) -> torch.Tensor:
        return self.net(torch.cat([obs, act], dim=-1))


# ── 经验回放 ──────────────────────────────────────────────────
class _ReplayBuffer:
    def __init__(self, capacity: int = 100_000) -> None:
        self._buf: deque = deque(maxlen=capacity)

    def push(self, obs, act, rew, next_obs, done) -> None:
        self._buf.append((obs, act, rew, next_obs, done))

    def sample(self, n: int):
        idx = np.random.choice(len(self._buf), n, replace=False)
        batch = [self._buf[i] for i in idx]
        obs, act, rew, nobs, done = zip(*batch)
        to = lambda x: torch.tensor(np.array(x), dtype=torch.float32)
        return to(obs), to(act), to(rew).unsqueeze(1), to(nobs), to(done).unsqueeze(1)

    def save(self, path: str) -> None:
        import csv
        header = ["obs_speed", "obs_target", "obs_uprev",
                  "action", "reward",
                  "nobs_speed", "nobs_target", "nobs_uprev", "done"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for obs, act, rew, nobs, done in self._buf:
                o = np.array(obs).flatten()
                a = np.array(act).flatten()
                no = np.array(nobs).flatten()
                w.writerow([*o, *a, float(rew), *no, float(done)])

    def load(self, path: str) -> None:
        import csv
        self._buf.clear()
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                obs = np.array([float(row["obs_speed"]), float(row["obs_target"]), float(row["obs_uprev"])], dtype=np.float32)
                act = np.array([float(row["action"])], dtype=np.float32)
                rew = float(row["reward"])
                nobs = np.array([float(row["nobs_speed"]), float(row["nobs_target"]), float(row["nobs_uprev"])], dtype=np.float32)
                done = float(row["done"])
                self._buf.append((obs, act, rew, nobs, done))

    def __len__(self) -> int:
        return len(self._buf)


# ── MPC 专家 ──────────────────────────────────────────────────
def _mpc_action(obs: np.ndarray, u_max: float = 24.0,
                a: float = 0.9, b: float = 0.1, N: int = 10,
                Q: float = 1.0, R: float = 0.01) -> float:
    speed = obs[0] * 1500.0
    target = obs[1] * 1500.0
    best_u, best_cost = 0.0, float("inf")
    for i in range(41):
        u = -u_max + 2 * u_max * i / 40
        y, cost = speed, 0.0
        for _ in range(N):
            y = a * y + b * u
            cost += Q * (target - y) ** 2 + R * u ** 2
        if cost < best_cost:
            best_cost, best_u = cost, u
    return best_u / u_max


# ── DRL 训练器 ────────────────────────────────────────────────
class DRLTrainer(QObject):
    epochDone = Signal(int, float, float)
    finished = Signal(str)
    error = Signal(str)
    mpcInfo = Signal(int, int, str, float, float)  # steps, buf_size, state_str, act, rew
    datasetReady = Signal(int)  # 生成完成，条数

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._expert_buf: _ReplayBuffer = _ReplayBuffer()

    def generate_expert_dataset(self, params: dict, n_steps: int = 10_000) -> None:
        """独立生成 MPC 专家数据集，不启动 RL 训练。"""
        self._stop.clear()
        t = threading.Thread(target=self._gen_expert, args=(params, n_steps), daemon=True)
        t.start()

    def _gen_expert(self, params: dict, n_steps: int) -> None:
        try:
            cfg = params.get("mpc_cfg", {})
            mpc_N = int(cfg.get("prediction_horizon", 10))
            mpc_Q = float(cfg.get("weight_q", 1.0))
            mpc_R = float(cfg.get("weight_r", 0.01))
            mpc_umax = float(cfg.get("u_max", 24.0))
            env = _MotorEnv(u_max=mpc_umax)
            _mpc = lambda obs: _mpc_action(obs, u_max=mpc_umax, N=mpc_N, Q=mpc_Q, R=mpc_R)
            obs = env.reset()
            for i in range(n_steps):
                if self._stop.is_set():
                    break
                act = _mpc(obs)
                nobs, rew, done = env.step(act * env.u_max)
                self._expert_buf.push(obs, [act], rew, nobs, float(done))
                obs = nobs if not done else env.reset()
                if i % 500 == 0:
                    speed = obs[0] * 1500.0
                    target = obs[1] * 1500.0
                    state_str = f"转速={speed:.0f}rpm 目标={target:.0f}rpm"
                    self.mpcInfo.emit(i, len(self._expert_buf), state_str, act, rew)
            self.datasetReady.emit(len(self._expert_buf))
        except Exception as e:
            self.error.emit(str(e))

    def save_expert_dataset(self, path: str) -> None:
        self._expert_buf.save(path)

    def load_expert_dataset(self, path: str) -> int:
        self._expert_buf.load(path)
        return len(self._expert_buf)

    def start(self, params: dict) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._run, args=(params,), daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, p: dict) -> None:
        try:
            algo = p.get("algorithm", "PPO")
            if algo == "PPO":
                self._train_ppo(p)
            else:
                self._train_offpolicy(p, algo)
        except Exception as exc:
            self.error.emit(str(exc))

    # ── PPO ───────────────────────────────────────────────────
    def _train_ppo(self, p: dict) -> None:
        cfg = p.get("mpc_cfg", {})
        mpc_N = int(cfg.get("prediction_horizon", 10))
        mpc_Q = float(cfg.get("weight_q", 1.0))
        mpc_R = float(cfg.get("weight_r", 0.01))
        mpc_umax = float(cfg.get("u_max", 24.0))
        env = _MotorEnv(u_max=mpc_umax)
        hidden = int(p.get("hidden_size", 256))
        actor = _Actor(env.OBS_DIM, hidden)
        critic = _Critic(env.OBS_DIM, hidden)
        opt_a = torch.optim.Adam(actor.parameters(), lr=float(p.get("actor_lr", 3e-4)))
        opt_c = torch.optim.Adam(critic.parameters(), lr=float(p.get("critic_lr", 3e-4)))
        gamma = float(p.get("gamma", 0.99))
        mpc_ref = bool(p.get("mpc_reference", True))
        total_steps = int(p.get("env_steps", 100_000))
        rollout_len = 512
        report_every = max(1, total_steps // 200)
        _mpc = lambda obs: _mpc_action(obs, u_max=mpc_umax, N=mpc_N, Q=mpc_Q, R=mpc_R)

        if mpc_ref:
            self._mpc_pretrain(actor, opt_a, env, steps=2000, mpc_fn=_mpc)

        obs = env.reset()
        step = 0
        while step < total_steps and not self._stop.is_set():
            # 收集 rollout
            obs_buf, act_buf, rew_buf, val_buf, logp_buf = [], [], [], [], []
            for _ in range(rollout_len):
                obs_t = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)
                with torch.no_grad():
                    mu = actor(obs_t).squeeze()
                    val = critic(obs_t).squeeze()
                noise = torch.randn_like(mu) * 0.2
                act = float((mu + noise).clamp(-1, 1))
                logp = float(-0.5 * noise.pow(2).sum())
                next_obs, rew, done = env.step(act * env.u_max)
                obs_buf.append(obs); act_buf.append([act])
                rew_buf.append(rew); val_buf.append(float(val)); logp_buf.append(logp)
                obs = next_obs if not done else env.reset()
                step += 1

            # GAE 优势估计
            returns, adv = [], []
            R = 0.0
            for r, v in zip(reversed(rew_buf), reversed(val_buf)):
                R = r + gamma * R
                returns.insert(0, R)
                adv.insert(0, R - v)
            adv_t = torch.tensor(adv, dtype=torch.float32)
            adv_t = (adv_t - adv_t.mean()) / (adv_t.std() + 1e-8)
            obs_t = torch.tensor(np.array(obs_buf), dtype=torch.float32)
            act_t = torch.tensor(np.array(act_buf), dtype=torch.float32)
            ret_t = torch.tensor(returns, dtype=torch.float32).unsqueeze(1)
            old_logp = torch.tensor(logp_buf, dtype=torch.float32)

            # PPO 更新
            for _ in range(4):
                mu_new = actor(obs_t).squeeze(1)
                noise_new = act_t.squeeze(1) - mu_new
                new_logp = -0.5 * noise_new.pow(2)
                ratio = (new_logp - old_logp).exp()
                clip_ratio = ratio.clamp(0.8, 1.2)
                actor_loss = -torch.min(ratio * adv_t, clip_ratio * adv_t).mean()
                opt_a.zero_grad(); actor_loss.backward(); opt_a.step()

            critic_loss = F.mse_loss(critic(obs_t), ret_t)
            opt_c.zero_grad(); critic_loss.backward(); opt_c.step()

            # DAgger：每隔一段用 MPC 纠正
            if mpc_ref and step % 10_000 < rollout_len:
                self._mpc_pretrain(actor, opt_a, env, steps=200, mpc_fn=_mpc)

            if step % report_every == 0:
                self.epochDone.emit(step, float(actor_loss.detach()), float(critic_loss.detach()))

        self.finished.emit(f"PPO 训练完成，共 {step} 步")

    # ── SAC / TD3 ─────────────────────────────────────────────
    def _train_offpolicy(self, p: dict, algo: str) -> None:
        cfg = p.get("mpc_cfg", {})
        mpc_N = int(cfg.get("prediction_horizon", 10))
        mpc_Q = float(cfg.get("weight_q", 1.0))
        mpc_R = float(cfg.get("weight_r", 0.01))
        mpc_umax = float(cfg.get("u_max", 24.0))
        env = _MotorEnv(u_max=mpc_umax)
        _mpc = lambda obs: _mpc_action(obs, u_max=mpc_umax, N=mpc_N, Q=mpc_Q, R=mpc_R)
        hidden = int(p.get("hidden_size", 256))
        actor = _Actor(env.OBS_DIM, hidden)
        q1 = _QNet(env.OBS_DIM, env.ACT_DIM, hidden)
        q2 = _QNet(env.OBS_DIM, env.ACT_DIM, hidden)
        q1_t = _QNet(env.OBS_DIM, env.ACT_DIM, hidden); q1_t.load_state_dict(q1.state_dict())
        q2_t = _QNet(env.OBS_DIM, env.ACT_DIM, hidden); q2_t.load_state_dict(q2.state_dict())
        opt_a = torch.optim.Adam(actor.parameters(), lr=float(p.get("actor_lr", 3e-4)))
        opt_q = torch.optim.Adam(list(q1.parameters()) + list(q2.parameters()),
                                 lr=float(p.get("critic_lr", 3e-4)))
        gamma = float(p.get("gamma", 0.99))
        mpc_ref = bool(p.get("mpc_reference", True))
        total_steps = int(p.get("env_steps", 100_000))
        buf = _ReplayBuffer()
        report_every = max(1, total_steps // 200)
        tau = 0.005
        log_alpha = torch.tensor(0.0, requires_grad=True)
        opt_alpha = torch.optim.Adam([log_alpha], lr=3e-4)
        target_entropy = -1.0

        if mpc_ref:
            self._mpc_pretrain(actor, opt_a, env, steps=2000, buf=buf, mpc_fn=_mpc)
            obs = env.reset()
            for _ in range(3000):
                act = _mpc(obs)
                nobs, rew, done = env.step(act * env.u_max)
                buf.push(obs, [act], rew, nobs, float(done))
                obs = nobs if not done else env.reset()

        obs = env.reset()
        actor_loss_val = critic_loss_val = 0.0
        for step in range(1, total_steps + 1):
            if self._stop.is_set():
                break
            obs_t = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)
            with torch.no_grad():
                act = float(actor(obs_t).squeeze()) + np.random.normal(0, 0.1)
                act = np.clip(act, -1, 1)
            nobs, rew, done = env.step(act * env.u_max)
            buf.push(obs, [act], rew, nobs, float(done))
            obs = nobs if not done else env.reset()

            if len(buf) < 256:
                continue

            obs_b, act_b, rew_b, nobs_b, done_b = buf.sample(256)
            with torch.no_grad():
                next_act = actor(nobs_b)
                if algo == "TD3":
                    noise = torch.randn_like(next_act).clamp(-0.2, 0.2) * 0.1
                    next_act = (next_act + noise).clamp(-1, 1)
                q_next = torch.min(q1_t(nobs_b, next_act), q2_t(nobs_b, next_act))
                if algo == "SAC":
                    alpha = log_alpha.exp()
                    ent = -0.5 * (1 + np.log(2 * np.pi) + 0.0)  # 近似熵
                    q_next = q_next - alpha * ent
                target_q = rew_b + gamma * (1 - done_b) * q_next

            critic_loss = F.mse_loss(q1(obs_b, act_b), target_q) + \
                          F.mse_loss(q2(obs_b, act_b), target_q)
            opt_q.zero_grad(); critic_loss.backward(); opt_q.step()
            critic_loss_val = float(critic_loss)

            if step % 2 == 0:
                act_new = actor(obs_b)
                actor_loss = -q1(obs_b, act_new).mean()
                if algo == "SAC":
                    alpha = log_alpha.exp()
                    actor_loss = actor_loss + alpha * act_new.pow(2).mean()
                    alpha_loss = -(log_alpha * (act_new.pow(2).mean().detach() + target_entropy))
                    opt_alpha.zero_grad(); alpha_loss.backward(); opt_alpha.step()
                opt_a.zero_grad(); actor_loss.backward(); opt_a.step()
                actor_loss_val = float(actor_loss)

                for p_t, p_s in zip(q1_t.parameters(), q1.parameters()):
                    p_t.data.mul_(1 - tau).add_(p_s.data * tau)
                for p_t, p_s in zip(q2_t.parameters(), q2.parameters()):
                    p_t.data.mul_(1 - tau).add_(p_s.data * tau)

            if step % report_every == 0:
                self.epochDone.emit(step, actor_loss_val, critic_loss_val)

        self.finished.emit(f"{algo} 训练完成，共 {step} 步")

    # ── MPC 模仿预热 ──────────────────────────────────────────
    def _mpc_pretrain(self, actor: _Actor, opt: torch.optim.Optimizer,
                      env: _MotorEnv, steps: int = 2000,
                      buf: "_ReplayBuffer | None" = None, mpc_fn=None) -> None:
        if mpc_fn is None:
            mpc_fn = _mpc_action
        obs = env.reset()
        for i in range(steps):
            expert_act = mpc_fn(obs)
            obs_t = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)
            pred = actor(obs_t).squeeze()
            loss = F.mse_loss(pred, torch.tensor(expert_act, dtype=torch.float32))
            opt.zero_grad(); loss.backward(); opt.step()
            act = float(pred.detach().clamp(-1, 1))
            nobs, rew, done = env.step(act * env.u_max)
            if buf is not None:
                buf.push(obs, [expert_act], rew, nobs, float(done))
            if i % 200 == 0:
                state_str = f"转速={obs[0]*1500:.0f}rpm 目标={obs[1]*1500:.0f}rpm"
                self.mpcInfo.emit(i, len(buf) if buf else 0, state_str, expert_act, rew)
            obs = nobs if not done else env.reset()
