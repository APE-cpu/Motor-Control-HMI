"""Render an honest summary figure for a saved legacy RLS capture.

The old CSV schema does not contain direct Id/IdRef or a sample sequence, so
the figure deliberately presents the quasi-steady command-frame fits as
diagnostic evidence rather than as validated Ld/Lq identification.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from communications.native_telemetry import run_offline_rls_analysis
from rls_offline import load_rls_capture_csv


def _configure_matplotlib():
    import matplotlib as mpl

    mpl.use("Agg")
    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [
            "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans",
        ],
        "axes.unicode_minus": False,
        "figure.facecolor": "#10151d",
        "axes.facecolor": "#111823",
        "savefig.facecolor": "#10151d",
        "text.color": "#dbe7f2",
        "axes.labelcolor": "#b8c9d9",
        "axes.edgecolor": "#627386",
        "xtick.color": "#a9bacb",
        "ytick.color": "#a9bacb",
        "grid.color": "#607080",
        "grid.alpha": 0.22,
        "grid.linewidth": 0.7,
    })


def _style_axis(axis) -> None:
    axis.grid(True, which="major")
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def render(csv_path: Path, output_path: Path) -> Path:
    _configure_matplotlib()
    import matplotlib.pyplot as plt
    import numpy as np

    dataset = load_rls_capture_csv(csv_path)
    analysis = run_offline_rls_analysis(dataset)
    evidence = dict(analysis.get("legacy_evidence", {}))
    fits = list(evidence.get("fits", ()))
    if not fits:
        raise RuntimeError("该CSV没有可绘制的旧格式诊断结果")

    count = int(dataset["count"])
    rate_hz = int(dataset["rate_hz"])
    stride = max(1, math.ceil(count / 12000))
    sample = np.arange(0, count, stride)
    time_s = sample / float(rate_hz)
    speed_rpm = np.asarray(dataset["speed_rpm"], dtype=float)[sample]
    iq_a = np.asarray(dataset["iq_a"], dtype=float)[sample]
    iqref_a = np.asarray(dataset["iqref_a"], dtype=float)[sample]

    labels = [
        f"{item['block_ms']:.0f} ms\n起点 {item['start_s']:.0f} s"
        for item in fits
    ]
    resistance = np.asarray(
        [item["resistance_ohm"] for item in fits], dtype=float)
    inductance = np.asarray(
        [item["shared_inductance_mh"] for item in fits], dtype=float)
    flux_mwb = 1e3 * np.asarray(
        [item["flux_wb"] for item in fits], dtype=float)

    fig = plt.figure(figsize=(15.8, 10.2))
    grid = fig.add_gridspec(
        2, 3, height_ratios=(1.12, 1.0), left=0.065, right=0.95,
        top=0.86, bottom=0.16, hspace=0.34, wspace=0.22)
    operating = fig.add_subplot(grid[0, :])
    axes = [fig.add_subplot(grid[1, column]) for column in range(3)]

    cyan = "#40bdf4"
    orange = "#f6ad55"
    pink = "#e986c8"
    nominal = "#dfe8f1"
    warning = "#f0c96a"

    operating.plot(time_s, speed_rpm, color=cyan, linewidth=1.4,
                   label="转速")
    operating.set_title("旧真机 CSV 的 60 s 实验工况", fontsize=14, pad=11)
    operating.set_xlabel("时间 / s")
    operating.set_ylabel("转速 / rpm", color=cyan)
    operating.tick_params(axis="y", colors=cyan)
    _style_axis(operating)
    current_axis = operating.twinx()
    current_axis.plot(time_s, iq_a, color=orange, linewidth=1.0, alpha=0.9,
                      label="Iq 实际")
    current_axis.plot(time_s, iqref_a, color=pink, linewidth=1.0,
                      linestyle="--", alpha=0.9, label="Iq 给定")
    current_axis.set_ylabel("q轴电流 / A", color=orange)
    current_axis.tick_params(axis="y", colors=orange)
    current_axis.spines["top"].set_visible(False)
    lines = operating.lines + current_axis.lines
    operating.legend(lines, [line.get_label() for line in lines], ncol=3,
                     loc="upper left", frameon=False)

    specs = (
        (axes[0], resistance, "电阻估计 R", "Ω", 0.59, cyan),
        (axes[1], inductance, "共享电感估计 L", "mH", 0.66, orange),
        (axes[2], flux_mwb, "永磁磁链估计 ψf", "mWb", 5.85, pink),
    )
    x = np.arange(len(labels))
    for axis, values, title, unit, reference, color in specs:
        axis.plot(x, values, color=color, marker="o", markersize=7,
                  linewidth=1.8, label="旧数据诊断值")
        axis.axhline(reference, color=nominal, linewidth=1.25,
                     linestyle="--", label=f"真机标称 {reference:g} {unit}")
        axis.set_title(title, fontsize=13, pad=9)
        axis.set_ylabel(unit)
        axis.set_xticks(x, labels, fontsize=8)
        _style_axis(axis)
        axis.legend(loc="best", frameon=False, fontsize=9)
        for index, value in enumerate(values):
            axis.annotate(f"{value:.3f}", (index, value),
                          xytext=(0, 7), textcoords="offset points",
                          ha="center", color="#dbe7f2", fontsize=8)

    r_median = float(evidence["resistance_ohm_median"])
    l_median = float(evidence["shared_inductance_mh_median"])
    flux_median = 1e3 * float(evidence["flux_wb_median"])
    fig.suptitle(
        "旧真机数据离线辨识：可用证据与可信边界",
        fontsize=19, fontweight="bold", y=0.975)
    fig.text(
        0.5, 0.935,
        f"诊断中位数：R={r_median:.3f} Ω，共享 L={l_median:.3f} mH，"
        f"ψf={flux_median:.3f} mWb  ｜  Park重构相关系数="
        f"{evidence['park_reconstruction']['correlation']:.6f}",
        ha="center", va="top", fontsize=11, color="#bcd3e6")
    fig.text(
        0.5, 0.052,
        "结论：R 接近标称值；共享 L 与 0.66 mH 仍有明显偏差。",
        ha="center", va="bottom", fontsize=10.5, color=warning)
    fig.text(
        0.5, 0.024,
        "旧CSV缺少直接 Id/IdRef、连续样本序号和端电压实测，不能分离 Ld/Lq；"
        "因此本图是诊断证据，不是物理辨识通过。",
        ha="center", va="bottom", fontsize=10.5, color=warning)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=170, pad_inches=0.18)
    plt.close(fig)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制旧格式RLS离线诊断结果")
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        output = render(args.csv, args.output)
    except Exception as exc:
        print(f"RLS evidence plotting failed: {exc}", file=sys.stderr)
        return 2
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
