"""Read-only enlarged copies of offline plots; never re-run analysis."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTabWidget, QPushButton, QHBoxLayout
import pyqtgraph as pg


class AnalysisPlotDialog(QDialog):
    def __init__(self, plots, parent=None):
        super().__init__(parent)
        self.setWindowTitle("驭衡智控 · 放大查看")
        self.resize(1100, 720)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.views = []
        self.bars = []
        for title, source in plots:
            view = pg.PlotWidget(background="#10151c")
            self.views.append(view)
            plot = view.getPlotItem()
            plot.setTitle(title)
            plot.addLegend()
            for axis in ("left", "bottom"):
                original = source.getAxis(axis)
                plot.setLabel(axis, original.labelText, units=original.labelUnits)
            for curve in source.listDataItems():
                if not curve.isVisible():
                    continue
                x, y = curve.getData()
                if x is None:
                    continue
                plot.plot(x.copy(), y.copy(), pen=curve.opts.get("pen"),
                          name=curve.opts.get("name"), symbol=curve.opts.get("symbol"),
                          symbolSize=curve.opts.get("symbolSize", 5))
            for item in source.items:
                if isinstance(item, pg.ImageItem) and item.image is not None:
                    copy = pg.ImageItem(item.image.copy(), axisOrder=item.axisOrder)
                    copy.setLookupTable(item.lut)
                    copy.setLevels(item.getLevels())
                    copy.setTransform(item.transform())
                    plot.addItem(copy)
                    for i in range(source.layout.count()):
                        original_bar = source.layout.itemAt(i)
                        if isinstance(original_bar, pg.ColorBarItem):
                            bar = pg.ColorBarItem(values=item.getLevels(),
                                colorMap=original_bar.colorMap(), label="相对功率 dB", interactive=False)
                            bar.setImageItem(copy, insert_in=plot)
                            self.bars.append((plot, bar))
                            break
                elif isinstance(item, pg.InfiniteLine):
                    plot.addItem(pg.InfiniteLine(item.value(), angle=item.angle, pen=item.pen))
            plot.showGrid(x=True, y=True, alpha=.15)
            self.tabs.addTab(view, title)
        row = QHBoxLayout()
        reset = QPushButton("适应全部数据")
        reset.clicked.connect(lambda: self.views[self.tabs.currentIndex()].autoRange())
        row.addWidget(reset)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

    def done(self, result):
        for plot, bar in self.bars:
            plot.layout.removeItem(bar)
            bar.close()
            plot.scene().removeItem(bar)
        self.bars = []
        for view in self.views:
            view.close()
        self.views = []
        super().done(result)


def show_plot_dialog(plots, parent):
    dialog = AnalysisPlotDialog(plots, parent)
    dialog.exec()
    dialog.deleteLater()
