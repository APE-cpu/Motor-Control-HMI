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
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
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


class FourierAnalysisPage(QWidget):
    """分析当前监控原始缓冲，或从“保存所有波形”CSV读取数据。"""

    def __init__(self, snapshot_provider: Callable[[str], dict] | None = None,
                 source_items: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self._snapshot_provider = snapshot_provider
        self._loaded_sources: dict[str, dict] = {}
        self._source_items = source_items or []

        root = QVBoxLayout(self)
        title = QLabel("离线傅里叶分析")
        title.setObjectName("TitleLabel")
        root.addWidget(title)
        intro = QLabel(
            "对已采集的原始缓冲或已保存 CSV 做 FFT；不在实时刷新线程中计算。"
            "分析使用原始点，不使用监控页的“显示平滑”结果。")
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
        buttons.addStretch(1)
        grid.addLayout(buttons, 4, 0, 1, 4)
        root.addWidget(controls)

        metrics = QGroupBox("分析结果")
        metric_grid = QGridLayout(metrics)
        self._metric_fs = QLabel("采样率：--")
        self._metric_n = QLabel("样本/分辨率：--")
        self._metric_fund = QLabel("基频：--")
        self._metric_amp = QLabel("基波幅值：--")
        self._metric_thd = QLabel("THD：--")
        self._metric_rms = QLabel("AC RMS：--")
        for index, widget in enumerate((
                self._metric_fs, self._metric_n, self._metric_fund,
                self._metric_amp, self._metric_thd, self._metric_rms)):
            widget.setAlignment(Qt.AlignCenter)
            metric_grid.addWidget(widget, index // 3, index % 3)
        root.addWidget(metrics)

        self._processing = QLabel("数据处理：等待选择数据")
        self._processing.setStyleSheet("color:#80cbc4;")
        self._processing.setWordWrap(True)
        root.addWidget(self._processing)

        if _FFT_PLOT_OK:
            self._plot = pg.PlotWidget(title="单边幅频谱（去直流）")
            self._plot.setBackground("#10131a")
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
            root.addWidget(self._plot, 1)
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
            values = list(snapshot.get("values", ()))
            count = int(self._points_combo.currentData() or 0)
            if count > 0:
                values = values[-count:]
            result = compute_spectrum(
                values, float(snapshot.get("sample_rate_hz", 0.0)),
                str(self._window_combo.currentData()),
                self._remove_dc.isChecked())
        except (ValueError, RuntimeError, KeyError) as exc:
            QMessageBox.warning(self, "FFT 无法执行", str(exc))
            return
        analysis_kind = str(self._analysis_combo.currentData())
        if analysis_kind == "auto":
            analysis_kind = str(snapshot.get("analysis_kind", "dc"))
        self._show_result(label, snapshot, result, analysis_kind)

    def _show_result(self, label: str, snapshot: dict, result: dict,
                     analysis_kind: str = "ac") -> None:
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
        self._metric_fs.setText(f"采样率：{result['sample_rate_hz']:.3f} Hz")
        self._metric_n.setText(
            f"样本：{result['sample_count']} 点 · 分辨率："
            f"{result['resolution_hz']:.3f} Hz")
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
            self._plot.grab().save(path, "PNG")

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
