"""各模型对应的超参数面板（故障分类 6 种 + DRL）。"""
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QSpinBox, QWidget,
)


class ModelPanel(QWidget):
    def values(self) -> dict:
        raise NotImplementedError


class MLPPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.hidden = QSpinBox(); self.hidden.setRange(4, 1024); self.hidden.setValue(64)
        self.layers = QSpinBox(); self.layers.setRange(1, 10); self.layers.setValue(2)
        self.dropout = QDoubleSpinBox(); self.dropout.setRange(0, 0.9); self.dropout.setSingleStep(0.05); self.dropout.setValue(0.0)
        f.addRow("隐藏层维度", self.hidden)
        f.addRow("隐藏层数", self.layers)
        f.addRow("Dropout", self.dropout)

    def values(self) -> dict:
        return {"hidden_size": self.hidden.value(),
                "num_layers": self.layers.value(),
                "dropout": self.dropout.value()}


class CNNPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.channels = QSpinBox(); self.channels.setRange(4, 256); self.channels.setValue(16)
        self.kernel = QSpinBox(); self.kernel.setRange(1, 7); self.kernel.setSingleStep(2); self.kernel.setValue(3)
        self.dropout = QDoubleSpinBox(); self.dropout.setRange(0, 0.9); self.dropout.setSingleStep(0.05); self.dropout.setValue(0.0)
        f.addRow("通道数", self.channels)
        f.addRow("卷积核大小", self.kernel)
        f.addRow("Dropout", self.dropout)

    def values(self) -> dict:
        return {"hidden_size": self.channels.value(),
                "kernel_size": self.kernel.value(),
                "dropout": self.dropout.value()}


class LSTMPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.hidden = QSpinBox(); self.hidden.setRange(4, 512); self.hidden.setValue(32)
        self.layers = QSpinBox(); self.layers.setRange(1, 6); self.layers.setValue(1)
        self.dropout = QDoubleSpinBox(); self.dropout.setRange(0, 0.9); self.dropout.setSingleStep(0.05); self.dropout.setValue(0.0)
        f.addRow("隐藏层维度", self.hidden)
        f.addRow("LSTM 层数", self.layers)
        f.addRow("Dropout", self.dropout)

    def values(self) -> dict:
        return {"hidden_size": self.hidden.value(),
                "num_layers": self.layers.value(),
                "dropout": self.dropout.value()}


class TransformerPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.d_model = QSpinBox(); self.d_model.setRange(8, 512); self.d_model.setValue(32)
        self.nhead = QSpinBox(); self.nhead.setRange(1, 16); self.nhead.setValue(4)
        self.layers = QSpinBox(); self.layers.setRange(1, 12); self.layers.setValue(2)
        self.dropout = QDoubleSpinBox(); self.dropout.setRange(0, 0.9); self.dropout.setSingleStep(0.05); self.dropout.setValue(0.0)
        f.addRow("d_model", self.d_model)
        f.addRow("注意力头数", self.nhead)
        f.addRow("Encoder 层数", self.layers)
        f.addRow("Dropout", self.dropout)

    def values(self) -> dict:
        return {"hidden_size": self.d_model.value(),
                "nhead": self.nhead.value(),
                "num_layers": self.layers.value(),
                "dropout": self.dropout.value()}


class RFPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.n_est = QSpinBox(); self.n_est.setRange(10, 1000); self.n_est.setValue(100)
        self.max_depth = QSpinBox(); self.max_depth.setRange(0, 100); self.max_depth.setValue(10)
        f.addRow("树数量 n_estimators", self.n_est)
        f.addRow("最大深度 (0=不限)", self.max_depth)

    def values(self) -> dict:
        return {"n_estimators": self.n_est.value(),
                "max_depth": self.max_depth.value()}


class SVMPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.C = QDoubleSpinBox(); self.C.setRange(0.001, 1e4); self.C.setDecimals(3); self.C.setValue(1.0)
        self.kernel = QComboBox(); self.kernel.addItems(["rbf", "linear", "poly", "sigmoid"])
        self.gamma = QComboBox(); self.gamma.addItems(["scale", "auto"])
        f.addRow("惩罚系数 C", self.C)
        f.addRow("核函数", self.kernel)
        f.addRow("gamma", self.gamma)

    def values(self) -> dict:
        return {"svm_C": self.C.value(),
                "svm_kernel": self.kernel.currentText(),
                "svm_gamma": self.gamma.currentText()}


class DRLPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.algo = QComboBox(); self.algo.addItems(["PPO", "SAC", "TD3"])
        self.aggregation = QComboBox(); self.aggregation.addItems(["无", "DAgger", "HG-DAgger"])
        self.env_steps = QSpinBox(); self.env_steps.setRange(1000, 10_000_000); self.env_steps.setValue(100_000); self.env_steps.setSingleStep(10_000)
        self.gamma = QDoubleSpinBox(); self.gamma.setRange(0.8, 0.9999); self.gamma.setDecimals(4); self.gamma.setValue(0.99)
        self.actor_lr = QDoubleSpinBox(); self.actor_lr.setRange(1e-6, 1e-2); self.actor_lr.setDecimals(6); self.actor_lr.setValue(3e-4)
        self.critic_lr = QDoubleSpinBox(); self.critic_lr.setRange(1e-6, 1e-2); self.critic_lr.setDecimals(6); self.critic_lr.setValue(3e-4)
        self.hidden = QSpinBox(); self.hidden.setRange(32, 1024); self.hidden.setValue(256)
        self.mpc_ref = QCheckBox("以MPC为参考策略"); self.mpc_ref.setChecked(True)
        f.addRow("算法", self.algo)
        f.addRow("数据集聚合", self.aggregation)
        f.addRow("环境步数", self.env_steps)
        f.addRow("折扣因子 γ", self.gamma)
        f.addRow("Actor 学习率", self.actor_lr)
        f.addRow("Critic 学习率", self.critic_lr)
        f.addRow("隐藏层维度", self.hidden)
        f.addRow("", self.mpc_ref)

    def values(self) -> dict:
        return {"algorithm": self.algo.currentText(),
                "aggregation": self.aggregation.currentText(),
                "env_steps": self.env_steps.value(),
                "gamma": self.gamma.value(),
                "actor_lr": self.actor_lr.value(),
                "critic_lr": self.critic_lr.value(),
                "hidden_size": self.hidden.value(),
                "mpc_reference": self.mpc_ref.isChecked()}


MODEL_PANELS = {
    "MLP (多层感知机)": MLPPanel,
    "1D-CNN (一维卷积)": CNNPanel,
    "LSTM (长短时记忆)": LSTMPanel,
    "Transformer": TransformerPanel,
    "随机森林 (Random Forest)": RFPanel,
    "支持向量机 (SVM)": SVMPanel,
    "深度强化学习(DRL)": DRLPanel,
}
