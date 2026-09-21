"""Compare raw motor-current data with two offline low-pass filters.

The script is intentionally read-only with respect to the capture.  It writes
one PNG and one text summary under ``artifacts/filter_fft``.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path

import numpy as np


def _load_columns(path: Path) -> dict[str, np.ndarray]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream))
    wanted = ("time_s", "rate_hz", "speed_rpm", "iq_a", "ia_a", "ib_a")
    missing = [name for name in wanted if name not in header]
    if missing:
        raise ValueError(f"CSV 缺少列: {', '.join(missing)}")
    indices = tuple(header.index(name) for name in wanted)
    values = np.loadtxt(path, delimiter=",", skiprows=1, usecols=indices)
    return {name: values[:, index] for index, name in enumerate(wanted)}


def _one_pole_causal(x: np.ndarray, alpha: float) -> np.ndarray:
    """Firmware-equivalent y[n]=alpha*x[n]+(1-alpha)*y[n-1]."""
    y = np.empty_like(x)
    y[0] = x[0]
    beta = 1.0 - alpha
    for index in range(1, x.size):
        y[index] = alpha * x[index] + beta * y[index - 1]
    return y


def _zero_phase_butterworth_magnitude(
        x: np.ndarray, fs: float, cutoff_hz: float, order: int = 4,
) -> np.ndarray:
    """Offline zero-phase Butterworth-magnitude low-pass.

    This is a diagnostic/display filter, not a causal controller filter.  The
    smooth magnitude response avoids the ringing of a hard FFT brick wall.
    """
    centered = np.asarray(x, dtype=float) - float(np.mean(x))
    spectrum = np.fft.rfft(centered)
    frequency = np.fft.rfftfreq(centered.size, 1.0 / fs)
    response = 1.0 / np.sqrt(1.0 + (frequency / cutoff_hz) ** (2 * order))
    return np.fft.irfft(spectrum * response, n=x.size) + float(np.mean(x))


def _spectrum(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    centered = np.asarray(x, dtype=float) - float(np.mean(x))
    window = np.hanning(centered.size)
    amplitude = np.abs(np.fft.rfft(centered * window)) * (2.0 / window.sum())
    frequency = np.fft.rfftfreq(centered.size, 1.0 / fs)
    return frequency, amplitude


def _band_rms(frequency: np.ndarray, amplitude: np.ndarray,
              low_hz: float, high_hz: float) -> float:
    mask = (frequency >= low_hz) & (frequency < high_hz)
    if not np.any(mask):
        return float("nan")
    # One-sided sine peak amplitudes: RMS power is sum(A_peak^2 / 2).
    return float(np.sqrt(np.sum(amplitude[mask] ** 2) / 2.0))


def _local_peak(frequency: np.ndarray, amplitude: np.ndarray,
                low_hz: float, high_hz: float) -> tuple[float, float]:
    mask = (frequency >= low_hz) & (frequency <= high_hz)
    if not np.any(mask):
        return float("nan"), float("nan")
    local = np.flatnonzero(mask)
    index = int(local[np.argmax(amplitude[mask])])
    return float(frequency[index]), float(amplitude[index])


def _top_local_peaks(frequency: np.ndarray, amplitude: np.ndarray,
                     low_hz: float, high_hz: float,
                     count: int = 4) -> list[tuple[float, float]]:
    band = np.flatnonzero((frequency >= low_hz) & (frequency <= high_hz))
    if band.size < 3:
        return []
    centers = band[1:-1]
    peaks = centers[(amplitude[centers] > amplitude[band[:-2]]) &
                    (amplitude[centers] > amplitude[band[2:]])]
    if peaks.size == 0:
        return []
    selected = peaks[np.argsort(amplitude[peaks])[-count:][::-1]]
    return [(float(frequency[index]), float(amplitude[index]))
            for index in selected]


def _metrics(raw: np.ndarray, firmware: np.ndarray, diagnostic: np.ndarray,
             fs: float) -> dict[str, object]:
    series = {"raw": raw, "firmware_2p5khz": firmware,
              "offline_1khz": diagnostic}
    spectra = {name: _spectrum(value, fs) for name, value in series.items()}
    result: dict[str, object] = {}
    for name, value in series.items():
        frequency, amplitude = spectra[name]
        result[name] = {
            "mean": float(np.mean(value)),
            "ac_rms": float(np.std(value)),
            "peak_to_peak": float(np.ptp(value)),
            "band_rms_1_8khz": _band_rms(frequency, amplitude, 1000.0, 8000.1),
            "peak_20_45hz": _local_peak(frequency, amplitude, 20.0, 45.0),
            "peak_80_120hz": _local_peak(frequency, amplitude, 80.0, 120.0),
            "peak_550_750hz": _local_peak(frequency, amplitude, 550.0, 750.0),
            "top_peaks_550_750hz": _top_local_peaks(
                frequency, amplitude, 550.0, 750.0),
        }
    return result


def _render(output: Path, time_s: np.ndarray, fs: float,
            signals: dict[str, dict[str, np.ndarray]],
            metrics: dict[str, object], start_s: float, end_s: float) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    import pyqtgraph as pg
    from pyqtgraph.exporters import ImageExporter

    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\msyh.ttc")
    font_families = QFontDatabase.applicationFontFamilies(font_id)
    ui_font = QFont(font_families[0] if font_families else "Microsoft YaHei UI", 10)
    app.setFont(ui_font)
    pg.setConfigOptions(antialias=True, background="#0f141c", foreground="#cbd5e1")
    canvas = pg.GraphicsLayoutWidget()
    canvas.resize(2400, 1280)
    canvas.ci.layout.setRowStretchFactor(2, 12)
    canvas.ci.layout.setRowStretchFactor(3, 12)
    title = pg.LabelItem(
        "16 kHz 真机电流：原始数据与低通滤波对比",
        color="#f8fafc", size="18pt", bold=True)
    canvas.addItem(title, row=0, col=0, colspan=3)
    subtitle = pg.LabelItem(
        f"稳态分析 {start_s:.1f}–{end_s:.1f} s；FFT 使用 Hann 窗并去均值。"
        "蓝：原始；橙：固件等效一阶 IIR 2.5 kHz；绿：离线零相位 4 阶低通 1.0 kHz",
        color="#94a3b8", size="10pt")
    canvas.addItem(subtitle, row=1, col=0, colspan=3)

    colors = {"raw": "#38bdf8", "firmware": "#fb923c", "diagnostic": "#4ade80"}
    names = {"raw": "原始", "firmware": "固件等效 IIR 2.5 kHz",
             "diagnostic": "离线低通 1.0 kHz"}
    wave_mask = (time_s >= 45.0) & (time_s <= 45.20)
    if not np.any(wave_mask):
        center = (start_s + end_s) / 2.0
        wave_mask = (time_s >= center) & (time_s <= center + 0.20)

    for row, (key, label) in enumerate((("ia", "相电流 Ia"), ("iq", "q 轴电流 Iq")), start=2):
        wave = canvas.addPlot(row=row, col=0, title=f"{label}：200 ms 波形")
        wave.setMinimumHeight(430)
        wave.setMinimumWidth(720)
        wave.showGrid(x=True, y=True, alpha=0.16)
        wave.setLabel("bottom", "时间", units="s")
        wave.setLabel("left", label, units="A")
        wave.addLegend(offset=(8, 8), brush=QColor(15, 20, 28, 210))
        for series_key in ("raw", "firmware", "diagnostic"):
            wave.plot(time_s[wave_mask], signals[key][series_key][wave_mask],
                      pen=pg.mkPen(colors[series_key], width=1.35),
                      name=names[series_key])

        spectrum = canvas.addPlot(row=row, col=1, title=f"{label}：0–1 kHz FFT")
        spectrum.setMinimumHeight(430)
        spectrum.setMinimumWidth(720)
        spectrum.showGrid(x=True, y=True, alpha=0.16)
        spectrum.setLabel("bottom", "频率", units="Hz")
        spectrum.setLabel("left", "单边峰值幅值（对数）", units="A")
        spectrum.setXRange(0.0, 1000.0, padding=0.0)
        spectrum.setLogMode(y=True)
        spectrum.addLegend(offset=(8, 8), brush=QColor(15, 20, 28, 210))
        for series_key in ("raw", "firmware", "diagnostic"):
            frequency, amplitude = _spectrum(signals[key][series_key], fs)
            visible = frequency <= 1000.0
            spectrum.plot(frequency[visible], amplitude[visible],
                          pen=pg.mkPen(colors[series_key], width=1.35),
                          name=names[series_key])
        for target in (32.0, 99.0, 650.0):
            line = pg.InfiniteLine(
                target, angle=90,
                pen=pg.mkPen("#64748b", width=1, style=Qt.PenStyle.DashLine))
            spectrum.addItem(line)

        full = canvas.addPlot(row=row, col=2, title=f"{label}：0–8 kHz FFT")
        full.setMinimumHeight(430)
        full.setMinimumWidth(720)
        full.showGrid(x=True, y=True, alpha=0.16)
        full.setLabel("bottom", "频率", units="Hz")
        full.setLabel("left", "单边峰值幅值（对数）", units="A")
        full.setXRange(0.0, fs / 2.0, padding=0.0)
        full.setLogMode(y=True)
        full.addLegend(offset=(8, 8), brush=QColor(15, 20, 28, 210))
        for series_key in ("raw", "firmware", "diagnostic"):
            frequency, amplitude = _spectrum(signals[key][series_key], fs)
            full.plot(frequency, amplitude,
                      pen=pg.mkPen(colors[series_key], width=1.15),
                      name=names[series_key])
        for target in (1000.0, 2500.0):
            full.addItem(pg.InfiniteLine(
                target, angle=90,
                pen=pg.mkPen("#64748b", width=1,
                             style=Qt.PenStyle.DashLine)))

    footer = pg.LabelItem(
        "说明：1 kHz 结果仅用于离线观察，不参与电机控制。16 点箱式平均未采用，"
        "因为它会显著压低 650–700 Hz 成分并干扰诊断。",
        color="#fbbf24", size="10pt")
    canvas.addItem(footer, row=4, col=0, colspan=3)
    for plot in canvas.ci.items.values():
        if hasattr(plot, "getAxis"):
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setTextPen("#aebdd0")
                axis.setPen("#526173")
                axis.setStyle(tickFont=QFont(ui_font.family(), 9))

    canvas.show()
    app.processEvents()
    exporter = ImageExporter(canvas.scene())
    exporter.parameters()["width"] = 2400
    exporter.export(str(output))
    canvas.close()
    app.processEvents()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("artifacts/filter_fft"))
    args = parser.parse_args()

    columns = _load_columns(args.csv)
    fs = float(np.median(columns["rate_hz"]))
    if not 15000.0 <= fs <= 17000.0:
        raise ValueError(f"期望约 16 kHz，实际 {fs:g} Hz")
    time_s = columns["time_s"]
    end_s = float(time_s[-1])
    analysis_end = end_s
    analysis_start = max(float(time_s[0]), analysis_end - 20.0)
    mask = (time_s >= analysis_start) & (time_s <= analysis_end)
    analysis_time = time_s[mask]
    alpha = 20491.0 / 32768.0
    signals: dict[str, dict[str, np.ndarray]] = {}
    all_metrics: dict[str, object] = {
        "source": str(args.csv), "sample_rate_hz": fs,
        "analysis_start_s": analysis_start, "analysis_end_s": analysis_end,
        "sample_count": int(mask.sum()),
        "mean_speed_rpm": float(np.mean(columns["speed_rpm"][mask])),
        "filter_firmware": {
            "type": "causal one-pole IIR",
            "alpha_q15": 20491,
            "alpha": alpha,
            "equivalent_cutoff_hz": -fs / (2.0 * math.pi) * math.log(1.0 - alpha),
        },
        "filter_offline": {
            "type": "zero-phase Butterworth magnitude",
            "order": 4, "cutoff_hz": 1000.0,
        },
    }
    for key, column in (("ia", "ia_a"), ("iq", "iq_a")):
        raw = np.asarray(columns[column][mask], dtype=float)
        firmware = _one_pole_causal(raw, alpha)
        diagnostic = _zero_phase_butterworth_magnitude(raw, fs, 1000.0, 4)
        signals[key] = {"raw": raw, "firmware": firmware,
                        "diagnostic": diagnostic}
        all_metrics[key] = _metrics(raw, firmware, diagnostic, fs)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = args.output_dir / "filter_fft_comparison.png"
    summary_path = args.output_dir / "filter_fft_metrics.json"
    _render(image_path, analysis_time, fs, signals, all_metrics,
            analysis_start, analysis_end)
    summary_path.write_text(json.dumps(all_metrics, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(image_path.resolve())
    print(summary_path.resolve())
    print(json.dumps(all_metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
