"""独立的电流采样与 PWM 时序诊断页面。"""
import csv
from collections import deque
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QGroupBox, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from widgets.trend_curve import TrendCurve
from runtime_paths import writable_path


class CurrentSamplingPage(QWidget):
    def __init__(self, comm) -> None:
        super().__init__()
        self._pending = deque(maxlen=1000)
        self._history = deque(maxlen=3000)  # 约一分钟 50 Hz 原始诊断
        root = QVBoxLayout(self)
        title = QLabel("电流采样诊断（50 Hz，不阻塞控制心跳）")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        note = QLabel("独立显示 ADC 注入采样、零点校准、PWM 扇区、占空比和采样点；仅在电机 RUN 时更新。运行监控F1：以太网1 kHz，串口200 Hz。")
        note.setWordWrap(True)
        root.addWidget(note)
        timing = QLabel("控制：电流环 16 kHz / 62.5 µs；速度环 500 Hz / 2 ms　　遥测：F1 以太网1 kHz/串口200 Hz；F2 50 Hz")
        timing.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        root.addWidget(timing)

        box = QGroupBox("实时快照")
        grid = QGridLayout(box)
        self._labels = {}
        fields = [("adc1_raw", "ADC1 JDR1"), ("adc2_raw", "ADC2 JDR1"),
                  ("offset_a", "A相零点"), ("offset_b", "B相零点"),
                  ("sector", "PWM扇区"), ("sample_point", "采样点 CCR4"),
                  ("duty_a", "A相比较值"), ("duty_b", "B相比较值"),
                  ("duty_c", "C相比较值")]
        fields.extend([("cal_adc1_pp", "校准期ADC1峰峰"),
                       ("cal_adc2_pp", "校准期ADC2峰峰")])
        fields.extend([("adc1_v", "ADC1电压/V"), ("adc2_v", "ADC2电压/V"),
                       ("adc1_delta_a", "ADC1等效电流/A"),
                       ("adc2_delta_a", "ADC2等效电流/A"),
                       ("vdda_v", "实测VDDA/V")])
        for index, (key, text) in enumerate(fields):
            grid.addWidget(QLabel(text), index // 3 * 2, index % 3)
            value = QLabel("--")
            value.setStyleSheet("font-size: 20px; color: #42bff5;")
            grid.addWidget(value, index // 3 * 2 + 1, index % 3)
            self._labels[key] = value
        clear = QPushButton("清空曲线")
        clear.clicked.connect(self._clear)
        grid.addWidget(clear, 12, 2)
        save = QPushButton("保存诊断 CSV")
        save.clicked.connect(self._save_csv)
        grid.addWidget(save, 12, 1)
        root.addWidget(box)

        curves = QGridLayout()
        self.adc_curve = TrendCurve("ADC注入组原始值", {"ADC1": "#4fc3f7", "ADC2": "#ff8a80"}, "ADC count", 2000)
        self.offset_curve = TrendCurve("零点校准值", {"A相零点": "#66bb6a", "B相零点": "#ffee58"}, "ADC x2", 2000)
        self.duty_curve = TrendCurve("PWM比较值与采样点", {"A": "#4fc3f7", "B": "#ff8a80", "C": "#ba68c8", "采样点": "#ffee58"}, "timer count", 2000)
        self.sector_curve = TrendCurve("PWM扇区", {"扇区": "#66bb6a"}, "1-6", 2000)
        curves.addWidget(self.adc_curve, 0, 0)
        curves.addWidget(self.offset_curve, 0, 1)
        curves.addWidget(self.duty_curve, 1, 0)
        curves.addWidget(self.sector_curve, 1, 1)
        root.addLayout(curves)

        comm.currentSamplingDiagReceived.connect(self._on_sample)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.start(50)

    def _on_sample(self, sample: dict) -> None:
        self._pending.append(dict(sample))
        self._history.append(dict(sample))
        for key, label in self._labels.items():
            value = sample.get(key, "--")
            label.setText(f"{value:.4f}" if isinstance(value, float) else str(value))

    def _flush(self) -> None:
        if not self._pending:
            return
        samples = list(self._pending)
        self._pending.clear()
        self.adc_curve.append_batch([{"ADC1": x["adc1_raw"], "ADC2": x["adc2_raw"]} for x in samples], .02)
        self.offset_curve.append_batch([{"A相零点": x["offset_a"], "B相零点": x["offset_b"]} for x in samples], .02)
        self.duty_curve.append_batch([{"A": x["duty_a"], "B": x["duty_b"], "C": x["duty_c"], "采样点": x["sample_point"]} for x in samples], .02)
        self.sector_curve.append_batch([{"扇区": x["sector"]} for x in samples], .02)

    def _clear(self) -> None:
        self._pending.clear()
        self._history.clear()
        for curve in (self.adc_curve, self.offset_curve, self.duty_curve, self.sector_curve):
            curve.clear()

    def _save_csv(self) -> None:
        default = str(writable_path(
            "波形记录", f"电流采样诊断_{datetime.now():%Y%m%d_%H%M%S}.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "保存电流采样诊断", default,
                                               "CSV (*.csv)")
        if not path:
            return
        fields = ("tick_ms", "adc1_raw", "adc2_raw", "offset_a", "offset_b",
                  "sector", "duty_a", "duty_b", "duty_c", "sample_point",
                  "cal_adc1_min", "cal_adc1_max", "cal_adc1_pp",
                  "cal_adc2_min", "cal_adc2_max", "cal_adc2_pp",
                  "adc1_v", "adc2_v", "zero_a_v", "zero_b_v",
                  "adc1_delta_a", "adc2_delta_a", "vdda_v")
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in fields}
                             for row in self._history)
