"""各模型对应的超参数面板（故障分类 6 种 + DRL）。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)


# DRL 算法说明
_DRL_ALGO_DESC = {
    "PPO": "<b>PPO（近端策略优化）</b>：on-policy，用裁剪目标限制每次更新幅度，"
           "训练稳定、超参数好调，工业界最常用的稳健基线；样本效率中等（每批数据用一次即弃）。",
    "SAC": "<b>SAC（软演员-评论家）</b>：off-policy + 最大熵，鼓励探索、样本效率高，"
           "对连续控制（电机电压/电流给定）尤其合适，对超参数较鲁棒。",
    "TD3": "<b>TD3（双延迟确定性策略梯度）</b>：off-policy、确定性策略，双 Critic 抑制"
           "价值过估计、延迟更新 Actor，连续控制精度高；探索靠外加动作噪声。",
}

# 数据集聚合 / 模仿学习说明
_DRL_AGG_DESC = {
    "无": "<b>纯强化学习</b>：不使用专家示范，从零探索。需要大量环境交互，"
          "收敛慢，但上限不受专家约束。",
    "DAgger": "<b>DAgger（数据集聚合，Dataset Aggregation）</b>：模仿学习的经典改进。"
              "让<u>学生策略自己跑</u>，把它遇到的状态交给专家（MPC）标注正确动作，"
              "聚合进数据集反复重训——专治模仿学习的<b>分布漂移</b>"
              "（学生一旦偏离专家轨迹就没见过、越错越离谱）。",
    "HG-DAgger": "<b>HG-DAgger（人在回路门控 DAgger）</b>：在 DAgger 基础上，由安全"
                 "监督/人工决定何时让专家接管（门控），只在关键/危险状态采集专家动作，"
                 "更安全、样本更省。",
}


def _rich_label() -> QLabel:
    lab = QLabel()
    lab.setTextFormat(Qt.RichText)
    lab.setWordWrap(True)
    lab.setStyleSheet("QLabel { color: #c7d3e0; font-size: 12px; }")
    lab.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    return lab


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


_DRL_INTRO = (
    "<b>目标：让神经网络学会 MPC 的控制策略</b>（模仿学习），用轻量网络替代"
    "在线求解量大的 MPC，便于部署到单片机。<br>"
    "<b>热启动（warm start）</b>：勾选下方「以MPC为参考策略」后，先用 MPC 专家"
    "生成示范数据做行为克隆，再用强化学习微调——避免从随机策略冷启动的漫长探索。")


class DRLPanel(ModelPanel):
    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.algo = QComboBox(); self.algo.addItems(["PPO", "SAC", "TD3"])
        self.algo.setToolTip("强化学习算法：PPO 稳健通用；SAC 样本效率高、适合连续控制；"
                             "TD3 确定性、精度高")
        self.aggregation = QComboBox(); self.aggregation.addItems(["无", "DAgger", "HG-DAgger"])
        self.aggregation.setToolTip("模仿学习的数据采集方式：无=纯 RL；"
                                    "DAgger=聚合专家纠正、治分布漂移；HG-DAgger=人在回路门控")
        self.env_steps = QSpinBox(); self.env_steps.setRange(1000, 10_000_000); self.env_steps.setValue(100_000); self.env_steps.setSingleStep(10_000)
        self.env_steps.setToolTip("与仿真环境交互的总步数，越多越充分但越慢")
        self.gamma = QDoubleSpinBox(); self.gamma.setRange(0.8, 0.9999); self.gamma.setDecimals(4); self.gamma.setValue(0.99)
        self.gamma.setToolTip("折扣因子：越接近 1 越看重长期回报；调速这类任务常用 0.99")
        self.actor_lr = QDoubleSpinBox(); self.actor_lr.setRange(1e-6, 1e-2); self.actor_lr.setDecimals(6); self.actor_lr.setValue(3e-4)
        self.actor_lr.setToolTip("策略网络（Actor，输出动作）学习率，典型 3e-4")
        self.critic_lr = QDoubleSpinBox(); self.critic_lr.setRange(1e-6, 1e-2); self.critic_lr.setDecimals(6); self.critic_lr.setValue(3e-4)
        self.critic_lr.setToolTip("价值网络（Critic，评估动作好坏）学习率")
        self.hidden = QSpinBox(); self.hidden.setRange(32, 1024); self.hidden.setValue(256)
        self.hidden.setToolTip("Actor/Critic 隐藏层维度，越大表达力越强但越慢")
        self.mpc_ref = QCheckBox("以MPC为参考策略（专家热启动）"); self.mpc_ref.setChecked(True)
        self.mpc_ref.setToolTip("用现有 MPC 控制器作为专家生成示范，行为克隆热启动后再 RL 微调")
        f.addRow("算法", self.algo)
        f.addRow("数据集聚合", self.aggregation)
        f.addRow("环境步数", self.env_steps)
        f.addRow("折扣因子 γ", self.gamma)
        f.addRow("Actor 学习率", self.actor_lr)
        f.addRow("Critic 学习率", self.critic_lr)
        f.addRow("隐藏层维度", self.hidden)
        f.addRow("", self.mpc_ref)
        root.addLayout(f)

        # 说明区：随算法/聚合方式实时更新
        self._desc = _rich_label()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(150)
        scroll.setWidget(self._desc)
        root.addWidget(scroll)
        self.algo.currentIndexChanged.connect(self._update_desc)
        self.aggregation.currentIndexChanged.connect(self._update_desc)
        self._update_desc()

    def _update_desc(self) -> None:
        self._desc.setText(
            _DRL_INTRO + "<hr>"
            + _DRL_ALGO_DESC[self.algo.currentText()] + "<br><br>"
            + _DRL_AGG_DESC[self.aggregation.currentText()])

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
