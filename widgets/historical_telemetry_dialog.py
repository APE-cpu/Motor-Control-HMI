"""历史实验遥测曲线与基础统计对话框。"""
from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from experiments.telemetry import summarize_telemetry


_PLOTS = (
    ("转速", "rpm", "speed_actual", "speed_target"),
    ("电流", "A", "current_actual", "current_target"),
    ("转矩", "N·m", "torque_actual", "torque_target"),
    ("直流母线", "V", "vdc", None),
    ("温度", "°C", "temperature", None),
)
_METRIC_NAMES = {
    "speed_actual": "实际转速 (rpm)",
    "current_actual": "实际电流 (A)",
    "torque_actual": "实际转矩 (N·m)",
    "vdc": "直流母线 (V)",
    "temperature": "温度 (°C)",
}


class HistoricalTelemetryDialog(QDialog):
    def __init__(self, experiment_id: str, rows: list[dict],
                 events: list[dict] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"历史遥测 - {experiment_id}")
        self.resize(1050, 720)
        self.summary = summarize_telemetry(rows)
        self.timeline_events = _timeline_events(events or [])
        self.marker_line_count = 0

        root = QVBoxLayout(self)
        head = QLabel(
            f"实验：{experiment_id}　样本：{self.summary['samples']}　"
            f"时长：{self.summary['duration_s']:.3f} s"
        )
        head.setObjectName("TitleLabel")
        root.addWidget(head)

        tabs = QTabWidget()
        tabs.addTab(self._build_curves(rows), "遥测曲线")
        tabs.addTab(self._build_statistics(), "基础统计")
        tabs.addTab(self._build_events(), f"事件时间线（{len(self.timeline_events)}）")
        root.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_curves(self, rows: list[dict]) -> QWidget:
        tabs = QTabWidget()
        plot_rows = _decimate(rows, 3000)
        x = [self._x_value(row, index) for index, row in enumerate(plot_rows)]
        for title, unit, actual_key, target_key in _PLOTS:
            plot = pg.PlotWidget()
            plot.setBackground("#10131a")
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setLabel("bottom", "时间", units="s")
            plot.setLabel("left", title, units=unit)
            actual = [_plot_number(row.get(actual_key)) for row in plot_rows]
            plot.plot(x, actual, pen=pg.mkPen("#4fc3f7", width=2), name="实际值")
            if target_key:
                target = [_plot_number(row.get(target_key)) for row in plot_rows]
                plot.plot(x, target, pen=pg.mkPen("#ffa726", width=1.5,
                                                  style=Qt.DashLine),
                          name="给定值")
            plot.addLegend()
            for event in self.timeline_events:
                color = _MARKER_COLORS.get(
                    event.get("details", {}).get("category"), "#fdd835")
                plot.addItem(pg.InfiniteLine(
                    pos=float(event["monotonic_s"]), angle=90,
                    movable=False, pen=pg.mkPen(color, width=1.4,
                                                style=Qt.DashLine)))
                self.marker_line_count += 1
            tabs.addTab(plot, title)
        return tabs

    def _build_events(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        table = QTableWidget(len(self.timeline_events), 4)
        table.setHorizontalHeaderLabels(["相对时间(s)", "类别", "事件", "备注"])
        for row, event in enumerate(self.timeline_events):
            details = event.get("details", {})
            values = (
                f"{float(event['monotonic_s']):.6g}",
                str(details.get("category", "")),
                str(event.get("message", "")),
                str(details.get("note", "")),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        layout.addWidget(table)
        return page

    def _build_statistics(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        table = QTableWidget(len(_METRIC_NAMES), 7)
        table.setHorizontalHeaderLabels(
            ["物理量", "有效点", "最小值", "最大值", "均值", "RMS", "峰峰值"])
        for row, (field, name) in enumerate(_METRIC_NAMES.items()):
            metric = self.summary["metrics"][field]
            values = [name, str(metric.get("count", 0))]
            for key in ("min", "max", "mean", "rms", "peak_to_peak"):
                value = metric.get(key)
                values.append("—" if value is None else f"{value:.6g}")
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        layout.addWidget(table)
        mae = self.summary.get("speed_mae_rpm")
        layout.addWidget(QLabel(
            "转速平均绝对跟踪误差：" + ("—" if mae is None else f"{mae:.6g} rpm")))
        return page

    @staticmethod
    def _x_value(row: dict, fallback: int) -> float:
        value = row.get("monotonic_s")
        try:
            number = float(value)
            return number if math.isfinite(number) else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)


def _plot_number(value) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else math.nan
    except (TypeError, ValueError):
        return math.nan


def _decimate(rows: list[dict], max_points: int) -> list[dict]:
    if len(rows) <= max_points:
        return rows
    last = len(rows) - 1
    return [rows[round(i * last / (max_points - 1))] for i in range(max_points)]


_MARKER_COLORS = {
    "target_changed": "#42a5f5", "load_applied": "#fb8c00",
    "load_removed": "#66bb6a", "parameter_changed": "#26a69a",
    "oscillation": "#ab47bc",
    "abnormal_sound": "#ec407a", "protection": "#ef5350",
    "custom": "#fdd835",
}


def _timeline_events(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        if event.get("type") != "experiment_marker":
            continue
        try:
            value = float(event.get("monotonic_s"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            item = dict(event)
            item["monotonic_s"] = value
            result.append(item)
    return sorted(result, key=lambda item: item["monotonic_s"])
