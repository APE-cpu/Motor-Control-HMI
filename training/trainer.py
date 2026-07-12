"""模型训练器：支持多种模型与超参数。

支持模型：
- MLP                 多层感知机
- 1D-CNN              一维卷积，将 8 维特征视为长度 8 的序列
- LSTM                以单步序列输入演示循环网络
- Transformer         单步 Encoder
- RandomForest / SVM  非深度模型，使用 scikit-learn（若可用）

任务定义：8 维电机遥测特征 → 1 维异常分数（0~1）。
深度模型通过 Qt Signal 在后台线程推送 epoch 损失，可中止与导出 ONNX。
非深度模型一次性训练，完成后推送一个等价的 epochDone 信号。
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from PySide6.QtCore import QObject, Signal


try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.svm import SVR
    _SK_OK = True
except ImportError:
    _SK_OK = False


IN_DIM = 8   # 默认全特征维度；实际训练维度由数据列数决定


# ──────────────────────────────────────────────────────────────
# 模型定义
# ──────────────────────────────────────────────────────────────
class _MLP(nn.Module):
    def __init__(self, hidden: int = 64, num_layers: int = 2, dropout: float = 0.0,
                 in_dim: int = IN_DIM) -> None:
        super().__init__()
        layers: list[nn.Module] = []
        last = in_dim
        for _ in range(num_layers):
            layers += [nn.Linear(last, hidden), nn.ReLU()]
            if dropout > 0:
                layers.append(nn.Dropout(dropout))
            last = hidden
        layers += [nn.Linear(last, 1), nn.Sigmoid()]
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class _CNN1D(nn.Module):
    def __init__(self, channels: int = 16, kernel: int = 3, dropout: float = 0.0,
                 in_dim: int = IN_DIM) -> None:
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv1d(1, channels, kernel_size=kernel, padding=kernel // 2),
            nn.ReLU(),
            nn.Conv1d(channels, channels, kernel_size=kernel, padding=kernel // 2),
            nn.ReLU(),
        )
        self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()
        self.fc = nn.Sequential(nn.Linear(channels * in_dim, 1), nn.Sigmoid())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.conv(x.unsqueeze(1))            # [B,1,8] → [B,C,8]
        z = self.dropout(z).flatten(1)
        return self.fc(z)


class _LSTM(nn.Module):
    def __init__(self, hidden: int = 32, num_layers: int = 1, dropout: float = 0.0) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=1, hidden_size=hidden, num_layers=num_layers,
            batch_first=True, dropout=dropout if num_layers > 1 else 0.0,
        )
        self.fc = nn.Sequential(nn.Linear(hidden, 1), nn.Sigmoid())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 将 8 维特征当作长度 8 的序列
        out, _ = self.lstm(x.unsqueeze(-1))      # [B,8,H]
        return self.fc(out[:, -1, :])


class _Transformer(nn.Module):
    def __init__(self, d_model: int = 32, nhead: int = 4, num_layers: int = 2,
                 dropout: float = 0.0) -> None:
        super().__init__()
        self.embed = nn.Linear(1, d_model)
        enc_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=d_model * 2,
            dropout=dropout, batch_first=True,
        )
        self.enc = nn.TransformerEncoder(enc_layer, num_layers=num_layers)
        self.fc = nn.Sequential(nn.Linear(d_model, 1), nn.Sigmoid())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.embed(x.unsqueeze(-1))          # [B,8,1] → [B,8,D]
        z = self.enc(z)                          # [B,8,D]
        return self.fc(z.mean(dim=1))


def _build_torch_model(name: str, hp: dict, in_dim: int = IN_DIM) -> nn.Module:
    if name.startswith("MLP"):
        return _MLP(
            hidden=int(hp.get("hidden_size", 64)),
            num_layers=int(hp.get("num_layers", 2)),
            dropout=float(hp.get("dropout", 0.0)),
            in_dim=in_dim,
        )
    if name.startswith("1D-CNN"):
        return _CNN1D(
            channels=int(hp.get("hidden_size", 16)),
            kernel=int(hp.get("kernel_size", 3)),
            dropout=float(hp.get("dropout", 0.0)),
            in_dim=in_dim,
        )
    if name.startswith("LSTM"):
        return _LSTM(
            hidden=int(hp.get("hidden_size", 32)),
            num_layers=int(hp.get("num_layers", 1)),
            dropout=float(hp.get("dropout", 0.0)),
        )
    if name.startswith("Transformer"):
        d_model = int(hp.get("hidden_size", 32))
        nhead = max(1, int(hp.get("nhead", 4)))
        if d_model % nhead != 0:
            d_model = (d_model // nhead) * nhead or nhead
        return _Transformer(
            d_model=d_model, nhead=nhead,
            num_layers=int(hp.get("num_layers", 2)),
            dropout=float(hp.get("dropout", 0.0)),
        )
    raise ValueError(f"未知 Torch 模型：{name}")


def _build_optimizer(name: str, params, lr: float, weight_decay: float):
    name = name.lower()
    if name == "adam":
        return torch.optim.Adam(params, lr=lr, weight_decay=weight_decay)
    if name == "adamw":
        return torch.optim.AdamW(params, lr=lr, weight_decay=weight_decay)
    if name == "sgd":
        return torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=weight_decay)
    if name == "rmsprop":
        return torch.optim.RMSprop(params, lr=lr, weight_decay=weight_decay)
    raise ValueError(f"未知优化器：{name}")


def _build_loss(name: str):
    return {
        "MSELoss": nn.MSELoss(),
        "L1Loss": nn.L1Loss(),
        "BCELoss": nn.BCELoss(),
        "CrossEntropy": nn.BCELoss(),   # 标签为 0/0.5/1 的连续值，仍用 BCE 兼容
    }.get(name, nn.MSELoss())


def _build_scheduler(name: str, opt, epochs: int):
    if name == "StepLR":
        return torch.optim.lr_scheduler.StepLR(opt, step_size=max(1, epochs // 5), gamma=0.5)
    if name == "CosineAnnealingLR":
        return torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    if name == "ReduceLROnPlateau":
        return torch.optim.lr_scheduler.ReduceLROnPlateau(opt, factor=0.5, patience=5)
    return None


# ──────────────────────────────────────────────────────────────
# Trainer
# ──────────────────────────────────────────────────────────────
class Trainer(QObject):
    """在后台线程训练，通过 Qt Signal 推送进度。"""
    epochDone = Signal(int, float, float)   # epoch, train_loss, val_loss
    finished = Signal(str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._torch_model: Optional[nn.Module] = None
        self._sk_model = None
        self._model_name: str = "MLP (多层感知机)"
        self._in_dim: int = IN_DIM
        self._stop = threading.Event()

    # ─── 公共接口 ─────────────────────────────────────────────
    def start(self, X: np.ndarray, y: np.ndarray, *,
              model_name: str = "MLP (多层感知机)",
              epochs: int = 50, lr: float = 1e-3,
              batch_size: int = 32, val_split: float = 0.2,
              optimizer: str = "Adam", loss_name: str = "MSELoss",
              scheduler: str = "None", weight_decay: float = 0.0,
              hyper: Optional[dict] = None) -> None:
        self._stop.clear()
        self._model_name = model_name
        hyper = hyper or {}
        t = threading.Thread(
            target=self._dispatch,
            args=(X, y, epochs, lr, batch_size, val_split,
                  optimizer, loss_name, scheduler, weight_decay, hyper),
            daemon=True,
        )
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def export_onnx(self, path: str) -> None:
        if self._torch_model is None:
            raise RuntimeError("当前模型不可导出 ONNX（仅 PyTorch 模型支持）")
        self._torch_model.eval()
        dummy = torch.zeros(1, self._in_dim)
        kwargs = dict(
            input_names=["features"], output_names=["score"],
            dynamic_axes={"features": {0: "batch"}, "score": {0: "batch"}},
            opset_version=11,
        )
        try:
            # torch≥2.9 默认走 dynamo 导出器（需额外的 onnxscript）；
            # 显式 dynamo=False 使用稳定的 TorchScript 导出器，仅依赖 onnx
            torch.onnx.export(self._torch_model, dummy, path,
                              dynamo=False, **kwargs)
        except TypeError:
            # 旧版 torch 无 dynamo 参数
            torch.onnx.export(self._torch_model, dummy, path, **kwargs)

    # ─── 调度 ────────────────────────────────────────────────
    def _dispatch(self, X, y, epochs, lr, batch_size, val_split,
                  optimizer, loss_name, scheduler, weight_decay, hyper):
        try:
            if self._model_name.startswith(("随机森林", "支持向量机")):
                self._train_sklearn(X, y, val_split, hyper)
            else:
                self._train_torch(X, y, epochs, lr, batch_size, val_split,
                                  optimizer, loss_name, scheduler, weight_decay, hyper)
        except Exception as exc:
            self.error.emit(str(exc))

    # ─── PyTorch 训练 ────────────────────────────────────────
    def _train_torch(self, X, y, epochs, lr, batch_size, val_split,
                     optimizer, loss_name, scheduler, weight_decay, hyper):
        n = len(X)
        split = int(n * (1 - val_split))
        idx = np.random.permutation(n)
        X_tr, y_tr = X[idx[:split]], y[idx[:split]]
        X_val, y_val = X[idx[split:]], y[idx[split:]]

        X_tr_t = torch.tensor(X_tr, dtype=torch.float32)
        y_tr_t = torch.tensor(y_tr, dtype=torch.float32).unsqueeze(1)
        X_val_t = torch.tensor(X_val, dtype=torch.float32)
        y_val_t = torch.tensor(y_val, dtype=torch.float32).unsqueeze(1)

        ds = TensorDataset(X_tr_t, y_tr_t)
        loader = DataLoader(ds, batch_size=batch_size, shuffle=True)

        self._sk_model = None
        self._in_dim = int(X.shape[1])   # 输入维度跟随所选特征数
        self._torch_model = _build_torch_model(self._model_name, hyper,
                                               in_dim=self._in_dim)
        opt = _build_optimizer(optimizer, self._torch_model.parameters(), lr, weight_decay)
        loss_fn = _build_loss(loss_name)
        sch = _build_scheduler(scheduler, opt, epochs)

        for ep in range(1, epochs + 1):
            if self._stop.is_set():
                self.finished.emit("训练已中止")
                return
            self._torch_model.train()
            for xb, yb in loader:
                opt.zero_grad()
                loss_fn(self._torch_model(xb), yb).backward()
                opt.step()

            self._torch_model.eval()
            with torch.no_grad():
                tr_loss = float(loss_fn(self._torch_model(X_tr_t), y_tr_t))
                val_loss = float(loss_fn(self._torch_model(X_val_t), y_val_t))
            if sch is not None:
                if isinstance(sch, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    sch.step(val_loss)
                else:
                    sch.step()
            self.epochDone.emit(ep, tr_loss, val_loss)

        self.finished.emit(f"训练完成（{self._model_name}），共 {epochs} 轮")

    # ─── scikit-learn 训练 ───────────────────────────────────
    def _train_sklearn(self, X, y, val_split, hyper):
        if not _SK_OK:
            raise RuntimeError("scikit-learn 未安装，无法训练随机森林/SVM")
        n = len(X)
        split = int(n * (1 - val_split))
        idx = np.random.permutation(n)
        X_tr, y_tr = X[idx[:split]], y[idx[:split]]
        X_val, y_val = X[idx[split:]], y[idx[split:]]

        if self._model_name.startswith("随机森林"):
            model = RandomForestRegressor(
                n_estimators=int(hyper.get("n_estimators", 100)),
                max_depth=int(hyper.get("max_depth", 10)) or None,
                n_jobs=-1, random_state=42,
            )
        else:
            model = SVR(
                C=float(hyper.get("svm_C", 1.0)),
                gamma=str(hyper.get("svm_gamma", "scale")),
                kernel=str(hyper.get("svm_kernel", "rbf")),
            )
        model.fit(X_tr, y_tr)
        self._sk_model = model
        self._torch_model = None

        tr_loss = float(np.mean((model.predict(X_tr) - y_tr) ** 2))
        val_loss = float(np.mean((model.predict(X_val) - y_val) ** 2)) if len(X_val) else tr_loss
        self.epochDone.emit(1, tr_loss, val_loss)
        self.finished.emit(f"训练完成（{self._model_name}） train MSE={tr_loss:.5f} val MSE={val_loss:.5f}")
