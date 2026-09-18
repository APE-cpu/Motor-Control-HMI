"""训练页的特征工程与分析面板。"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from training.feature_engineering import (
    FeatureAnalysis, FeatureEngineeringConfig, FeatureEngineeringResult,
)

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:
    _PG_OK = False


class FeatureAnalysisPanel(QWidget):
    buildRequested = Signal()
    exportRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._result: FeatureEngineeringResult | None = None
        self._analysis: FeatureAnalysis | None = None

        root = QVBoxLayout(self)
        controls = QGroupBox("滑窗与预处理")
        row = QHBoxLayout(controls)
        form = QFormLayout()
        self.sample_rate = QDoubleSpinBox()
        self.sample_rate.setRange(0.1, 100000.0)
        self.sample_rate.setDecimals(1)
        self.sample_rate.setValue(10.0)
        self.sample_rate.setSuffix(" Hz")
        self.window_size = QSpinBox()
        self.window_size.setRange(8, 65536)
        self.window_size.setValue(32)
        self.hop_size = QSpinBox()
        self.hop_size.setRange(1, 65536)
        self.hop_size.setValue(8)
        self.label_mode = QComboBox()
        self.label_mode.addItem("窗口最大严重度", "max")
        self.label_mode.addItem("窗口多数标签", "majority")
        self.label_mode.addItem("窗口标签均值", "mean")
        form.addRow("原始采样率", self.sample_rate)
        form.addRow("窗口点数", self.window_size)
        form.addRow("滑动步长", self.hop_size)
        form.addRow("窗口标签", self.label_mode)
        row.addLayout(form)

        options = QVBoxLayout()
        self.time_features = QCheckBox("时域特征")
        self.time_features.setChecked(True)
        self.frequency_features = QCheckBox("频域特征")
        self.frequency_features.setChecked(True)
        self.robust_clip = QCheckBox("训练段0.5%~99.5%鲁棒裁剪")
        self.robust_clip.setChecked(True)
        self.deduplicate = QCheckBox("删除相邻完全重复点")
        self.deduplicate.setChecked(False)
        self.deduplicate.setToolTip(
            "时序特征通常不应去重；仅确认重复点来自通信重发时启用")
        for widget in (self.time_features, self.frequency_features,
                       self.robust_clip, self.deduplicate):
            options.addWidget(widget)
        options.addStretch(1)
        row.addLayout(options)

        actions = QVBoxLayout()
        self.build_button = QPushButton("生成特征并分析")
        self.build_button.setObjectName("PrimaryButton")
        self.build_button.clicked.connect(self.buildRequested)
        self.export = QPushButton("导出特征 CSV")
        self.export.setEnabled(False)
        self.export.clicked.connect(self.exportRequested)
        actions.addWidget(self.build_button)
        actions.addWidget(self.export)
        actions.addStretch(1)
        row.addLayout(actions)
        root.addWidget(controls)

        self.status = QLabel(
            "尚未生成。建议：常规遥测填10 Hz；16 kHz电流数据填16000 Hz。"
            "频率上限为采样率的一半。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#8fa3b8;")
        root.addWidget(self.status)

        tabs = QTabWidget()
        tabs.addTab(self._build_summary_tab(), "特征统计")
        tabs.addTab(self._build_importance_tab(), "特征重要性")
        tabs.addTab(self._build_correlation_tab(), "特征相关性")
        tabs.addTab(self._build_distribution_tab(), "特征分布")
        root.addWidget(tabs, 1)

    def config(self, train_fraction: float) -> FeatureEngineeringConfig:
        return FeatureEngineeringConfig(
            sample_rate_hz=float(self.sample_rate.value()),
            window_size=int(self.window_size.value()),
            hop_size=int(self.hop_size.value()),
            include_time=self.time_features.isChecked(),
            include_frequency=self.frequency_features.isChecked(),
            robust_clip=self.robust_clip.isChecked(),
            deduplicate_adjacent=self.deduplicate.isChecked(),
            label_mode=str(self.label_mode.currentData()),
            train_fraction=float(train_fraction),
        )

    def clear_result(self) -> None:
        self._result = None
        self._analysis = None
        self.export.setEnabled(False)
        self.status.setText("原始数据已变化，请重新生成特征。")

    def set_busy(self, busy: bool) -> None:
        self.build_button.setEnabled(not busy)
        self.export.setEnabled(not busy and self._result is not None)
        if busy:
            self.status.setText(
                "正在后台执行预处理、滑窗特征提取和重要性分析…")

    def _build_summary_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.summary_table = QTableWidget(0, 7)
        self.summary_table.setHorizontalHeaderLabels([
            "特征", "重要性", "与标签相关", "均值", "标准差", "最小值", "最大值"])
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.summary_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        for index in range(1, 7):
            self.summary_table.horizontalHeader().setSectionResizeMode(
                index, QHeaderView.ResizeToContents)
        layout.addWidget(self.summary_table)
        return widget

    def _build_importance_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.importance_note = QLabel("尚未分析")
        self.importance_note.setStyleSheet("color:#80cbc4;")
        layout.addWidget(self.importance_note)
        if _PG_OK:
            self.importance_plot = pg.PlotWidget()
            self.importance_plot.setBackground("#10131a")
            self.importance_plot.showGrid(x=True, y=False, alpha=0.25)
            self.importance_plot.setLabel("bottom", "Importance")
            layout.addWidget(self.importance_plot, 1)
        else:
            self.importance_plot = None
            layout.addWidget(QLabel("未安装 pyqtgraph，重要性数值仍可在统计表查看。"))
        return widget

    def _build_correlation_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        note = QLabel("显示重要性最高特征在训练段内的 Pearson 相关系数；"
                      "高相关不代表因果，也提示可能存在冗余特征。")
        note.setStyleSheet("color:#8fa3b8;")
        layout.addWidget(note)
        self.correlation_table = QTableWidget(0, 0)
        self.correlation_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.correlation_table.horizontalHeader().setDefaultSectionSize(92)
        self.correlation_table.verticalHeader().setDefaultSectionSize(28)
        layout.addWidget(self.correlation_table, 1)
        return widget

    def _build_distribution_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("特征"))
        self.distribution_feature = QComboBox()
        self.distribution_feature.currentIndexChanged.connect(
            self._refresh_distribution)
        bar.addWidget(self.distribution_feature, 1)
        self.distribution_stats = QLabel("--")
        self.distribution_stats.setStyleSheet("color:#80cbc4;")
        bar.addWidget(self.distribution_stats)
        layout.addLayout(bar)
        if _PG_OK:
            self.distribution_plot = pg.PlotWidget()
            self.distribution_plot.setBackground("#10131a")
            self.distribution_plot.showGrid(x=True, y=True, alpha=0.25)
            self.distribution_plot.addLegend()
            self.distribution_plot.setLabel("left", "密度")
            layout.addWidget(self.distribution_plot, 1)
        else:
            self.distribution_plot = None
            layout.addWidget(QLabel("未安装 pyqtgraph，无法绘制分布直方图。"))
        return widget

    def set_result(self, result: FeatureEngineeringResult,
                   analysis: FeatureAnalysis) -> None:
        self._result = result
        self._analysis = analysis
        self.export.setEnabled(True)
        cfg = result.config
        duration = cfg.window_size / cfg.sample_rate_hz
        self.status.setText(
            f"输入 {result.input_rows} 点，有效 {result.valid_rows} 点，"
            f"删除 {result.dropped_rows} 点，裁剪 {result.clipped_values} 个值；"
            f"窗口 {cfg.window_size} 点/{duration:.3f}s，步长 {cfg.hop_size} 点；"
            f"训练窗 {result.train_windows}，验证窗 {result.validation_windows}，"
            f"生成 {len(result.feature_names)} 个特征；Nyquist "
            f"{cfg.sample_rate_hz / 2.0:.3f} Hz。")
        self._fill_summary(result, analysis)
        self._fill_importance(result, analysis)
        self._fill_correlation(result, analysis)
        self.distribution_feature.blockSignals(True)
        self.distribution_feature.clear()
        for index in analysis.top_indices:
            self.distribution_feature.addItem(
                result.feature_names[int(index)], int(index))
        self.distribution_feature.blockSignals(False)
        self._refresh_distribution()

    def _fill_summary(self, result: FeatureEngineeringResult,
                      analysis: FeatureAnalysis) -> None:
        ordered = sorted(
            analysis.summary, key=lambda row: row["importance"], reverse=True)
        self.summary_table.setRowCount(len(ordered))
        keys = ("name", "importance", "target_correlation", "mean", "std", "min", "max")
        for row_index, row in enumerate(ordered):
            for column, key in enumerate(keys):
                value = row[key]
                text = str(value) if key == "name" else f"{float(value):.6g}"
                item = QTableWidgetItem(text)
                if column > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.summary_table.setItem(row_index, column, item)

    def _fill_importance(self, result: FeatureEngineeringResult,
                         analysis: FeatureAnalysis) -> None:
        self.importance_note.setText(
            f"方法：{analysis.importance_method}。只用训练段拟合，验证段不参与排名。")
        if self.importance_plot is None:
            return
        self.importance_plot.clear()
        indices = analysis.top_indices[::-1]
        values = analysis.importance[indices]
        positions = np.arange(len(indices), dtype=float)
        bars = pg.BarGraphItem(
            x0=np.zeros_like(values), y=positions,
            width=values, height=0.68,
            brush=pg.mkBrush("#29b6f6"), pen=pg.mkPen("#81d4fa"))
        self.importance_plot.addItem(bars)
        labels = [(float(pos), result.feature_names[int(index)])
                  for pos, index in zip(positions, indices)]
        self.importance_plot.getAxis("left").setTicks([labels])
        self.importance_plot.setYRange(-1, len(indices))
        self.importance_plot.enableAutoRange(axis="x")

    @staticmethod
    def _corr_color(value: float) -> QColor:
        value = max(-1.0, min(1.0, float(value)))
        neutral = np.array([30, 35, 45], dtype=float)
        target = (np.array([41, 182, 246], dtype=float) if value >= 0
                  else np.array([240, 98, 146], dtype=float))
        rgb = neutral * (1.0 - abs(value)) + target * abs(value)
        return QColor(*(int(channel) for channel in rgb))

    def _fill_correlation(self, result: FeatureEngineeringResult,
                          analysis: FeatureAnalysis) -> None:
        indices = analysis.top_indices
        names = [result.feature_names[int(index)] for index in indices]
        size = len(names)
        self.correlation_table.setRowCount(size)
        self.correlation_table.setColumnCount(size)
        short = [name if len(name) <= 16 else name[:15] + "…" for name in names]
        self.correlation_table.setHorizontalHeaderLabels(short)
        self.correlation_table.setVerticalHeaderLabels(short)
        for row in range(size):
            for column in range(size):
                value = float(analysis.correlation[row, column])
                item = QTableWidgetItem(f"{value:+.2f}")
                item.setTextAlignment(Qt.AlignCenter)
                item.setBackground(self._corr_color(value))
                item.setToolTip(f"{names[row]} × {names[column]} = {value:+.4f}")
                self.correlation_table.setItem(row, column, item)

    def _refresh_distribution(self) -> None:
        if self._result is None or self.distribution_feature.count() == 0:
            return
        index = int(self.distribution_feature.currentData())
        values = np.asarray(self._result.X[:, index], dtype=float)
        labels = np.asarray(self._result.y, dtype=float)
        self.distribution_stats.setText(
            f"均值 {np.mean(values):.4g}　标准差 {np.std(values):.4g}　"
            f"范围 [{np.min(values):.4g}, {np.max(values):.4g}]")
        if self.distribution_plot is None:
            return
        self.distribution_plot.clear()
        lo, hi = float(np.min(values)), float(np.max(values))
        if hi <= lo:
            hi = lo + 1.0
        edges = np.linspace(lo, hi, 31)
        palette = ["#4fc3f7", "#ffb74d", "#f06292", "#81c784"]
        for color, label in zip(palette, np.unique(labels)):
            group = values[labels == label]
            if not group.size:
                continue
            hist, _ = np.histogram(group, bins=edges, density=True)
            self.distribution_plot.plot(
                edges, hist, stepMode="center",
                pen=pg.mkPen(color, width=2),
                fillLevel=0, brush=pg.mkBrush(color + "55"),
                name=f"标签 {label:g} (n={len(group)})")
        self.distribution_plot.setLabel(
            "bottom", self._result.feature_names[index])
