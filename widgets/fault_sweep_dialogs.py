"""训练页数据采集辅助对话框：自定义故障判据、扫频测试点配置。"""
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLabel, QSpinBox, QVBoxLayout,
)


def _frange(start: float, stop: float, step: float) -> list:
    """含端点的等步进序列；step<=0 时只取起点。"""
    if step <= 0 or stop <= start:
        return [start]
    vals, v = [], start
    while v <= stop + 1e-9:
        vals.append(round(v, 6))
        v += step
    return vals


class FaultCriteriaDialog(QDialog):
    """自定义自动标注的故障/告警判据。"""

    def __init__(self, cfg: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("故障判据设置")
        self.resize(420, 340)
        root = QVBoxLayout(self)
        info = QLabel("自动标注时，每帧按下列判据判定 正常/警告/故障。"
                      "电流阈值填 0 表示禁用该项。")
        info.setWordWrap(True)
        info.setStyleSheet("color: #8fa3b8;")
        root.addWidget(info)

        f = QFormLayout()
        self._temp_fault = self._dsp(0, 300, cfg["temp_fault"], " °C")
        self._temp_warn = self._dsp(0, 300, cfg["temp_warn"], " °C")
        self._cur_fault = self._dsp(0, 1000, cfg["cur_fault"], " A")
        self._cur_warn = self._dsp(0, 1000, cfg["cur_warn"], " A")
        self._sensor_q = self._dsp(0, 1, cfg["sensor_q_warn"], "", 2, 0.05)
        self._use_bus = QCheckBox("采用母线状态判据（过压→故障，欠压/斩波→告警）")
        self._use_bus.setChecked(cfg.get("use_bus", True))
        f.addRow("过温故障阈值", self._temp_fault)
        f.addRow("温度告警阈值", self._temp_warn)
        f.addRow("过流故障阈值", self._cur_fault)
        f.addRow("过流告警阈值", self._cur_warn)
        f.addRow("传感器质量告警下限", self._sensor_q)
        f.addRow(self._use_bus)
        root.addLayout(f)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    @staticmethod
    def _dsp(mn, mx, val, suffix="", decimals=1, step=1.0):
        sp = QDoubleSpinBox()
        sp.setRange(mn, mx)
        sp.setDecimals(decimals)
        sp.setSingleStep(step)
        sp.setValue(val)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    def values(self) -> dict:
        return {
            "temp_fault": self._temp_fault.value(),
            "temp_warn": self._temp_warn.value(),
            "cur_fault": self._cur_fault.value(),
            "cur_warn": self._cur_warn.value(),
            "sensor_q_warn": self._sensor_q.value(),
            "use_bus": self._use_bus.isChecked(),
        }


class SweepConfigDialog(QDialog):
    """扫频采集配置：转速×负载测试点网格 + 每点稳定/采集时间。"""

    def __init__(self, parent=None, has_load: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("扫频采集 — 测试点网格")
        self.resize(460, 420)
        root = QVBoxLayout(self)
        info = QLabel("自动遍历「转速 × 负载」测试点网格：每点先驱动电机到该工况、"
                      "等待稳定，再采集一段数据（自动标注）。免去逐点手动采样。")
        info.setWordWrap(True)
        info.setStyleSheet("color: #8fa3b8;")
        root.addWidget(info)

        f = QFormLayout()
        self._spd_start = self._sp(0, 20000, 500, " rpm")
        self._spd_stop = self._sp(0, 20000, 3000, " rpm")
        self._spd_step = self._sp(0, 20000, 500, " rpm")
        f.addRow("转速 起始", self._spd_start)
        f.addRow("转速 终止", self._spd_stop)
        f.addRow("转速 步进", self._spd_step)

        self._load_start = self._dsp(0, 100, 0.0, " N·m")
        self._load_stop = self._dsp(0, 100, 0.4, " N·m")
        self._load_step = self._dsp(0, 100, 0.2, " N·m")
        if has_load:
            f.addRow("负载 起始", self._load_start)
            f.addRow("负载 终止", self._load_stop)
            f.addRow("负载 步进", self._load_step)
        else:
            note = QLabel("（真机模式无负载注入，仅按转速扫描）")
            note.setStyleSheet("color: #ffb74d;")
            f.addRow(note)
        self._has_load = has_load

        self._dwell = self._dsp(0.5, 60, 3.0, " s", 1, 0.5)
        self._collect = self._dsp(0.5, 120, 3.0, " s", 1, 0.5)
        f.addRow("每点稳定时间", self._dwell)
        f.addRow("每点采集时间", self._collect)
        root.addLayout(f)

        self._preview = QLabel("")
        self._preview.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        root.addWidget(self._preview)
        for w in (self._spd_start, self._spd_stop, self._spd_step,
                  self._load_start, self._load_stop, self._load_step,
                  self._dwell, self._collect):
            w.valueChanged.connect(self._update_preview)
        self._update_preview()

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("开始扫频")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    @staticmethod
    def _sp(mn, mx, val, suffix=""):
        sp = QSpinBox(); sp.setRange(mn, mx); sp.setValue(val)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    @staticmethod
    def _dsp(mn, mx, val, suffix="", decimals=1, step=0.1):
        sp = QDoubleSpinBox(); sp.setRange(mn, mx); sp.setDecimals(decimals)
        sp.setSingleStep(step); sp.setValue(val)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    def speeds(self) -> list:
        return _frange(self._spd_start.value(), self._spd_stop.value(),
                       self._spd_step.value())

    def loads(self) -> list:
        if not self._has_load:
            return [0.0]
        return _frange(self._load_start.value(), self._load_stop.value(),
                       self._load_step.value())

    def _update_preview(self) -> None:
        npt = len(self.speeds()) * len(self.loads())
        per = self._dwell.value() + self._collect.value()
        total = npt * per
        self._preview.setText(
            f"{len(self.speeds())} 转速 × {len(self.loads())} 负载 = "
            f"{npt} 个测试点，预计约 {total:.0f} s（{total/60:.1f} min）")

    def values(self) -> dict:
        return {
            "speeds": self.speeds(),
            "loads": self.loads(),
            "dwell_s": self._dwell.value(),
            "collect_s": self._collect.value(),
        }
