"""离线傅里叶分析页。

该页只在用户点击“开始离线 FFT”或加载 CSV 后计算，不参与实时绘图
刷新，因而不会抢占 16 kHz 通信/绘图路径。
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

try:
    import numpy as np
    import pyqtgraph as pg
    _FFT_PLOT_OK = True
except Exception:  # pragma: no cover
    _FFT_PLOT_OK = False


def compute_spectrum(values, sample_rate_hz: float,
                     window_name: str = "hann",
                     remove_dc: bool = True) -> dict:
    """计算单边峰值幅频谱；可选仅移除0 Hz平均值。"""
    if not _FFT_PLOT_OK:
        raise RuntimeError("未安装 numpy/pyqtgraph")
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("采样率无效，无法建立频率轴")
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    if data.size < 32:
        raise ValueError("有效样本少于 32 点，无法进行可靠 FFT")

    dc = float(np.mean(data))
    centered = data - dc
    name = str(window_name).lower()
    if name == "hann":
        window = np.hanning(data.size)
        window_text = "Hann（汉宁）"
    elif name == "blackman":
        window = np.blackman(data.size)
        window_text = "Blackman"
    elif name in ("rect", "rectangular", "none"):
        window = np.ones(data.size)
        window_text = "矩形（不加窗）"
    else:
        raise ValueError(f"未知窗函数：{window_name}")

    coherent_gain = float(np.sum(window))
    if coherent_gain <= 0.0:
        raise ValueError("窗函数相干增益无效")
    # 非零频率始终从去均值后的交流分量计算。若选择“保留 DC”，只把
    # 精确的平均值放回 0 Hz 频点，避免常量经 Hann/Blackman 窗后泄漏到
    # 第一个频点并被误判为基波。
    spectrum = np.fft.rfft(centered * window)
    amplitude = 2.0 * np.abs(spectrum) / coherent_gain
    amplitude[0] = 0.0 if remove_dc else abs(dc)
    if data.size % 2 == 0 and amplitude.size > 1:
        amplitude[-1] *= 0.5  # Nyquist 频点不能翻倍
    frequencies = np.fft.rfftfreq(data.size, d=1.0 / rate)

    fundamental_bin = int(np.argmax(amplitude[1:])) + 1
    fundamental_amp = float(amplitude[fundamental_bin])
    fundamental_hz = float(frequencies[fundamental_bin])
    harmonic_sq = 0.0
    harmonics = []
    if fundamental_amp > 1e-12 and fundamental_hz > 0.0:
        order = 2
        while order * fundamental_hz <= rate / 2.0:
            center = int(round(order * fundamental_hz * data.size / rate))
            lo = max(1, center - 1)
            hi = min(amplitude.size, center + 2)
            if lo >= hi:
                break
            local = amplitude[lo:hi]
            index = lo + int(np.argmax(local))
            value = float(amplitude[index])
            harmonic_sq += value * value
            harmonics.append((order, float(frequencies[index]), value))
            order += 1
    thd_percent = (math.sqrt(harmonic_sq) / fundamental_amp * 100.0
                   if fundamental_amp > 1e-12 else float("nan"))
    ripple_rms = float(np.sqrt(np.mean(centered * centered)))
    ripple_factor_percent = (
        ripple_rms / abs(dc) * 100.0 if abs(dc) > 1e-12 else float("nan"))
    return {
        "frequencies": frequencies,
        "amplitudes": amplitude,
        "sample_rate_hz": rate,
        "sample_count": int(data.size),
        "duration_s": float(data.size / rate),
        "resolution_hz": float(rate / data.size),
        "dc": dc,
        "rms": ripple_rms,
        "peak_to_peak": float(np.max(data) - np.min(data)),
        "ripple_factor_percent": ripple_factor_percent,
        "fundamental_hz": fundamental_hz,
        "fundamental_amplitude": fundamental_amp,
        "thd_percent": thd_percent,
        "harmonics": harmonics,
        "window_text": window_text,
        "remove_dc": bool(remove_dc),
    }


def compute_order_lms(values, sample_rate_hz: float,
                      base_frequency_hz: float,
                      orders=(1, 2, 3), step_size: float = 0.08,
                      epochs: int = 4) -> dict:
    """用离线归一化LMS学习并移除指定机械阶次。

    返回的 ``filtered_values`` 是速度域的理想阶次滤除结果，用来判断周期
    分量可被消除多少；它不是可直接下发的 q 轴补偿电流。实际电流还需要
    ``iq -> speed`` 对象的幅值和相位。
    """
    if not _FFT_PLOT_OK:
        raise RuntimeError("未安装 numpy/pyqtgraph")
    rate = float(sample_rate_hz)
    base_hz = float(base_frequency_hz)
    mu = float(step_size)
    pass_count = int(epochs)
    order_list = tuple(sorted({int(order) for order in orders}))
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("采样率无效")
    if not math.isfinite(base_hz) or base_hz <= 0.0:
        raise ValueError("机械基频必须大于0 Hz")
    if not order_list or order_list[0] <= 0:
        raise ValueError("阶次必须为正整数")
    if order_list[-1] * base_hz >= rate / 2.0:
        raise ValueError("最高补偿阶次达到或超过Nyquist频率")
    if not math.isfinite(mu) or not 0.0 < mu <= 1.0:
        raise ValueError("LMS步长必须位于0～1之间")
    if pass_count < 1:
        raise ValueError("LMS迭代轮数至少为1")

    data = np.asarray(values, dtype=float)
    if data.ndim != 1:
        data = data.reshape(-1)
    if data.size < 32 or not np.all(np.isfinite(data)):
        raise ValueError("LMS要求至少32个连续有限样本")
    dc = float(np.mean(data))
    target = data - dc
    sample_index = np.arange(data.size, dtype=float)
    base_phase = 2.0 * np.pi * base_hz * sample_index / rate
    columns = []
    for order in order_list:
        columns.extend((np.sin(order * base_phase),
                        np.cos(order * base_phase)))
    regressors = np.column_stack(columns)
    weights = np.zeros(regressors.shape[1], dtype=float)
    convergence_rms = []
    epsilon = 1e-12
    for _ in range(pass_count):
        squared_error = 0.0
        for row, desired in zip(regressors, target):
            error = float(desired - np.dot(weights, row))
            weights += mu * error * row / (
                epsilon + float(np.dot(row, row)))
            squared_error += error * error
        convergence_rms.append(math.sqrt(squared_error / data.size))

    learned_ripple = regressors @ weights
    # 有限记录不一定包含整数周期。移除预测分量的有限窗均值，确保离线
    # 滤除只影响交流阶次，不移动原始直流工作点。
    learned_ripple -= float(np.mean(learned_ripple))
    filtered = data - learned_ripple
    before_rms = float(np.sqrt(np.mean(target * target)))
    filtered_ac = filtered - float(np.mean(filtered))
    after_rms = float(np.sqrt(np.mean(filtered_ac * filtered_ac)))
    attenuation = (100.0 * (1.0 - after_rms / before_rms)
                   if before_rms > 1e-12 else 0.0)
    components = []
    for index, order in enumerate(order_list):
        sine_weight = float(weights[2 * index])
        cosine_weight = float(weights[2 * index + 1])
        components.append({
            "order": order,
            "frequency_hz": order * base_hz,
            "sine_weight": sine_weight,
            "cosine_weight": cosine_weight,
            "amplitude": math.hypot(sine_weight, cosine_weight),
            "phase_deg": math.degrees(math.atan2(
                cosine_weight, sine_weight)),
        })
    return {
        "sample_rate_hz": rate,
        "sample_count": int(data.size),
        "dc": dc,
        "base_frequency_hz": base_hz,
        "orders": order_list,
        "step_size": mu,
        "epochs": pass_count,
        "weights": weights,
        "order_components": components,
        "learned_ripple": learned_ripple,
        "filtered_values": filtered,
        "before_rms": before_rms,
        "after_rms": after_rms,
        "attenuation_percent": attenuation,
        "convergence_rms": convergence_rms,
    }


class OrderLmsDialog(QDialog):
    """离线机械阶次LMS实验，不占用傅里叶主页高度。"""

    def __init__(self, label: str, times, values, sample_rate_hz: float,
                 unit: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("离线阶次 LMS（速度域理想补偿）")
        self.resize(1180, 760)
        self._label = str(label)
        self._times = np.asarray(times, dtype=float)
        self._values = np.asarray(values, dtype=float)
        self._sample_rate_hz = float(sample_rate_hz)
        self._unit = str(unit)
        self._result = None

        root = QVBoxLayout(self)
        note = QLabel(
            "NLMS学习与机械转频同步的正弦/余弦阶次，并显示从原信号中"
            "理想扣除后的结果。这里只验证速度纹波可消除量，不直接输出iq补偿电流；"
            "真实注入还需辨识 iq→速度 的增益和相位。")
        note.setStyleSheet("color:#90a4ae;")
        note.setWordWrap(True)
        root.addWidget(note)

        controls = QGroupBox("LMS设置")
        grid = QGridLayout(controls)
        grid.addWidget(QLabel("机械基频"), 0, 0)
        self._base_frequency = QDoubleSpinBox()
        self._base_frequency.setRange(0.01, max(
            0.01, self._sample_rate_hz / 2.0 - 0.01))
        self._base_frequency.setDecimals(4)
        mean_value = float(np.mean(self._values)) if self._values.size else 0.0
        default_base = abs(mean_value) / 60.0 if "rpm" in self._unit.lower() else 0.0
        if default_base <= 0.0 or default_base >= self._sample_rate_hz / 2.0:
            default_base = min(8.3333, self._sample_rate_hz / 4.0)
        self._base_frequency.setValue(default_base)
        self._base_frequency.setSuffix(" Hz")
        self._base_frequency.setToolTip(
            "转速信号默认使用 |平均rpm|/60；可按FFT主峰手动修正。")
        grid.addWidget(self._base_frequency, 0, 1)

        grid.addWidget(QLabel("连续阶次"), 0, 2)
        self._order_count = QSpinBox()
        self._order_count.setRange(1, 8)
        self._order_count.setValue(5)
        self._order_count.setSuffix(" 阶")
        grid.addWidget(self._order_count, 0, 3)

        grid.addWidget(QLabel("NLMS步长"), 0, 4)
        self._step_size = QDoubleSpinBox()
        self._step_size.setRange(0.001, 1.0)
        self._step_size.setDecimals(3)
        self._step_size.setSingleStep(0.01)
        self._step_size.setValue(0.08)
        grid.addWidget(self._step_size, 0, 5)

        grid.addWidget(QLabel("离线轮数"), 0, 6)
        self._epochs = QSpinBox()
        self._epochs.setRange(1, 30)
        self._epochs.setValue(4)
        grid.addWidget(self._epochs, 0, 7)

        self._run_btn = QPushButton("运行LMS")
        self._run_btn.setObjectName("PrimaryButton")
        self._run_btn.clicked.connect(self.run_analysis)
        grid.addWidget(self._run_btn, 0, 8)
        self._export_btn = QPushButton("导出结果CSV…")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        grid.addWidget(self._export_btn, 0, 9)
        root.addWidget(controls)

        self._metrics = QLabel("等待运行LMS")
        self._metrics.setStyleSheet("color:#80cbc4;")
        self._metrics.setWordWrap(True)
        root.addWidget(self._metrics)
        self._components = QLabel("阶次系数：--")
        self._components.setStyleSheet("color:#ffcc80;")
        self._components.setWordWrap(True)
        root.addWidget(self._components)

        self._plots = pg.GraphicsLayoutWidget()
        self._plots.setBackground("#10131a")
        self._time_plot = self._plots.addPlot(
            row=0, col=0, title="速度纹波：原始 / LMS预测 / 滤后残差")
        self._time_plot.showGrid(x=True, y=True, alpha=0.3)
        self._time_plot.setLabel("bottom", "采集时间", units="s")
        self._time_plot.setLabel("left", "交流纹波", units=self._unit or None)
        self._raw_curve = self._time_plot.plot(
            [], [], pen=pg.mkPen("#90a4ae", width=1), name="原始交流")
        self._learned_curve = self._time_plot.plot(
            [], [], pen=pg.mkPen("#ffb74d", width=1.5), name="LMS预测阶次")
        self._filtered_curve = self._time_plot.plot(
            [], [], pen=pg.mkPen("#4fc3f7", width=1.5), name="滤后残差")
        self._time_plot.addLegend()

        self._spectrum_plot = self._plots.addPlot(
            row=1, col=0, title="补偿前后单边频谱")
        self._spectrum_plot.showGrid(x=True, y=True, alpha=0.3)
        self._spectrum_plot.setLabel("bottom", "频率", units="Hz")
        self._spectrum_plot.setLabel("left", "峰值幅值", units=self._unit or None)
        self._before_spectrum = self._spectrum_plot.plot(
            [], [], pen=pg.mkPen("#ff8a80", width=1.3), name="补偿前")
        self._after_spectrum = self._spectrum_plot.plot(
            [], [], pen=pg.mkPen("#4fc3f7", width=1.5), name="理想补偿后")
        self._spectrum_plot.addLegend()

        visibility = QHBoxLayout()
        visibility.setContentsMargins(0, 0, 0, 0)
        visibility.addWidget(QLabel("显示："))
        self._show_raw = QCheckBox("原始交流")
        self._show_learned = QCheckBox("LMS预测阶次")
        self._show_filtered = QCheckBox("滤后残差")
        self._show_before_spectrum = QCheckBox("补偿前频谱")
        self._show_after_spectrum = QCheckBox("补偿后频谱")
        curve_switches = (
            (self._show_raw, self._raw_curve),
            (self._show_learned, self._learned_curve),
            (self._show_filtered, self._filtered_curve),
            (self._show_before_spectrum, self._before_spectrum),
            (self._show_after_spectrum, self._after_spectrum),
        )
        for checkbox, curve in curve_switches:
            checkbox.setChecked(True)
            checkbox.toggled.connect(curve.setVisible)
            visibility.addWidget(checkbox)
        visibility.addStretch(1)
        root.addLayout(visibility)
        root.addWidget(self._plots, 1)

    def run_analysis(self) -> None:
        try:
            order_count = int(self._order_count.value())
            result = compute_order_lms(
                self._values, self._sample_rate_hz,
                self._base_frequency.value(),
                orders=tuple(range(1, order_count + 1)),
                step_size=self._step_size.value(),
                epochs=self._epochs.value())
            before = compute_spectrum(
                self._values, self._sample_rate_hz, "hann", True)
            after = compute_spectrum(
                result["filtered_values"], self._sample_rate_hz,
                "hann", True)
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "LMS无法执行", str(exc))
            return
        self._result = result
        center = self._values - float(np.mean(self._values))
        filtered_ac = (result["filtered_values"]
                       - float(np.mean(result["filtered_values"])))
        self._raw_curve.setData(self._times, center)
        self._learned_curve.setData(self._times, result["learned_ripple"])
        self._filtered_curve.setData(self._times, filtered_ac)
        self._before_spectrum.setData(
            before["frequencies"], before["amplitudes"])
        self._after_spectrum.setData(
            after["frequencies"], after["amplitudes"])
        max_order_hz = result["base_frequency_hz"] * max(result["orders"])
        self._spectrum_plot.setXRange(
            0.0, min(self._sample_rate_hz / 2.0,
                     max(50.0, max_order_hz * 1.5)), padding=0.01)
        self._time_plot.enableAutoRange()
        self._metrics.setText(
            f"{self._label} · N={result['sample_count']} · "
            f"fs={result['sample_rate_hz']:.3f} Hz · "
            f"机械基频={result['base_frequency_hz']:.4f} Hz · "
            f"交流RMS {result['before_rms']:.4g} → "
            f"{result['after_rms']:.4g} {self._unit} · "
            f"理想衰减={result['attenuation_percent']:.2f}% · "
            f"末轮在线误差RMS={result['convergence_rms'][-1]:.4g}")
        descriptions = [
            f"{item['order']}阶 {item['frequency_hz']:.3f} Hz："
            f"A={item['amplitude']:.4g} {self._unit}，"
            f"φ={item['phase_deg']:.1f}°"
            for item in result["order_components"]
        ]
        self._components.setText("阶次系数：" + "；".join(descriptions))
        self._export_btn.setEnabled(True)

    def _export_csv(self) -> None:
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出LMS离线结果", "阶次LMS结果.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow((
                    "time_s", "original", "learned_order_ripple",
                    "ideal_filtered", "unit"))
                for time_value, original, learned, filtered in zip(
                        self._times, self._values,
                        self._result["learned_ripple"],
                        self._result["filtered_values"]):
                    writer.writerow((time_value, original, learned,
                                     filtered, self._unit))
        except OSError as exc:
            QMessageBox.warning(self, "CSV导出失败", str(exc))


class FourierAnalysisPage(QWidget):
    """分析当前监控原始缓冲，或从“保存所有波形”CSV读取数据。"""

    def __init__(self, snapshot_provider: Callable[[str], dict] | None = None,
                 source_items: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self._snapshot_provider = snapshot_provider
        self._loaded_sources: dict[str, dict] = {}
        self._source_items = source_items or []
        self._order_lms_dialog = None
        self._updating_selection_region = False
        self._last_result = None

        root = QVBoxLayout(self)
        title = QLabel("离线傅里叶分析")
        title.setObjectName("TitleLabel")
        root.addWidget(title)
        intro = QLabel(
            "对已采集的原始缓冲或已保存 CSV 做 FFT；不在实时刷新线程中计算。"
            "分析使用原始点，不使用监控页的“显示平滑”结果。"
            "完成一次分析后可直接拖动蓝色区间的左右边界，松开即重新计算。")
        intro.setStyleSheet("color:#90a4ae;")
        intro.setWordWrap(True)
        root.addWidget(intro)

        controls = QGroupBox("分析设置")
        grid = QGridLayout(controls)
        grid.addWidget(QLabel("信号"), 0, 0)
        self._signal_combo = QComboBox()
        for label, key in self._source_items:
            self._signal_combo.addItem(label, ("live", key))
        grid.addWidget(self._signal_combo, 0, 1)

        grid.addWidget(QLabel("窗函数"), 0, 2)
        self._window_combo = QComboBox()
        self._window_combo.addItem("Hann（推荐，抑制频谱泄漏）", "hann")
        self._window_combo.addItem("Blackman（更强旁瓣抑制）", "blackman")
        self._window_combo.addItem("矩形（不加窗）", "rect")
        grid.addWidget(self._window_combo, 0, 3)

        grid.addWidget(QLabel("分析类型"), 1, 0)
        self._analysis_combo = QComboBox()
        self._analysis_combo.addItem("自动（按信号类型）", "auto")
        self._analysis_combo.addItem("交流量：基波 / THD", "ac")
        self._analysis_combo.addItem("直流量：纹波 / 主振荡", "dc")
        grid.addWidget(self._analysis_combo, 1, 1)

        self._remove_dc = QCheckBox("FFT前去直流（仅移除0 Hz平均值）")
        self._remove_dc.setChecked(True)
        self._remove_dc.setToolTip(
            "只减去信号平均值，不会删除交流基波或其他非零频率峰。")
        grid.addWidget(self._remove_dc, 1, 2, 1, 2)

        grid.addWidget(QLabel("样本数"), 2, 0)
        self._points_combo = QComboBox()
        self._points_combo.addItem("全部有效样本", 0)
        for count in (1024, 2048, 4096):
            self._points_combo.addItem(f"最新 {count} 点", count)
        grid.addWidget(self._points_combo, 2, 1)

        grid.addWidget(QLabel("显示上限"), 2, 2)
        self._max_freq = QDoubleSpinBox()
        self._max_freq.setRange(0.0, 100000.0)
        self._max_freq.setDecimals(0)
        self._max_freq.setValue(1000.0)
        self._max_freq.setSuffix(" Hz")
        self._max_freq.setSpecialValueText("自动（Nyquist）")
        grid.addWidget(self._max_freq, 2, 3)

        self._annotate_peaks = QCheckBox("在图中标注尖峰")
        self._annotate_peaks.setChecked(True)
        grid.addWidget(self._annotate_peaks, 3, 0)
        self._peak_count = QSpinBox()
        self._peak_count.setRange(1, 12)
        self._peak_count.setValue(6)
        self._peak_count.setSuffix(" 个")
        grid.addWidget(self._peak_count, 3, 1)

        self._use_plot_interval = QCheckBox("使用蓝色拖选区间")
        self._use_plot_interval.setToolTip(
            "拖动时域图中蓝色区域或两侧边界，松开后自动按该区间重新计算FFT")
        grid.addWidget(self._use_plot_interval, 3, 2, 1, 2)
        self._points_combo.currentIndexChanged.connect(
            self._disable_custom_interval)
        self._signal_combo.currentIndexChanged.connect(
            self._disable_custom_interval)

        buttons = QHBoxLayout()
        self._analyze_btn = QPushButton("开始离线 FFT")
        self._analyze_btn.setObjectName("PrimaryButton")
        self._analyze_btn.clicked.connect(self._analyze_selected)
        buttons.addWidget(self._analyze_btn)
        self._load_btn = QPushButton("加载波形 CSV…")
        self._load_btn.clicked.connect(self._load_csv_dialog)
        buttons.addWidget(self._load_btn)
        self._save_plot_btn = QPushButton("保存带标注频谱图…")
        self._save_plot_btn.clicked.connect(self._save_plot)
        buttons.addWidget(self._save_plot_btn)
        self._order_lms_btn = QPushButton("阶次 LMS…")
        self._order_lms_btn.setToolTip(
            "在独立窗口中离线学习机械阶次，不增加当前页面高度")
        self._order_lms_btn.clicked.connect(self._open_order_lms)
        buttons.addWidget(self._order_lms_btn)
        buttons.addStretch(1)
        grid.addLayout(buttons, 4, 0, 1, 4)
        root.addWidget(controls)

        metrics = QGroupBox("分析结果")
        metric_grid = QGridLayout(metrics)
        self._metric_fs = QLabel("有效采样率：--")
        self._metric_fs.setToolTip(
            "FFT频率轴采用的曲线点率。F0慢变量可能由时间戳估算，"
            "不等于固件内部控制器或测速器的更新频率。")
        self._metric_n = QLabel("样本/分辨率：--")
        self._metric_time = QLabel("FFT区间：--")
        self._metric_fund = QLabel("基频：--")
        self._metric_amp = QLabel("基波幅值：--")
        self._metric_thd = QLabel("THD：--")
        self._metric_rms = QLabel("AC RMS：--")
        for index, widget in enumerate((
                self._metric_fs, self._metric_n, self._metric_fund,
                self._metric_amp, self._metric_thd, self._metric_rms)):
            widget.setAlignment(Qt.AlignCenter)
            metric_grid.addWidget(widget, index // 3, index % 3)
        self._metric_time.setAlignment(Qt.AlignCenter)
        metric_grid.addWidget(self._metric_time, 2, 0, 1, 3)
        root.addWidget(metrics)

        self._processing = QLabel("数据处理：等待选择数据")
        self._processing.setStyleSheet("color:#80cbc4;")
        self._processing.setWordWrap(True)
        root.addWidget(self._processing)

        if _FFT_PLOT_OK:
            # 两张图共享原来单张频谱图的伸缩区域，不增加页面固定高度。
            # 上图保留完整输入并高亮FFT实际使用片段，下图显示其频谱。
            self._plot_container = pg.GraphicsLayoutWidget()
            self._plot_container.setBackground("#10131a")
            self._input_plot = self._plot_container.addPlot(
                row=0, col=0, title="FFT输入时域波形（等待分析）")
            self._input_plot.showGrid(x=True, y=True, alpha=0.3)
            self._input_plot.setLabel("bottom", "采集时间", units="s")
            self._input_plot.setLabel("left", "输入幅值")
            self._input_full_curve = self._input_plot.plot(
                [], [], pen=pg.mkPen("#546e7a", width=1))
            self._input_selected_curve = self._input_plot.plot(
                [], [], pen=pg.mkPen("#4fc3f7", width=1.8))
            self._selection_region = pg.LinearRegionItem(
                [0.0, 0.0], movable=True,
                brush=pg.mkBrush(79, 195, 247, 35),
                pen=pg.mkPen("#4fc3f7", width=1))
            self._selection_region.setZValue(-5)
            self._input_plot.addItem(self._selection_region)
            self._selection_region.hide()
            self._selection_region.sigRegionChangeFinished.connect(
                self._analyze_dragged_region)

            self._plot = self._plot_container.addPlot(
                row=1, col=0, title="单边幅频谱（去直流）")
            self._plot.showGrid(x=True, y=True, alpha=0.3)
            self._plot.setLabel("bottom", "频率", units="Hz")
            self._plot.setLabel("left", "峰值幅值")
            self._spectrum_curve = self._plot.plot(
                [], [], pen=pg.mkPen("#4fc3f7", width=1.5))
            self._peak_markers = pg.ScatterPlotItem(
                size=7, pen=pg.mkPen("#ffcc80"),
                brush=pg.mkBrush("#ff8f00"))
            self._plot.addItem(self._peak_markers)
            self._peak_text_items = []
            root.addWidget(self._plot_container, 1)
        else:
            root.addWidget(QLabel("[未安装 numpy/pyqtgraph，无法进行 FFT]"), 1)
            self._analyze_btn.setEnabled(False)
            self._save_plot_btn.setEnabled(False)

        self._peak_summary = QLabel("峰值标注：等待分析")
        self._peak_summary.setStyleSheet("color:#ffcc80; font-size:11px;")
        self._peak_summary.setWordWrap(True)
        root.addWidget(self._peak_summary)

        self._detail = QLabel(
            "Ia/Ib 等交流量计算基波与THD；Iq/Vd/Vq/转速/转矩等"
            "直流量改为平均值、纹波RMS、峰峰值、纹波率和主振荡频率。"
            "“去直流”只移除0 Hz平均值，不会移除基波主峰。")
        self._detail.setStyleSheet("color:#90a4ae; font-size:11px;")
        self._detail.setWordWrap(True)
        root.addWidget(self._detail)

    def _selected_snapshot(self) -> tuple[str, dict]:
        token = self._signal_combo.currentData()
        if not token:
            raise ValueError("请先选择信号")
        kind, key = token
        if kind == "loaded":
            return self._signal_combo.currentText(), self._loaded_sources[key]
        if self._snapshot_provider is None:
            raise ValueError("当前监控缓冲不可用")
        return self._signal_combo.currentText(), self._snapshot_provider(key)

    def _analyze_selected(self) -> None:
        try:
            label, snapshot = self._selected_snapshot()
            all_values = list(snapshot.get("values", ()))
            rate_hz = float(snapshot.get("sample_rate_hz", 0.0))
            all_times = list(snapshot.get("times", ()))
            if len(all_times) != len(all_values):
                if rate_hz <= 0.0:
                    raise ValueError("采样率和采集时间均无效，无法建立FFT区间")
                all_times = [index / rate_hz
                             for index in range(len(all_values))]
            if (self._use_plot_interval.isChecked()
                    and self._selection_region.isVisible()):
                region_start, region_end = sorted(
                    float(value) for value in self._selection_region.getRegion())
                selected_indices = [
                    index for index, time_value in enumerate(all_times)
                    if region_start <= float(time_value) < region_end
                ]
                if not selected_indices:
                    raise ValueError("蓝色区间内没有有效样本")
                first = selected_indices[0]
                last = selected_indices[-1] + 1
                values = all_values[first:last]
                selected_times = all_times[first:last]
            else:
                count = int(self._points_combo.currentData() or 0)
                start = max(0, len(all_values) - count) if count > 0 else 0
                values = all_values[start:]
                selected_times = all_times[start:]
            result = compute_spectrum(
                values, rate_hz,
                str(self._window_combo.currentData()),
                self._remove_dc.isChecked())
        except (ValueError, RuntimeError, KeyError) as exc:
            QMessageBox.warning(self, "FFT 无法执行", str(exc))
            return
        analysis_kind = str(self._analysis_combo.currentData())
        if analysis_kind == "auto":
            analysis_kind = str(snapshot.get("analysis_kind", "dc"))
        analysis_snapshot = dict(snapshot)
        analysis_snapshot.update({
            "fft_all_times": all_times,
            "fft_all_values": all_values,
            "fft_times": selected_times,
            "fft_values": values,
        })
        self._last_result = result
        self._show_result(label, analysis_snapshot, result, analysis_kind)

    def _disable_custom_interval(self, _index=None) -> None:
        """选择信号或快捷样本数后，下一次恢复快捷区间。"""
        if hasattr(self, "_use_plot_interval"):
            self._use_plot_interval.setChecked(False)

    def _analyze_dragged_region(self) -> None:
        """蓝色区间拖动结束后，将其作为FFT的准确输入范围。"""
        if (self._updating_selection_region
                or not self._selection_region.isVisible()):
            return
        self._use_plot_interval.setChecked(True)
        self._analyze_selected()

    def _show_result(self, label: str, snapshot: dict, result: dict,
                     analysis_kind: str = "ac") -> None:
        self._show_input_waveform(label, snapshot, result)
        frequency = result["frequencies"]
        amplitude = result["amplitudes"]
        limit = float(self._max_freq.value())
        if limit > 0.0:
            mask = frequency <= min(limit, result["sample_rate_hz"] / 2.0)
            frequency = frequency[mask]
            amplitude = amplitude[mask]
        self._spectrum_curve.setData(frequency, amplitude)
        self._plot.enableAutoRange()
        self._plot.setTitle(
            f"{label} · 单边幅频谱"
            f"（{'已去DC' if result['remove_dc'] else '保留DC'}）")
        unit = str(snapshot.get("unit", ""))
        suffix = f" {unit}" if unit else ""
        self._update_peak_annotations(frequency, amplitude, suffix)
        self._metric_fs.setText(
            f"有效采样率：{result['sample_rate_hz']:.3f} Hz")
        self._metric_n.setText(
            f"样本：{result['sample_count']} 点 · 分辨率："
            f"{result['resolution_hz']:.3f} Hz")
        fft_times = list(snapshot.get("fft_times", ()))
        if fft_times:
            self._metric_time.setText(
                f"FFT区间：t={fft_times[0]:.3f}～{fft_times[-1]:.3f} s · "
                f"记录长度 T=N/fs={result['duration_s']:.3f} s")
        else:
            self._metric_time.setText(
                f"FFT记录长度：{result['duration_s']:.3f} s")
        if analysis_kind == "dc":
            self._metric_fund.setText(
                f"主振荡：{result['fundamental_hz']:.3f} Hz")
            self._metric_amp.setText(
                f"平均值：{result['dc']:.4g}{suffix} · 峰峰："
                f"{result['peak_to_peak']:.4g}{suffix}")
            ripple = result["ripple_factor_percent"]
            self._metric_thd.setText(
                f"纹波率：{ripple:.2f}%" if math.isfinite(ripple)
                else "纹波率：--（平均值近零）")
            self._metric_rms.setText(
                f"纹波 RMS：{result['rms']:.4g}{suffix}")
            analysis_text = "直流量模式（不计算THD）"
        else:
            self._metric_fund.setText(
                f"基频：{result['fundamental_hz']:.3f} Hz")
            self._metric_amp.setText(
                f"基波峰值：{result['fundamental_amplitude']:.4g}{suffix}")
            thd = result["thd_percent"]
            self._metric_thd.setText(
                f"THD：{thd:.2f}%" if math.isfinite(thd) else "THD：--")
            self._metric_rms.setText(f"AC RMS：{result['rms']:.4g}{suffix}")
            analysis_text = "交流量模式（整数次谐波THD）"
        source_processing = snapshot.get("source_processing", "未声明")
        display_filter = snapshot.get(
            "display_filter", "未声明（FFT使用导入的原始列）")
        self._processing.setText(
            f"数据处理：{analysis_text}；{source_processing}；{display_filter}；"
            f"FFT窗：{result['window_text']}；"
            f"{'已减去' if result['remove_dc'] else '已保留'}"
            f"直流 DC={result['dc']:.4g}{suffix}。")

    def _show_input_waveform(self, label: str, snapshot: dict,
                             result: dict) -> None:
        """显示完整输入，并用亮线/阴影明确FFT实际采用的片段。"""
        all_times = list(snapshot.get("fft_all_times", ()))
        all_values = list(snapshot.get("fft_all_values", ()))
        fft_times = list(snapshot.get("fft_times", ()))
        fft_values = list(snapshot.get("fft_values", ()))
        self._input_full_curve.setData(all_times, all_values)
        self._input_selected_curve.setData(fft_times, fft_values)
        if fft_times:
            interval = 1.0 / max(float(result["sample_rate_hz"]), 1e-12)
            region_end = fft_times[-1] + interval
            self._updating_selection_region = True
            try:
                self._selection_region.setRegion(
                    [fft_times[0], region_end])
                self._selection_region.show()
            finally:
                self._updating_selection_region = False
            self._input_plot.setTitle(
                f"{label} · FFT输入时域波形（蓝色区间 "
                f"{fft_times[0]:.3f}～{region_end:.3f} s）")
        else:
            self._selection_region.hide()
            self._input_plot.setTitle(f"{label} · FFT输入时域波形")
        unit = str(snapshot.get("unit", ""))
        self._input_plot.setLabel("left", "输入幅值", units=unit or None)
        self._input_plot.enableAutoRange()

    def _update_peak_annotations(self, frequency, amplitude,
                                 suffix: str = "") -> None:
        """标出当前显示范围内幅值最大的局部峰，保存图片时一并保留。"""
        for item in getattr(self, "_peak_text_items", []):
            self._plot.removeItem(item)
        self._peak_text_items = []
        self._peak_markers.setData([], [])
        if not self._annotate_peaks.isChecked() or len(frequency) == 0:
            self._peak_summary.setText("峰值标注：已关闭")
            return
        candidates = []
        if len(amplitude) and frequency[0] == 0.0 and amplitude[0] > 0.0:
            candidates.append(0)
        for index in range(1, len(amplitude) - 1):
            if (amplitude[index] > 0.0 and
                    amplitude[index] >= amplitude[index - 1] and
                    amplitude[index] >= amplitude[index + 1]):
                candidates.append(index)
        if len(amplitude) == 1:
            candidates = [0]
        selected = sorted(
            candidates, key=lambda index: float(amplitude[index]),
            reverse=True)[:self._peak_count.value()]
        selected.sort(key=lambda index: float(frequency[index]))
        xs = [float(frequency[index]) for index in selected]
        ys = [float(amplitude[index]) for index in selected]
        self._peak_markers.setData(xs, ys)
        descriptions = []
        for x_value, y_value in zip(xs, ys):
            text = pg.TextItem(
                f"{x_value:.3f} Hz\n{y_value:.4g}{suffix}",
                color="#ffcc80", anchor=(0.5, 1.0))
            text.setPos(x_value, y_value)
            self._plot.addItem(text)
            self._peak_text_items.append(text)
            descriptions.append(
                f"{x_value:.3f} Hz / {y_value:.4g}{suffix}")
        self._peak_summary.setText(
            "峰值标注：" + ("；".join(descriptions) if descriptions else "未找到局部峰"))

    def _save_plot(self) -> None:
        if not _FFT_PLOT_OK:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存带标注频谱图", "FFT频谱.png", "PNG 图片 (*.png)")
        if path:
            self._plot_container.grab().save(path, "PNG")

    def _open_order_lms(self) -> None:
        try:
            label, snapshot = self._selected_snapshot()
            values = list(snapshot.get("values", ()))
            rate_hz = float(snapshot.get("sample_rate_hz", 0.0))
            times = list(snapshot.get("times", ()))
            if rate_hz <= 0.0:
                raise ValueError("当前信号没有有效采样率")
            if len(times) != len(values):
                times = [index / rate_hz for index in range(len(values))]
            if (self._use_plot_interval.isChecked()
                    and self._selection_region.isVisible()):
                region_start, region_end = sorted(
                    float(value) for value in self._selection_region.getRegion())
                selected_indices = [
                    index for index, time_value in enumerate(times)
                    if region_start <= float(time_value) < region_end
                ]
                if not selected_indices:
                    raise ValueError("蓝色区间内没有有效样本")
                first = selected_indices[0]
                last = selected_indices[-1] + 1
                values = values[first:last]
                times = times[first:last]
            else:
                count = int(self._points_combo.currentData() or 0)
                start = max(0, len(values) - count) if count > 0 else 0
                values = values[start:]
                times = times[start:]
            if len(values) < 32:
                raise ValueError("有效样本少于32点")
        except (ValueError, RuntimeError, KeyError) as exc:
            QMessageBox.warning(self, "LMS无法执行", str(exc))
            return
        if self._order_lms_dialog is not None:
            self._order_lms_dialog.close()
        self._order_lms_dialog = OrderLmsDialog(
            label, times, values, rate_hz,
            str(snapshot.get("unit", "")), self)
        self._order_lms_dialog.show()
        self._order_lms_dialog.run_analysis()
        self._order_lms_dialog.raise_()
        self._order_lms_dialog.activateWindow()

    def _load_csv_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载已保存波形", "", "CSV (*.csv);;所有文件 (*)")
        if path:
            self.load_csv(path)

    def load_csv(self, path: str) -> None:
        """加载监控页导出的长表 CSV，同时兼容旧的四列格式。"""
        grouped: dict[tuple[str, str], dict] = {}
        try:
            with open(path, encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    channel = str(row.get("channel", "")).strip()
                    series = str(row.get("series", "")).strip()
                    if not channel or not series:
                        continue
                    item = grouped.setdefault((channel, series), {
                        "times": [], "values": [],
                        "sample_rate_hz": 0.0,
                        "source_processing": row.get(
                            "source_filter", "CSV未记录采集滤波"),
                        "display_filter": row.get(
                            "display_filter", "FFT使用CSV原始列"),
                        "unit": row.get("unit", ""),
                        "analysis_kind": (
                            "ac" if channel == "phase_current" else "dc"),
                    })
                    item["times"].append(float(row["time_s"]))
                    item["values"].append(float(row["value"]))
                    saved_rate = row.get("sampling_rate_hz", "")
                    if saved_rate:
                        item["sample_rate_hz"] = float(saved_rate)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "CSV 加载失败", str(exc))
            return
        if not grouped:
            QMessageBox.warning(self, "CSV 加载失败", "未找到可分析的波形列")
            return

        source_path = Path(path)
        for (channel, series), item in grouped.items():
            if float(item["sample_rate_hz"]) <= 0.0:
                diffs = [b - a for a, b in zip(item["times"], item["times"][1:])
                         if b > a]
                if diffs:
                    ordered = sorted(diffs)
                    item["sample_rate_hz"] = 1.0 / ordered[len(ordered) // 2]
            key = f"{source_path.resolve()}::{channel}::{series}"
            self._loaded_sources[key] = item
            self._signal_combo.addItem(
                f"CSV · {channel} / {series}", ("loaded", key))
        self._signal_combo.setCurrentIndex(self._signal_combo.count() - len(grouped))
        self._processing.setText(
            f"已加载 {source_path.name}：{len(grouped)} 组信号。"
            "选择信号后点击“开始离线 FFT”。")
