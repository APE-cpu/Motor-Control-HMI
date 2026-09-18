import numpy as np
import pytest
import torch

from training.trainer import Trainer, _classifier_module


MODEL_NAME = "MLP (多层感知机)"
HYPER = {"hidden_size": 8, "num_layers": 2, "dropout": 0.0}
FEATURES = ["电流·时域RMS", "电流·6阶幅值"]


def _dataset():
    rng = np.random.default_rng(42)
    X = rng.normal(size=(40, 2)).astype(np.float32)
    y = (X[:, 0] + 0.25 * X[:, 1] > 0).astype(np.float32)
    return X, y


def _train_direct(trainer: Trainer, X, y, *, transfer=None, epochs=1):
    trainer._model_name = MODEL_NAME
    trainer._feature_names = list(FEATURES)
    trainer._feature_metadata = {"kind": "window_features"}
    trainer._train_torch(
        X, y, epochs=epochs, lr=1e-3, batch_size=8, val_split=0.2,
        optimizer="Adam", loss_name="BCELoss", scheduler="None",
        weight_decay=0.0, hyper=dict(HYPER), normalize=True,
        split_index=32, transfer=transfer or {},
    )


def test_训练检查点保存权重特征顺序和预处理元数据(tmp_path):
    X, y = _dataset()
    trainer = Trainer()
    _train_direct(trainer, X, y)
    path = tmp_path / "source.pt"

    trainer.save_checkpoint(str(path))
    info = Trainer.inspect_checkpoint(str(path))

    assert info["model_name"] == MODEL_NAME
    assert info["input_dimension"] == 2
    assert info["feature_names"] == FEATURES
    assert info["feature_pipeline"]["kind"] == "window_features"
    assert len(info["normalization_mean"]) == 2


def test_仅训练分类头时骨干权重保持不变(tmp_path):
    X, y = _dataset()
    source = Trainer()
    _train_direct(source, X, y)
    path = tmp_path / "source.pt"
    source.save_checkpoint(str(path))
    checkpoint = torch.load(str(path), map_location="cpu", weights_only=True)

    target = Trainer()
    _train_direct(target, X, 1.0 - y, transfer={
        "checkpoint_path": str(path),
        "mode": "head_only",
    })

    head_ids = {id(parameter)
                for parameter in _classifier_module(target._torch_model).parameters()}
    for name, parameter in target._torch_model.named_parameters():
        if id(parameter) not in head_ids:
            assert torch.equal(parameter.detach(), checkpoint["state_dict"][name])
    assert target._transfer_metadata["mode"] == "head_only"


def test_特征顺序不一致时拒绝迁移(tmp_path):
    X, y = _dataset()
    source = Trainer()
    _train_direct(source, X, y)
    path = tmp_path / "source.pt"
    source.save_checkpoint(str(path))

    target = Trainer()
    target._model_name = MODEL_NAME
    target._feature_names = list(reversed(FEATURES))
    target._feature_metadata = {}
    with pytest.raises(ValueError, match="特征名称或顺序"):
        target._train_torch(
            X, y, epochs=1, lr=1e-3, batch_size=8, val_split=0.2,
            optimizer="Adam", loss_name="BCELoss", scheduler="None",
            weight_decay=0.0, hyper=dict(HYPER), normalize=True,
            split_index=32, transfer={
                "checkpoint_path": str(path),
                "mode": "head_only",
            },
        )


def test_分阶段微调会在冻结期后解冻全网(tmp_path):
    X, y = _dataset()
    source = Trainer()
    _train_direct(source, X, y)
    path = tmp_path / "source.pt"
    source.save_checkpoint(str(path))

    messages = []
    target = Trainer()
    target.transferStatus.connect(messages.append)
    _train_direct(target, X, y, epochs=2, transfer={
        "checkpoint_path": str(path),
        "mode": "staged",
        "freeze_epochs": 1,
    })

    assert all(parameter.requires_grad
               for parameter in target._torch_model.parameters())
    assert any("解冻特征提取层" in message for message in messages)
