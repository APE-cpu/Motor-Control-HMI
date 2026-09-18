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

import json
import os
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


class _PreprocessedModel(nn.Module):
    """把训练集拟合的Z-score固化进ONNX，使模型继续接收原始特征。"""

    def __init__(self, model: nn.Module, mean: np.ndarray,
                 std: np.ndarray) -> None:
        super().__init__()
        self.model = model
        self.register_buffer("input_mean", torch.as_tensor(
            mean, dtype=torch.float32).reshape(1, -1))
        self.register_buffer("input_std", torch.as_tensor(
            std, dtype=torch.float32).reshape(1, -1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model((x - self.input_mean) / self.input_std)


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


def _classifier_module(model: nn.Module) -> nn.Module:
    """Return the task head while keeping model-specific backbones reusable."""
    if isinstance(model, _MLP):
        for module in reversed(list(model.net.children())):
            if isinstance(module, nn.Linear):
                return module
    if isinstance(model, (_CNN1D, _LSTM, _Transformer)):
        return model.fc
    raise TypeError(f"模型 {type(model).__name__} 未声明可迁移的分类头")


def _set_transfer_trainability(model: nn.Module, backbone_trainable: bool) -> tuple[int, int]:
    """Freeze/unfreeze the backbone and always leave the task head trainable."""
    for parameter in model.parameters():
        parameter.requires_grad = bool(backbone_trainable)
    head = _classifier_module(model)
    for parameter in head.parameters():
        parameter.requires_grad = True
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    return trainable, total


def _set_training_mode(model: nn.Module, backbone_trainable: bool) -> None:
    """Frozen backbones stay deterministic while the task head trains."""
    if backbone_trainable:
        model.train()
        return
    model.eval()
    _classifier_module(model).train()


def _safe_torch_load(path: str) -> dict:
    """Load state-only checkpoints without allowing pickled code execution."""
    try:
        value = torch.load(path, map_location="cpu", weights_only=True)
    except TypeError:  # torch versions before weights_only
        value = torch.load(path, map_location="cpu")
    if not isinstance(value, dict):
        raise ValueError("训练检查点格式无效：根对象不是字典")
    return value


def _json_safe(value):
    """Keep checkpoint metadata portable and compatible with weights_only."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


# ──────────────────────────────────────────────────────────────
# Trainer
# ──────────────────────────────────────────────────────────────
class Trainer(QObject):
    """在后台线程训练，通过 Qt Signal 推送进度。"""
    epochDone = Signal(int, float, float)   # epoch, train_loss, val_loss
    finished = Signal(str)
    error = Signal(str)
    transferStatus = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._torch_model: Optional[nn.Module] = None
        self._sk_model = None
        self._model_name: str = "MLP (多层感知机)"
        self._in_dim: int = IN_DIM
        self._input_mean = np.zeros(IN_DIM, dtype=np.float32)
        self._input_std = np.ones(IN_DIM, dtype=np.float32)
        self._feature_names: list[str] = []
        self._feature_metadata: dict = {}
        self._hyper: dict = {}
        self._transfer_metadata: dict = {}
        self._stop = threading.Event()

    # ─── 公共接口 ─────────────────────────────────────────────
    def start(self, X: np.ndarray, y: np.ndarray, *,
              model_name: str = "MLP (多层感知机)",
              epochs: int = 50, lr: float = 1e-3,
              batch_size: int = 32, val_split: float = 0.2,
              optimizer: str = "Adam", loss_name: str = "MSELoss",
              scheduler: str = "None", weight_decay: float = 0.0,
              hyper: Optional[dict] = None, normalize: bool = True,
              split_index: Optional[int] = None,
              feature_names: Optional[list[str]] = None,
              feature_metadata: Optional[dict] = None,
              transfer: Optional[dict] = None) -> None:
        self._stop.clear()
        self._model_name = model_name
        self._feature_names = list(feature_names or [])
        self._feature_metadata = dict(feature_metadata or {})
        hyper = hyper or {}
        self._hyper = dict(hyper)
        self._transfer_metadata = {}
        t = threading.Thread(
            target=self._dispatch,
            args=(X, y, epochs, lr, batch_size, val_split,
                  optimizer, loss_name, scheduler, weight_decay, hyper,
                  normalize, split_index, dict(transfer or {})),
            daemon=True,
        )
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def export_onnx(self, path: str) -> None:
        if self._torch_model is None:
            raise RuntimeError("当前模型不可导出 ONNX（仅 PyTorch 模型支持）")
        self._torch_model.eval()
        export_model = _PreprocessedModel(
            self._torch_model, self._input_mean, self._input_std)
        export_model.eval()
        dummy = torch.zeros(1, self._in_dim)
        kwargs = dict(
            input_names=["features"], output_names=["score"],
            dynamic_axes={"features": {0: "batch"}, "score": {0: "batch"}},
            opset_version=11,
        )
        try:
            # torch≥2.9 默认走 dynamo 导出器（需额外的 onnxscript）；
            # 显式 dynamo=False 使用稳定的 TorchScript 导出器，仅依赖 onnx
            torch.onnx.export(export_model, dummy, path,
                              dynamo=False, **kwargs)
        except TypeError:
            # 旧版 torch 无 dynamo 参数
            torch.onnx.export(export_model, dummy, path, **kwargs)
        metadata = {
            "model": self._model_name,
            "input_dimension": self._in_dim,
            "feature_names": self._feature_names,
            "normalization_embedded_in_onnx": True,
            "normalization_mean": self._input_mean.tolist(),
            "normalization_std": self._input_std.tolist(),
            "feature_pipeline": self._feature_metadata,
            "transfer_learning": self._transfer_metadata,
        }
        with open(path + ".features.json", "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2,
                      allow_nan=False)

    def save_checkpoint(self, path: str) -> None:
        """Save a state-only checkpoint that can be used for fine-tuning."""
        if self._torch_model is None:
            raise RuntimeError("当前没有可保存的 PyTorch 模型")
        payload = {
            "format": "motor-host-transfer-checkpoint",
            "format_version": 1,
            "model_name": self._model_name,
            "hyperparameters": _json_safe(self._hyper),
            "input_dimension": int(self._in_dim),
            "feature_names": list(self._feature_names),
            "feature_pipeline": _json_safe(self._feature_metadata),
            "normalization_mean": self._input_mean.tolist(),
            "normalization_std": self._input_std.tolist(),
            "transfer_learning": _json_safe(self._transfer_metadata),
            "state_dict": {
                name: tensor.detach().cpu()
                for name, tensor in self._torch_model.state_dict().items()
            },
        }
        torch.save(payload, path)

    @staticmethod
    def inspect_checkpoint(path: str) -> dict:
        checkpoint = _safe_torch_load(path)
        if checkpoint.get("format") != "motor-host-transfer-checkpoint":
            raise ValueError("不是本上位机导出的迁移学习检查点")
        if int(checkpoint.get("format_version", 0)) != 1:
            raise ValueError("不支持的迁移学习检查点版本")
        if not isinstance(checkpoint.get("state_dict"), dict):
            raise ValueError("训练检查点缺少 state_dict")
        return {
            key: checkpoint.get(key)
            for key in (
                "format_version", "model_name", "hyperparameters",
                "input_dimension", "feature_names", "feature_pipeline",
                "normalization_mean", "normalization_std",
                "transfer_learning",
            )
        }

    # ─── 调度 ────────────────────────────────────────────────
    def _dispatch(self, X, y, epochs, lr, batch_size, val_split,
                  optimizer, loss_name, scheduler, weight_decay, hyper,
                  normalize, split_index, transfer):
        try:
            if self._model_name.startswith(("随机森林", "支持向量机")):
                self._train_sklearn(
                    X, y, val_split, hyper, normalize, split_index)
            else:
                self._train_torch(X, y, epochs, lr, batch_size, val_split,
                                  optimizer, loss_name, scheduler, weight_decay,
                                  hyper, normalize, split_index, transfer)
        except Exception as exc:
            self.error.emit(str(exc))

    def _split_and_scale(self, X, y, val_split, normalize, split_index):
        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.float32)
        n = len(X)
        split = (int(split_index) if split_index is not None
                 else int(n * (1.0 - val_split)))
        if not 1 <= split < n:
            raise ValueError("训练/验证切分后任一集合为空")
        # 时序数据保持先后顺序；滑窗特征传入的split_index已经保证窗口不跨界。
        X_tr, y_tr = X[:split].copy(), y[:split].copy()
        X_val, y_val = X[split:].copy(), y[split:].copy()
        if normalize:
            mean = X_tr.mean(axis=0, dtype=np.float64).astype(np.float32)
            std = X_tr.std(axis=0, dtype=np.float64).astype(np.float32)
            std[std < 1e-8] = 1.0
        else:
            mean = np.zeros(X.shape[1], dtype=np.float32)
            std = np.ones(X.shape[1], dtype=np.float32)
        self._input_mean = mean
        self._input_std = std
        return ((X_tr - mean) / std, y_tr,
                (X_val - mean) / std, y_val)

    # ─── PyTorch 训练 ────────────────────────────────────────
    def _train_torch(self, X, y, epochs, lr, batch_size, val_split,
                     optimizer, loss_name, scheduler, weight_decay, hyper,
                     normalize, split_index, transfer=None):
        X_tr, y_tr, X_val, y_val = self._split_and_scale(
            X, y, val_split, normalize, split_index)

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
        self._hyper = dict(hyper)
        transfer = dict(transfer or {})
        transfer_mode = str(transfer.get("mode", "none"))
        frozen_epochs = max(0, int(transfer.get("freeze_epochs", 0)))
        backbone_trainable = True
        if transfer.get("checkpoint_path"):
            checkpoint_path = os.path.abspath(
                os.fspath(transfer["checkpoint_path"]))
            checkpoint = _safe_torch_load(checkpoint_path)
            self._validate_and_load_transfer_checkpoint(
                checkpoint, checkpoint_path)
            if transfer_mode not in {"head_only", "staged", "full"}:
                raise ValueError(f"未知迁移学习模式：{transfer_mode}")
            backbone_trainable = transfer_mode == "full"
            trainable, total = _set_transfer_trainability(
                self._torch_model, backbone_trainable)
            self._transfer_metadata = {
                "enabled": True,
                "source_checkpoint": checkpoint_path,
                "source_model": checkpoint.get("model_name", ""),
                "mode": transfer_mode,
                "freeze_epochs": frozen_epochs if transfer_mode == "staged" else 0,
                "target_normalization_refit": True,
            }
            mode_text = {
                "head_only": "冻结特征提取层，仅训练分类头",
                "staged": f"先冻结 {frozen_epochs} 轮，再解冻全网微调",
                "full": "加载权重后全网络微调",
            }[transfer_mode]
            self.transferStatus.emit(
                f"迁移权重已加载：{mode_text}；可训练参数 "
                f"{trainable}/{total}；归一化按目标训练集重新拟合")
        else:
            _set_transfer_trainability(self._torch_model, True)
            self._transfer_metadata = {"enabled": False}

        trainable_params = [
            parameter for parameter in self._torch_model.parameters()
            if parameter.requires_grad]
        opt = _build_optimizer(optimizer, trainable_params, lr, weight_decay)
        loss_fn = _build_loss(loss_name)
        sch = _build_scheduler(scheduler, opt, epochs)

        for ep in range(1, epochs + 1):
            if self._stop.is_set():
                self.finished.emit("训练已中止")
                return
            if (transfer_mode == "staged" and not backbone_trainable and
                    ep > frozen_epochs):
                backbone_trainable = True
                trainable, total = _set_transfer_trainability(
                    self._torch_model, True)
                fine_tune_lr = max(float(lr) * 0.1, 1e-8)
                opt = _build_optimizer(
                    optimizer, self._torch_model.parameters(), fine_tune_lr,
                    weight_decay)
                sch = _build_scheduler(
                    scheduler, opt, max(1, epochs - frozen_epochs))
                self.transferStatus.emit(
                    f"第 {ep} 轮解冻特征提取层：全网 {trainable}/{total} "
                    f"参数参与微调，学习率降为 {fine_tune_lr:g}")
            _set_training_mode(self._torch_model, backbone_trainable)
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

    def _validate_and_load_transfer_checkpoint(
            self, checkpoint: dict, path: str) -> None:
        if checkpoint.get("format") != "motor-host-transfer-checkpoint":
            raise ValueError("迁移源不是本上位机导出的 .pt 训练检查点")
        if int(checkpoint.get("format_version", 0)) != 1:
            raise ValueError("迁移源检查点版本不受支持")
        source_model = str(checkpoint.get("model_name", ""))
        if source_model != self._model_name:
            raise ValueError(
                f"模型结构不匹配：检查点为 {source_model}，当前为 "
                f"{self._model_name}")
        source_dim = int(checkpoint.get("input_dimension", -1))
        if source_dim != self._in_dim:
            raise ValueError(
                f"输入维度不匹配：检查点为 {source_dim}，当前为 "
                f"{self._in_dim}")
        source_features = list(checkpoint.get("feature_names") or [])
        if source_features and self._feature_names:
            if source_features != self._feature_names:
                raise ValueError(
                    "特征名称或顺序与迁移源不一致；为防止物理量错位，"
                    "已拒绝加载")
        state_dict = checkpoint.get("state_dict")
        if not isinstance(state_dict, dict):
            raise ValueError("迁移源检查点缺少 state_dict")
        try:
            self._torch_model.load_state_dict(state_dict, strict=True)
        except RuntimeError as exc:
            raise ValueError(
                f"网络超参数与迁移源不一致，无法加载 {path}：{exc}") from exc

    # ─── scikit-learn 训练 ───────────────────────────────────
    def _train_sklearn(self, X, y, val_split, hyper, normalize, split_index):
        if not _SK_OK:
            raise RuntimeError("scikit-learn 未安装，无法训练随机森林/SVM")
        X_tr, y_tr, X_val, y_val = self._split_and_scale(
            X, y, val_split, normalize, split_index)

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
