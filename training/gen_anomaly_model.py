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


def _label(cur: float, temp: float) -> float:
    if temp >= TEMP_FAULT or abs(cur) >= CUR_FAULT:
        return 1.0
    if temp >= TEMP_WARN or abs(cur) >= CUR_WARN:
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
    """扫 转速×负载×温度 网格，每工况取多帧含噪样本。"""
    random.seed(seed)
    speeds = list(range(300, 3300, 300))
    loads = [0.0, 0.1, 0.2, 0.3, 0.45, 0.6]
    temps = [30, 45, 58, 68, 78, 88, 100, 110]
    X, y = [], []
    sim = MotorSim(PMSMParams())
    for spd in speeds:
        for load in loads:
            sim.reset()
            sim.start(float(spd))
            sim.set_load(load)
            sim.step(3.0)                # 电气/机械稳态
            cur = sim.i_q
            for temp in temps:
                for _ in range(4):       # 每点几帧含噪
                    row = _feat(sim, float(temp))
                    X.append(row)
                    y.append(_label(row[2], row[7]))
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
