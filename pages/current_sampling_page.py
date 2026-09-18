"""独立的电流采样与 PWM 时序诊断页面。"""
import csv
from collections import deque
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from widgets.trend_curve import TrendCurve
from waveform_storage import create_waveform_record_dir


class CurrentSamplingPage(QWidget):
    def __init__(self, comm) -> None:
        super().__init__()
        self._comm = comm
        self._pending = deque(maxlen=1000)
        self._history = deque(maxlen=3000)  # 约一分钟 50 Hz 原始诊断
        root = QVBoxLayout(self)
        title = QLabel("电流采样诊断（F2按需启用，不阻塞控制心跳）")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        note = QLabel("独立显示 ADC 注入采样、零点校准、PWM 扇区、占空比和采样点；仅在电机 RUN 时更新。运行监控F1：以太网1 kHz，串口200 Hz。")
        note.setWordWrap(True)
        root.addWidget(note)
        timing = QLabel(
            "控制：电流环 16 kHz / 62.5 µs；速度环 500 Hz / 2 ms　　"
            "遥测：F1由通信页设置；F2可在下方选择 10/20/50 Hz")
        timing.setStyleSheet("color: #4fc3f7; font-weight: bold;")
        root.addWidget(timing)

        settings = QGroupBox("诊断采集与显示")
        settings_row = QHBoxLayout(settings)
        self._f2_enabled = QCheckBox("启用F2采样诊断")
        self._f2_enabled.setChecked(
            bool(comm.telemetry_config().get("flags", 0) & 0x01))
        settings_row.addWidget(self._f2_enabled)
        settings_row.addWidget(QLabel("采集频率"))
        self._f2_period = QComboBox()
        for text, period_ms in (("50 Hz", 20), ("20 Hz", 50), ("10 Hz", 100)):
            self._f2_period.addItem(text, period_ms)
        settings_row.addWidget(self._f2_period)
        self._display_smoothing = QCheckBox("显示平均")
        settings_row.addWidget(self._display_smoothing)
        self._smoothing_points = QSpinBox()
        self._smoothing_points.setRange(2, 64)
        self._smoothing_points.setValue(5)
        self._smoothing_points.setSuffix(" 点")
        self._smoothing_points.setEnabled(False)
        settings_row.addWidget(self._smoothing_points)
        self._pause_plot = QCheckBox("暂停绘图（继续接收）")
        settings_row.addWidget(self._pause_plot)
        self._f2_status = QLabel("F2：等待连接")
        self._f2_status.setStyleSheet("color:#90a4ae;")
        settings_row.addWidget(self._f2_status)
        settings_row.addStretch(1)
        root.addWidget(settings)

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

        self.adc_curve = TrendCurve("ADC注入组原始值", {"ADC1": "#4fc3f7", "ADC2": "#ff8a80"}, "ADC count", 2000)
        self.offset_curve = TrendCurve("零点校准值", {"A相零点": "#66bb6a", "B相零点": "#ffee58"}, "ADC x2", 2000)
        self.duty_curve = TrendCurve("PWM比较值与采样点", {"A": "#4fc3f7", "B": "#ff8a80", "C": "#ba68c8", "采样点": "#ffee58"}, "timer count", 2000)
        self.sector_curve = TrendCurve("PWM扇区", {"扇区": "#66bb6a"}, "1-6", 2000)
        for curve in (self.adc_curve, self.offset_curve,
                      self.duty_curve, self.sector_curve):
            curve.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            curve.setMinimumHeight(300)

        tabs = QTabWidget()
        analog_tab = QWidget()
        analog_layout = QHBoxLayout(analog_tab)
        analog_layout.setContentsMargins(4, 4, 4, 4)
        analog_layout.setSpacing(8)
        analog_layout.addWidget(self.adc_curve, 1)
        analog_layout.addWidget(self.offset_curve, 1)
        pwm_tab = QWidget()
        pwm_layout = QHBoxLayout(pwm_tab)
        pwm_layout.setContentsMargins(4, 4, 4, 4)
        pwm_layout.setSpacing(8)
        pwm_layout.addWidget(self.duty_curve, 1)
        pwm_layout.addWidget(self.sector_curve, 1)
        tabs.addTab(analog_tab, "ADC与零点")
        tabs.addTab(pwm_tab, "PWM时序")
        root.addWidget(tabs, 1)

        comm.currentSamplingDiagReceived.connect(self._on_sample)
        comm.statusChanged.connect(self._on_connection_changed)
        comm.telemetryConfigChanged.connect(self._sync_telemetry_config)
        self._f2_enabled.toggled.connect(self._apply_f2_setting)
        self._f2_period.currentIndexChanged.connect(self._apply_f2_setting)
        self._display_smoothing.toggled.connect(self._apply_display_smoothing)
        self._smoothing_points.valueChanged.connect(self._apply_display_smoothing)
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
        if self._pause_plot.isChecked():
            return
        interval_s = int(self._f2_period.currentData()) / 1000.0
        self.adc_curve.append_batch([{"ADC1": x["adc1_raw"], "ADC2": x["adc2_raw"]} for x in samples], interval_s)
        self.offset_curve.append_batch([{"A相零点": x["offset_a"], "B相零点": x["offset_b"]} for x in samples], interval_s)
        self.duty_curve.append_batch([{"A": x["duty_a"], "B": x["duty_b"], "C": x["duty_c"], "采样点": x["sample_point"]} for x in samples], interval_s)
        self.sector_curve.append_batch([{"扇区": x["sector"]} for x in samples], interval_s)

    def _apply_f2_setting(self, *_args) -> None:
        enabled = self._f2_enabled.isChecked()
        period_ms = int(self._f2_period.currentData())
        if not self._comm.is_connected():
            self._f2_status.setText("F2：连接后应用")
            return
        sent = self._comm.set_f2_diagnostics(enabled, period_ms)
        self._f2_status.setText(
            (f"F2：{'已启用' if enabled else '已关闭'} · "
             f"{1000 // period_ms} Hz") if sent else "F2：设置发送失败")

    def _sync_telemetry_config(self, config: dict) -> None:
        enabled = bool(int(config.get("flags", 0)) & 0x01)
        period_ms = int(config.get("f2_ms", 20))
        self._f2_enabled.blockSignals(True)
        self._f2_period.blockSignals(True)
        self._f2_enabled.setChecked(enabled)
        index = self._f2_period.findData(period_ms)
        if index >= 0:
            self._f2_period.setCurrentIndex(index)
        self._f2_enabled.blockSignals(False)
        self._f2_period.blockSignals(False)
        self._f2_status.setText(
            f"F2：{'已启用' if enabled else '已关闭'} · "
            f"{1000 // period_ms} Hz")

    def _on_connection_changed(self, connected: bool, _message: str) -> None:
        if connected:
            self._apply_f2_setting()
        else:
            self._f2_status.setText("F2：未连接")

    def _apply_display_smoothing(self, *_args) -> None:
        enabled = self._display_smoothing.isChecked()
        self._smoothing_points.setEnabled(enabled)
        points = self._smoothing_points.value() if enabled else 1
        for curve in (self.adc_curve, self.offset_curve, self.duty_curve):
            curve.set_smoothing(points)

    def _clear(self) -> None:
        self._pending.clear()
        self._history.clear()
        for curve in (self.adc_curve, self.offset_curve, self.duty_curve, self.sector_curve):
            curve.clear()

    def _save_csv(self) -> None:
        record_dir = create_waveform_record_dir("电流采样诊断", datetime.now())
        default = str(record_dir / "原始数据.csv")
        path, _ = QFileDialog.getSaveFileName(self, "保存电流采样诊断", default,
                                               "CSV (*.csv)")
        if not path:
            record_dir.rmdir()
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
