"""电机详情对话框：铭牌额定值 + 自测实际值 + 用户描述，持久化到 JSON。

实测值可一键从数字孪生读取（参数辨识页「应用到数字孪生」后的最新参数）。
"""
import json

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout,
)

from logs.operation_logger import logger
from runtime_paths import writable_path

_INFO_PATH = writable_path("config", "motor_info.json")

# (键, 标签, 单位, 小数位, 最大值)
_RATED_FIELDS = [
    ("voltage_V",  "额定电压",  "V",    1, 10000.0),
    ("current_A",  "额定电流",  "A",    2, 10000.0),
    ("power_W",    "额定功率",  "W",    1, 1e6),
    ("speed_rpm",  "额定转速",  "rpm",  0, 100000.0),
    ("torque_Nm",  "额定转矩",  "N·m",  3, 10000.0),
    ("psi_f_Wb",   "磁链 ψf",   "Wb",   4, 10.0),
]
_MEASURED_FIELDS = [
    ("Rs_ohm",   "定子电阻 Rs",   "Ω",        4, 1000.0),
    ("Ld_mH",    "d 轴电感 Ld",   "mH",       4, 1000.0),
    ("Lq_mH",    "q 轴电感 Lq",   "mH",       4, 1000.0),
    ("psi_f_Wb", "磁链 ψf",       "Wb",       4, 10.0),
    ("B",        "粘滞摩擦 B",    "N·m·s/rad", 6, 10.0),
    ("Tc_Nm",    "库仑摩擦 Tc",   "N·m",      4, 100.0),
    ("J_kgm2",   "转动惯量 J",    "kg·m²",    6, 100.0),
]


def load_motor_info() -> dict:
    try:
        with open(_INFO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class MotorInfoDialog(QDialog):
    def __init__(self, comm, parent=None) -> None:
        super().__init__(parent)
        self._comm = comm
        self.setWindowTitle("电机详情")
        self.resize(680, 560)

        root = QVBoxLayout(self)

        head = QFormLayout()
        self._model = QLineEdit()
        self._pole_pairs = QSpinBox()
        self._pole_pairs.setRange(1, 64)
        head.addRow("电机型号", self._model)
        head.addRow("极对数", self._pole_pairs)
        root.addLayout(head)

        body = QHBoxLayout()
        self._rated_spins = {}
        self._measured_spins = {}
        body.addWidget(self._build_group("额定值（出厂铭牌）", _RATED_FIELDS,
                                         self._rated_spins), 1)

        meas_box = self._build_group("实际值（自测）", _MEASURED_FIELDS,
                                     self._measured_spins)
        btn_twin = QPushButton("从数字孪生读取")
        btn_twin.setToolTip("读取当前数字孪生参数（参数辨识页应用后的最新值）")
        btn_twin.clicked.connect(self._on_read_twin)
        meas_box.layout().addRow("", btn_twin)
        body.addWidget(meas_box, 1)
        root.addLayout(body)

        desc_box = QGroupBox("描述（安装位置、负载情况、维护记录等）")
        dv = QVBoxLayout(desc_box)
        self._desc = QPlainTextEdit()
        self._desc.setPlaceholderText("自由填写，随「保存」一起持久化…")
        dv.addWidget(self._desc)
        root.addWidget(desc_box, 1)

        self._path_label = QLabel(f"保存位置：{_INFO_PATH}")
        self._path_label.setStyleSheet("color: #8fa3b8;")
        root.addWidget(self._path_label)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Save).setText("保存")
        btns.button(QDialogButtonBox.Close).setText("关闭")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self._load()

    @staticmethod
    def _build_group(title: str, fields: list, spins: dict) -> QGroupBox:
        box = QGroupBox(title)
        f = QFormLayout(box)
        for key, label, unit, decimals, mx in fields:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, mx)
            sp.setDecimals(decimals)
            sp.setSuffix(f" {unit}")
            spins[key] = sp
            f.addRow(label, sp)
        return box

    # ─── 读写 ───────────────────────────────────────────────
    def _load(self) -> None:
        info = load_motor_info()
        self._model.setText(info.get("model", "M-001"))
        self._pole_pairs.setValue(int(info.get("pole_pairs", 4)))
        for key, sp in self._rated_spins.items():
            sp.setValue(float(info.get("rated", {}).get(key, 0.0)))
        for key, sp in self._measured_spins.items():
            sp.setValue(float(info.get("measured", {}).get(key, 0.0)))
        self._desc.setPlainText(info.get("description", ""))

    def _on_save(self) -> None:
        info = {
            "model": self._model.text().strip(),
            "pole_pairs": self._pole_pairs.value(),
            "rated": {k: sp.value() for k, sp in self._rated_spins.items()},
            "measured": {k: sp.value() for k, sp in self._measured_spins.items()},
            "description": self._desc.toPlainText(),
        }
        try:
            with open(_INFO_PATH, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        logger.log("保存电机详情", f"型号={info['model']}")
        QMessageBox.information(self, "已保存", f"电机详情已保存到：\n{_INFO_PATH}")
        self.accept()

    def _on_read_twin(self) -> None:
        p = self._comm.motor_sim_params()
        self._measured_spins["Rs_ohm"].setValue(p.Rs)
        self._measured_spins["Ld_mH"].setValue(p.Ld * 1e3)
        self._measured_spins["Lq_mH"].setValue(p.Lq * 1e3)
        self._measured_spins["psi_f_Wb"].setValue(p.psi_f)
        self._measured_spins["B"].setValue(p.B)
        self._measured_spins["Tc_Nm"].setValue(p.T_coulomb)
        self._measured_spins["J_kgm2"].setValue(p.J)
        self._pole_pairs.setValue(p.pole_pairs)
