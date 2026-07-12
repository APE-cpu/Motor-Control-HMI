"""用数字孪生生成标注数据，训练故障检测模型并导出 ONNX（替换占位模型）。

要点：
- 特征顺序与 edge_ai / 遥测一致：
  [speed_actual, speed_target, current_actual, current_target,
   torque_actual, torque_target, angle_actual, temperature]
- 标签仅由“模型看得见的”信号（温度、电流）判定，不用 bus_state
  （bus_state 不是模型输入，否则无法学习）
- 缩放固化进模型：ONNX 接受原始遥测值，edge_ai 直接喂原始特征即可

运行：python -m training.gen_anomaly_model
"""
from __future__ import annotations

import os
import random

import numpy as np
import torch
import torch.nn as nn

from communications.motor_sim import MotorSim, PMSMParams

# 与训练页 _auto_label 一致的可学习判据（仅用温度/电流，模型可见）
TEMP_FAULT, TEMP_WARN = 85.0, 65.0
CUR_FAULT, CUR_WARN = 7.0, 5.0
# 各特征的物理量级，用于固化缩放（原始值 / SCALE ≈ O(1)）
SCALE = [3000.0, 3000.0, 10.0, 10.0, 5.0, 5.0, 360.0, 100.0]


# “已达速/稳态”判定：转速差同时小于固定值和目标的百分比才算达速。
# 只有达速后的大电流才是过载故障；加速中(转速差大)或超调(实际≥目标)
# 的大电流都是正常启动动态，不应误判。
SETTLED_RPM_ABS = 120.0     # 转速差绝对下限
SETTLED_RPM_PCT = 0.08      # 转速差占目标的比例下限


def _settled(spd_act: float, spd_tgt: float) -> bool:
    """电机是否已稳定在目标转速（可判过载的前提）。

    实际转速≥目标（超调）一定不是稳态过载——超调是启动动态；
    否则要求转速差同时小于绝对阈值和目标百分比阈值才算达速。
    """
    tgt, act = abs(spd_tgt), abs(spd_act)
    if act >= tgt:                       # 超调：启动动态，非稳态过载
        return False
    gap = tgt - act
    return gap <= max(SETTLED_RPM_ABS, SETTLED_RPM_PCT * tgt)


def _label(cur: float, temp: float, spd_act: float = 0.0,
           spd_tgt: float = 0.0) -> float:
    """故障判据（仅用模型可见信号：温度、电流、转速差）：

    过温任何时候都算故障；过流只有在“已达速稳态”时才算过载故障——
    加速中或超调阶段的大电流是正常启动限流，不应误判。
    """
    settled = _settled(spd_act, spd_tgt)
    if temp >= TEMP_FAULT:
        return 1.0
    if settled and abs(cur) >= CUR_FAULT:
        return 1.0
    if temp >= TEMP_WARN:
        return 0.5
    if settled and abs(cur) >= CUR_WARN:
        return 0.5
    return 0.0


def _feat(sim: MotorSim, temp: float, noise: bool = True) -> list:
    n = (lambda a: random.uniform(-a, a)) if noise else (lambda a: 0.0)
    return [
        sim.speed_rpm + n(2.0),          # speed_actual
        sim.speed_ref_rpm,               # speed_target
        sim.i_q + n(0.05),               # current_actual
        sim.iq_ref,                      # current_target
        sim.torque + n(0.02),            # torque_actual
        sim.torque_ref,                  # torque_target
        sim.angle_deg,                   # angle_actual
        temp + n(0.5),                   # temperature
    ]


def generate(seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """对每个 (转速, 负载) 跑完整"从零启动→加速→稳态"轨迹，逐帧采样。

    这样电流与转速差在每一帧都是物理上真实配对的：启动阶段(转速差大)
    自然伴随大电流、稳态(转速差≈0)电流回落。模型据此学到
    "转速差大+大电流=正常启动"、"转速差≈0+大电流=过载故障"的联合判据，
    而不是简单的"大电流=故障"。温度作为独立维度叠加，覆盖过温故障。
    """
    random.seed(seed)
    speeds = list(range(300, 3300, 300))
    loads = [0.0, 0.1, 0.2, 0.3, 0.45, 0.6]
    # 逐帧轨迹自带温度基线，这里额外抽几档温度让每帧覆盖冷机~过温
    temps = [25, 40, 55, 68, 78, 88, 100, 110]
    X, y = [], []
    sim = MotorSim(PMSMParams())
    for spd in speeds:
        for load in loads:
            sim.reset()
            sim.start(float(spd))
            sim.set_load(load)
            # 完整轨迹 4s @ 0.1s = 40 帧：前段加速大电流、后段稳态
            for k in range(40):
                sim.step(0.1)
                # 每帧在若干温度档上各生成一条样本（温度不影响电/机状态，
                # 是独立叠加的热维度），让"启动大电流×各温度"组合都被覆盖
                for temp in temps:
                    row = _feat(sim, float(temp))
                    X.append(row)
                    y.append(_label(row[2], row[7], row[0], row[1]))
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)


class ScaledMLP(nn.Module):
    """内置固定缩放层：接受原始遥测特征，内部归一化后过 MLP。"""

    def __init__(self, scale: list, hidden: int = 32) -> None:
        super().__init__()
        self.register_buffer("scale", torch.tensor(scale, dtype=torch.float32))
        self.net = nn.Sequential(
            nn.Linear(8, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1), nn.Sigmoid(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x / self.scale)


def train(X: np.ndarray, y: np.ndarray, epochs: int = 300) -> ScaledMLP:
    torch.manual_seed(0)
    n = len(X)
    idx = np.random.RandomState(0).permutation(n)
    split = int(n * 0.85)
    tr, va = idx[:split], idx[split:]
    Xt = torch.tensor(X[tr]); yt = torch.tensor(y[tr]).unsqueeze(1)
    Xv = torch.tensor(X[va]); yv = torch.tensor(y[va]).unsqueeze(1)

    model = ScaledMLP(SCALE)
    opt = torch.optim.Adam(model.net.parameters(), lr=2e-3)
    loss_fn = nn.MSELoss()
    for ep in range(1, epochs + 1):
        model.train()
        perm = torch.randperm(len(Xt))
        for i in range(0, len(Xt), 64):
            b = perm[i:i + 64]
            opt.zero_grad()
            loss_fn(model(Xt[b]), yt[b]).backward()
            opt.step()
        if ep % 50 == 0:
            model.eval()
            with torch.no_grad():
                vl = float(loss_fn(model(Xv), yv))
            print(f"  epoch {ep:3d}  val MSE={vl:.4f}")
    return model


def evaluate(model: ScaledMLP, X: np.ndarray, y: np.ndarray) -> None:
    model.eval()
    with torch.no_grad():
        pred = model(torch.tensor(X)).numpy().flatten()
    # 三分类准确率（就近归到 0/0.5/1）
    grid = np.array([0.0, 0.5, 1.0])
    pcls = grid[np.abs(pred[:, None] - grid[None, :]).argmin(axis=1)]
    acc = float((pcls == y).mean())
    print(f"  三档就近准确率: {acc*100:.1f}%")
    for lv, name in [(0.0, "正常"), (0.5, "警告"), (1.0, "故障")]:
        m = y == lv
        if m.any():
            print(f"    {name}(真值{lv}) 预测均值 {pred[m].mean():.3f} "
                  f"(n={int(m.sum())})")


def evaluate_trajectory(model: ScaledMLP) -> None:
    """跑真实启动轨迹逐帧检查——这才是运行时 edge_ai 实际看到的输入。

    冷机正常启动到 1500rpm：加速段大电流应判正常，稳态低电流应判正常，
    全程不应出现"异常"。这是复现用户误报场景的直接检验。
    """
    model.eval()
    sim = MotorSim(PMSMParams())
    sim.reset()
    sim.start(1500.0)
    sim.set_load(0.15)          # 轻载正常启动
    print("  冷机正常启动到 1500rpm（温度 25°C），逐帧判定：")
    worst = 0.0
    for k in range(30):
        sim.step(0.1)
        row = _feat(sim, 25.0, noise=False)
        with torch.no_grad():
            s = float(model(torch.tensor([row], dtype=torch.float32)))
        worst = max(worst, s)
        if k % 3 == 0 or s >= 0.6:
            tag = "正常" if s < 0.3 else ("警告" if s < 0.6 else "★异常")
            print(f"    t={k*0.1:.1f}s  转速={row[0]:6.0f}  "
                  f"电流={row[2]:5.2f}A  分数={s:.3f}  {tag}")
    print(f"  → 启动全程最高分数 {worst:.3f}"
          f"（{'✓ 无误报' if worst < 0.6 else '✗ 仍有误报'}）")


def export(model: ScaledMLP, path: str) -> None:
    model.eval()
    dummy = torch.zeros(1, 8)
    torch.onnx.export(
        model, dummy, path, dynamo=False,
        input_names=["features"], output_names=["score"],
        dynamic_axes={"features": {0: "batch"}, "score": {0: "batch"}},
        opset_version=11,
    )


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "motor_anomaly.onnx")
    print("生成数据…")
    X, y = generate()
    print(f"  样本 {len(X)} 条；正常 {(y==0).sum()} 警告 {(y==0.5).sum()} 故障 {(y==1).sum()}")
    print("训练…")
    model = train(X, y)
    print("评估…")
    evaluate(model, X, y)
    print(f"导出 → {out}")
    export(model, out)
    print("完成")


if __name__ == "__main__":
    main()
