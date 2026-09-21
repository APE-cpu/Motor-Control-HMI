"""Export the complete d/q seven-coefficient RLS replay as one figure."""

from __future__ import annotations

import argparse
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
    axis.set_xlabel("时间 / s")


def render(csv_path: Path, output_path: Path) -> Path:
    _configure_matplotlib()
    import matplotlib.pyplot as plt
    import numpy as np

    dataset = load_rls_capture_csv(csv_path)
    analysis = run_offline_rls_analysis(dataset)
    results = list(analysis.get("results", ()))
    if not results:
        raise RuntimeError("离线重放没有产生RLS结果")

    ticks = [int(item.get("tick_ms", 0)) for item in results]
    if ticks and any(value != ticks[0] for value in ticks[1:]):
        time_s = np.asarray(
            [((tick - ticks[0]) & 0xFFFFFFFF) / 1000.0 for tick in ticks])
    else:
        time_s = np.arange(len(results), dtype=float) * 0.1
    theta_d = np.asarray([item["theta_d"] for item in results], dtype=float)
    theta_q = np.asarray([item["theta_q"] for item in results], dtype=float)

    colors = (
        "#40bdf4", "#7bd389", "#d786e8", "#f6ad55",
        "#f58b70", "#68c7bd", "#ea79ad",
    )
    fig, axes = plt.subplots(2, 2, figsize=(15.8, 9.8))
    fig.subplots_adjust(
        left=0.065, right=0.97, top=0.86, bottom=0.13,
        hspace=0.34, wspace=0.18)

    groups = (
        (axes[0, 0], theta_d, range(3),
         ("a1_d", "a2_d", "a3_d"), "d轴：三个电流历史系数", "无量纲"),
        (axes[0, 1], theta_q, range(3),
         ("a1_q", "a2_q", "a3_q"), "q轴：三个电流历史系数", "无量纲"),
        (axes[1, 0], theta_d, range(3, 7),
         ("b_dd0", "b_dd1", "b_dq0", "b_dq1"),
         "d轴：四个延迟电压系数", "A/V"),
        (axes[1, 1], theta_q, range(3, 7),
         ("b_qd0", "b_qd1", "b_qq0", "b_qq1"),
         "q轴：四个延迟电压系数", "A/V"),
    )
    for axis, matrix, indices, names, title, ylabel in groups:
        for index, name in zip(indices, names):
            axis.plot(
                time_s, matrix[:, index], color=colors[index], linewidth=1.6,
                label=f"{name}  末值={matrix[-1, index]:.6g}")
        axis.set_title(title, fontsize=13, pad=9)
        axis.set_ylabel(ylabel)
        axis.legend(loc="best", frameon=False, fontsize=9, ncol=2)
        _style_axis(axis)

    fig.suptitle(
        "旧真机 CSV → C++ ESO/三阶 RLS：完整 7×2 系数收敛曲线",
        fontsize=18, fontweight="bold", y=0.965)
    fig.text(
        0.5, 0.92,
        "θd=[a1d,a2d,a3d,bdd0,bdd1,bdq0,bdq1]；"
        "θq=[a1q,a2q,a3q,bqd0,bqd1,bqq0,bqq1]",
        ha="center", fontsize=11, color="#bcd3e6")
    fig.text(
        0.5, 0.055,
        "这些是 ARX 回归系数，不是 R、Ld、Lq 的直接数值。"
        "本重放严格对应 R2024b 仿真 ESO（2.34 Ω / 19.36 mH / 100 kHz），"
        "对当前 F407 真机只能检查数值复现，不能据此宣称物理参数收敛。",
        ha="center", fontsize=10.5, color="#f0c96a")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=170, pad_inches=0.18)
    plt.close(fig)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制完整RLS 7×2系数")
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        output = render(args.csv, args.output)
    except Exception as exc:
        print(f"RLS coefficient plotting failed: {exc}", file=sys.stderr)
        return 2
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
