"""Offline analysis panel, embedded in Fourier subtabs or used in a dialog."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QThread, Signal, QRectF, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QDoubleSpinBox, QSpinBox, QToolButton,
    QFileDialog, QMessageBox)

from analysis_dynamic import (series, stft_map, cwt_map, order_map,
                              response_metrics, demo_snapshot, suggest_step)


class AnalysisWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            result = self.operation(self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def double_spin(value, low=-1e9, high=1e9, decimals=4):
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setValue(value)
    return box


class DynamicAnalysisDialog(QDialog):
    def __init__(self, label, snapshot, references=None, interval=None, parent=None,
                 *, embedded=False, fixed_mode=None, source_provider=None):
        super().__init__(parent)
        self.embedded = embedded
        self.fixed_mode = fixed_mode
        self.source_provider = source_provider
        self._using_demo = False
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.setWindowTitle("驭衡智控 · 时频与动态分析")
        self.resize(1240, 860)
        self.snapshot = snapshot
        self.references = references or {}
        self.source_label = label
        self.worker = None
        self.result = None
        self.metadata = {}
        empty = len(snapshot.get("values", ())) == 0
        if empty:
            t, y, fs = np.array([0., 1.]), np.array([0., 0.]), 2000.
        else:
            t, y, fs = self._preview(snapshot)
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.source = QLabel(label)
        top.addWidget(self.source, 1)
        if source_provider is not None:
            refresh = QPushButton("读取所选数据")
            refresh.clicked.connect(self.refresh_source)
            top.addWidget(refresh)
        demo = QPushButton("演示数据")
        demo.clicked.connect(self._demo)
        top.addWidget(demo)
        self.palette = QComboBox()
        for title, name in (("紫绿黄 · 参考图", "viridis"), ("熔金 · 原配色", "inferno"),
                            ("紫红黄", "plasma"), ("深紫暖白", "magma"),
                            ("蓝黄", "cividis"), ("灰阶", "CET-L1")):
            self.palette.addItem(title, name)
        from ui_theme import appearance_manager
        self._settings = appearance_manager().settings
        self.palette.setCurrentIndex(max(0, self.palette.findData(
            self._settings.value("analysis/colormap", "viridis"))))
        self.palette.currentIndexChanged.connect(self._change_palette)
        top.addWidget(self.palette)
        self.locate_button = QPushButton("定位阶跃")
        self.locate_button.setToolTip("优先按给定跳变定位；没有给定时尝试估计响应起点，仍可手动修改")
        self.locate_button.clicked.connect(self._locate_step)
        top.addWidget(self.locate_button)
        zoom = QToolButton()
        zoom.setText("↗")
        zoom.setToolTip("弹窗放大图像")
        zoom.clicked.connect(self._popout)
        top.addWidget(zoom)
        self.export = QPushButton("导出结果…")
        self.export.clicked.connect(self._export)
        self.export.setEnabled(False)
        top.addWidget(self.export)
        root.addLayout(top)
        grid = self.controls_grid = QGridLayout()
        self.mode = QComboBox()
        self.mode.addItems(["STFT 时频图", "小波系数能量图", "阶次图", "动态响应"])
        self.start = double_spin(interval[0] if interval else t[0])
        self.end = double_spin(interval[1] if interval else t[-1])
        self.window = QSpinBox()
        self.window.setRange(32, 8192)
        self.window.setValue(512 if empty else min(512, len(y)))
        self.fmin = double_spin(10. if empty else max(1., fs/len(y)*4), .001, fs/2)
        self.fmax = double_spin(min(500., fs/2), .01, fs/2)
        self.ref_kind = QComboBox()
        self.ref_kind.addItems(["机械阶次 · rpm", "电气阶次 · 电角度°"])
        self.reference = QComboBox()
        self.reference.addItems(list(self.references))
        self.ref_kind.currentIndexChanged.connect(self._suggest_reference)
        self._suggest_reference()
        self.max_order = double_spin(10, .1, 1000, 2)
        self.turns = double_spin(4, .25, 64, 2)
        self.event = double_spin(t[0]+.2*(t[-1]-t[0]))
        self.target = double_spin(float(np.median(y[-max(5, len(y)//10):])))
        self.band = double_spin(2, .1, 50, 1)
        self.dwell = double_spin(.05, .001, 100)
        self.smooth = double_spin(.01, .00001, 10, 5)
        self.sat_source = QComboBox()
        self.sat_source.addItem("无饱和分析")
        self.sat_source.addItem("按当前信号绝对限幅推断")
        self.sat_source.addItems(list(self.references))
        self.sat_source.setToolTip("选择实测饱和状态信号时，非零表示饱和；限幅推断不等于控制器实际标志")
        self.limit = double_spin(5, .0001, 1e9)
        fields = [("分析", self.mode), ("开始 s", self.start), ("结束 s", self.end),
                  ("窗长 / 点", self.window), ("最低 Hz", self.fmin), ("最高 Hz", self.fmax),
                  ("阶次基准", self.ref_kind), ("参考信号", self.reference),
                  ("最高阶次", self.max_order), ("每窗转数", self.turns),
                  ("阶跃时刻 s", self.event), ("目标值", self.target),
                  ("调节带 ±%", self.band), ("保持时间 s", self.dwell),
                  ("导数平滑 s", self.smooth), ("饱和依据", self.sat_source),
                  ("绝对限幅", self.limit)]
        self.field_labels = []
        for i, (title, widget) in enumerate(fields):
            row, col = divmod(i, 4)
            text = QLabel(title)
            self.field_labels.append((text, widget))
            grid.addWidget(text, row, col*2)
            grid.addWidget(widget, row, col*2+1)
        root.addLayout(grid)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        bar = QHBoxLayout()
        self.run_button = QPushButton("开始分析")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self._run)
        bar.addWidget(self.run_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        bar.addWidget(self.cancel_button)
        self.status = QLabel("选择时间范围后计算；计算在后台执行")
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        root.addLayout(bar)
        self.graph = pg.GraphicsLayoutWidget()
        self.graph.setBackground("#10151c")
        root.addWidget(self.graph, 1)
        self.wave_plot = self.graph.addPlot(row=0, col=0)
        self.wave_plot.setLabel("bottom", "时间", units="s")
        self.wave_plot.setMaximumHeight(175)
        self.map_plot = self.graph.addPlot(row=1, col=0)
        self.map_plot.setLabel("bottom", "时间", units="s")
        self.map_plot.setXLink(self.wave_plot)
        self.colorbar = None
        self._show_wave([] if empty else t, [] if empty else y)
        if empty:
            self.status.setText("当前信号暂无数据；可先点击“演示数据”查看效果")
        self._mode_changed()
        if fixed_mode is not None:
            self.mode.setCurrentIndex(fixed_mode)
        if source_provider is not None:
            self.refresh_source()

    @staticmethod
    def _preview(snapshot):
        y = np.asarray(snapshot.get("values", ()), dtype=float)
        t = np.asarray(snapshot.get("times", ()), dtype=float)
        fs = float(snapshot.get("sample_rate_hz", 0) or 2000.)
        if len(t) != len(y):
            t = np.arange(len(y))/fs
        if len(t) > 1 and t[-1] > t[0]:
            fs = (len(t)-1)/(t[-1]-t[0])
        return t, y, fs

    def refresh_source(self, _checked=False):
        if self.busy() or self.source_provider is None:
            return
        try:
            label, snapshot, references, interval = self.source_provider()
            self.snapshot, self.references = snapshot, references
            self.source_label = label
            self._using_demo = False
            self.source.setText(label)
            previous = self.reference.currentText()
            self.reference.clear()
            self.reference.addItems(list(references))
            if previous in references:
                self.reference.setCurrentText(previous)
            else:
                self._suggest_reference()
            self.sat_source.clear()
            self.sat_source.addItems(["无饱和分析", "按当前信号绝对限幅推断", *references])
            t, y, fs = self._preview(snapshot)
            self.result = None
            self.export.setEnabled(False)
            if self.colorbar is not None:
                self.map_plot.layout.removeItem(self.colorbar)
                self.colorbar.close()
                self.graph.scene().removeItem(self.colorbar)
                self.colorbar = None
            self.map_plot.clear()
            self._show_wave(t, y)
            if len(t) > 1:
                self.start.setValue(interval[0] if interval else t[0])
                self.end.setValue(interval[1] if interval else t[-1])
                self.event.setValue(t[0]+.2*(t[-1]-t[0]))
                self.target.setValue(float(np.median(y[-max(5,len(y)//10):])))
                self.fmax.setMaximum(fs/2)
                self.fmin.setMaximum(fs/2)
                self.status.setText(f"已读取 {len(y):,} 点；调整区间后开始分析")
                if self.mode.currentIndex() == 3:
                    self._locate_step()
            else:
                self.status.setText("所选信号暂无数据，可先查看演示")
        except Exception as exc:
            self.status.setText(str(exc))

    def _change_palette(self):
        name = self.palette.currentData()
        self._settings.setValue("analysis/colormap", name)
        if self.colorbar is not None:
            self.colorbar.setColorMap(pg.colormap.get(name))
        if self.metadata:
            self.metadata["colormap"] = name

    def _popout(self):
        from widgets.analysis_plot_dialog import show_plot_dialog
        show_plot_dialog([("原始波形", self.wave_plot),
                          (self.mode.currentText(), self.map_plot)], self)

    def _suggest_reference(self):
        for i in range(self.reference.count()):
            name = self.reference.itemText(i)
            match = (("转速" in name or "rpm" in name.lower() or name.lower().startswith("speed /")) and "给定" not in name
                     if self.ref_kind.currentIndex() == 0 else "电角度" in name)
            if match:
                self.reference.setCurrentIndex(i)
                break

    def _locate_step(self, _checked=False):
        if self.busy():
            return
        try:
            t, y, _ = series(self.snapshot, (self.start.value(), self.end.value()), timing="raw")
            reference = None
            source = self.source_label.lower()
            is_speed = self.snapshot.get("unit") == "rpm" or "speed" in source or "转速" in source
            for name, provider in self.references.items():
                lower = name.lower()
                given = any(token in lower for token in ("给定", "ref", "target", "目标"))
                same = (any(token in lower for token in ("转速", "speed", "rpm")) if is_speed
                        else "iq" in source and "iq" in lower)
                if given and same:
                    reference = provider()
                    break
            found = suggest_step(t, y, reference)
            if found:
                self.event.setValue(found["event"])
                self.target.setValue(found["target"])
                self.status.setText(f"{found['basis']}：{found['event']:.6g} s，目标 {found['target']:.6g}")
            else:
                self.status.setText("未找到清晰阶跃；可直接分析波形和变化率，或手动设置阶跃时刻与目标")
        except Exception as exc:
            self.status.setText(str(exc))

    def _mode_changed(self):
        mode = self.mode.currentIndex()
        self.palette.setVisible(mode != 3)
        self.locate_button.setVisible(mode == 3)
        enabled = set(range(3))
        if self.fixed_mode is not None:
            enabled.discard(0)
        enabled |= ({3, 5} if mode == 0 else {4, 5} if mode == 1 else
                    {6, 7, 8, 9} if mode == 2 else set(range(10, 17)))
        position = 0
        for i, (label, widget) in enumerate(self.field_labels):
            self.controls_grid.removeWidget(label)
            self.controls_grid.removeWidget(widget)
            label.setVisible(i in enabled)
            widget.setVisible(i in enabled)
            if i in enabled:
                row, col = divmod(position, 4)
                self.controls_grid.addWidget(label, row, col*2)
                self.controls_grid.addWidget(widget, row, col*2+1)
                position += 1

    def _show_wave(self, t, y):
        self.wave_plot.clear()
        curve = self.wave_plot.plot(t, y, pen=pg.mkPen("#6bcbbd", width=1))
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        self.wave_plot.setTitle(self.source_label)
        self.wave_plot.setLabel("left", "输入", units=self.snapshot.get("unit", ""))

    def _demo(self):
        if self.busy():
            return
        self.snapshot, speed = demo_snapshot()
        self._using_demo = True
        self.source_label = "演示信号 · 扫频＋250 Hz 衰减振铃（非实机）"
        self.source.setText(self.source_label)
        self.references = {"演示机械转速": lambda: speed}
        self.reference.clear()
        self.reference.addItems(list(self.references))
        self.sat_source.clear()
        self.sat_source.addItems(["无饱和分析", "按当前信号绝对限幅推断"])
        self.start.setValue(0)
        self.end.setValue(1.99975)
        self.fmax.setMaximum(2000)
        self.fmax.setValue(500)
        self.fmin.setValue(10)
        self.ref_kind.setCurrentIndex(0)
        self.mode.setCurrentIndex(self.fixed_mode if self.fixed_mode is not None else 0)
        if self.mode.currentIndex() == 3:
            t = self.snapshot["times"]
            elapsed = np.maximum(t-.3, 0)
            y = np.where(t < .3, 0, 1000*(1-np.exp(-elapsed*12)*np.cos(24*elapsed)))
            self.snapshot = dict(self.snapshot, values=y, unit="rpm")
            self.source_label = "演示响应 · 转速阶跃（非实机）"
            self.source.setText(self.source_label)
            self.event.setValue(.3)
            self.target.setValue(1000)
        self._run()

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def _cancel(self):
        if self.worker:
            self.worker.requestInterruption()
            self.status.setText("正在取消；等待当前计算结束")

    def _run(self):
        if self.busy():
            return
        try:
            interval = (self.start.value(), self.end.value())
            mode = self.mode.currentIndex()
            t, y, fs = series(self.snapshot, interval, timing="raw" if mode == 3 else "resample")
            ref = self.references[self.reference.currentText()]() if mode == 2 else None
            if ref is not None:
                unit = str(ref.get("unit", "")).strip()
                if unit and ((self.ref_kind.currentIndex() == 0 and unit != "rpm") or
                             (self.ref_kind.currentIndex() == 1 and unit not in ("°", "deg", "度"))):
                    raise ValueError("阶次参考应选择实际转速(rpm)或电角度(°)，当前参考单位为 "+unit)
                rt = np.asarray(ref.get("times", ()), dtype=float)
                if len(rt):
                    common = (t >= rt[0]) & (t <= rt[-1])
                    t, y = t[common], y[common]
                    if len(t) < 32:
                        raise ValueError("信号和参考没有足够的共同时间范围，请重新读取同一记录")
            sat = None
            if mode == 3 and self.sat_source.currentIndex() > 1:
                st, sy, _ = series(self.references[self.sat_source.currentText()](), timing="raw", min_samples=2)
                if st[0] > t[0] or st[-1] < t[-1]:
                    raise ValueError("饱和标志未覆盖所选区间")
                sat = sy[np.clip(np.searchsorted(st, t, side="right")-1, 0, len(st)-1)] != 0
            params = dict(window=self.window.value(), fmin=self.fmin.value(),
                          fmax=self.fmax.value(), max_order=self.max_order.value(),
                          turns=self.turns.value(), event=self.event.value(),
                          target=self.target.value(), band=self.band.value()/100,
                          dwell=self.dwell.value(), smooth=self.smooth.value(),
                          ref_kind="speed" if self.ref_kind.currentIndex() == 0 else "angle",
                          limit=self.limit.value() if self.sat_source.currentIndex() == 1 else None)
            self.metadata = dict(source=self.source_label, interval=interval, fs=fs,
                                 unit=self.snapshot.get("unit", ""),
                                 parameters=params, mode=self.mode.currentText(),
                                 reference=self.reference.currentText(),
                                 saturation_basis=self.sat_source.currentText(),
                                 source_processing=self.snapshot.get("source_processing", ""))
            self.metadata.update(colormap=self.palette.currentData(),
                                 effective_interval=[float(t[0]), float(t[-1])],
                                 time_processing="动态指标使用原始时间；导数重采样" if mode == 3 else "按实际时间线性重采样；参考按实际时间插值")
        except Exception as exc:
            self.status.setText(str(exc))
            return
        def operation(cancel):
            if mode == 0:
                return stft_map(t, y, fs, params["window"], params["fmax"])
            if mode == 1:
                return cwt_map(t, y, fs, params["fmin"], params["fmax"], cancel)
            if mode == 2:
                return order_map(t, y, fs, ref, params["ref_kind"],
                                 params["max_order"], params["turns"])
            return response_metrics(t, y, params["event"], params["target"],
                                    params["band"], params["dwell"], params["smooth"],
                                    sat, params["limit"])
        self._show_wave(t, y)
        self.result = None
        self.export.setEnabled(False)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText(f"后台分析 {len(y):,} 点，采样率 {fs:g} Hz…")
        self.worker = AnalysisWorker(operation, self)
        self.worker.succeeded.connect(self._show_result)
        self.worker.failed.connect(self.status.setText)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.worker and self.worker.isInterruptionRequested():
            self.status.setText("已取消")
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()

    def _show_result(self, result):
        self.result = result
        self.map_plot.clear()
        if self.colorbar is not None:
            self.map_plot.layout.removeItem(self.colorbar)
            self.colorbar.close()
            self.graph.scene().removeItem(self.colorbar)
            self.colorbar = None
        if "metrics" in result:
            m = result["metrics"]
            self.map_plot.plot(result["time"], result["derivative"], pen="#ffc477")
            unit = self.snapshot.get("unit", "")
            self.map_plot.setLabel("left", "加速度" if unit == "rpm" else "变化率", units=unit+"/s")
            self.map_plot.setTitle(f"Savitzky–Golay 导数 · 平滑窗 {result['smooth_s']:.4g} s")
            for value in ((m["target"], m["target"]+abs(m["target"]-m["baseline"])*result["band"],
                          m["target"]-abs(m["target"]-m["baseline"])*result["band"])
                          if result.get("step_valid", True) else ()):
                self.wave_plot.addItem(pg.InfiniteLine(value, angle=0, pen="#b6a277"))
            if result.get("step_valid", True):
                self.wave_plot.addItem(pg.InfiniteLine(result["event"], pen="#ffb65e"))
            for key in ("t10", "t90"):
                if result[key] is not None:
                    self.wave_plot.addItem(pg.InfiniteLine(result[key], pen="#70959e"))
            def fmt(key, suffix):
                return "未达到/无法判定" if m[key] is None else f"{m[key]:.4g}{suffix}"
            self.status.setText("  ｜  ".join([
                "上升(10–90%) "+fmt("rise_s", " s"), "超调 "+fmt("overshoot_pct", "%"),
                "调节 "+fmt("settling_s", " s"), "峰值变化率 "+fmt("peak_slope", " "+unit+"/s"),
                "振铃 "+fmt("ringing_hz", " Hz"), "饱和占比 "+fmt("saturation_fraction", " (0–1)"),
                "稳态误差 "+fmt("steady_error", " "+unit)]))
            if not result.get("step_valid", True):
                self.status.setText(result["message"]+" ｜ 峰值变化率 "+fmt("peak_slope", " "+unit+"/s")+
                                    " ｜ 峰峰值 "+fmt("peak_to_peak", " "+unit)+
                                    " ｜ 波动 RMS "+fmt("ac_rms", " "+unit)+
                                    " ｜ 饱和占比 "+fmt("saturation_fraction", ""))
            sat = result["saturation"]
            if sat is not None:
                indices = np.flatnonzero(sat)[::max(1, np.count_nonzero(sat)//2000)]
                self.wave_plot.plot(result["time"][indices], result["values"][indices],
                                    pen=None, symbol="o", symbolSize=3, symbolBrush="#ff7979")
        else:
            p = result["power"]
            finite = p[np.isfinite(p)]
            peak = float(np.max(finite)) if finite.size else 0
            if peak <= 1e-30:
                db = np.where(np.isfinite(p), -70., np.nan)
            else:
                db = 10*np.log10(np.maximum(p/peak, 1e-7))
            x, axis = result["time"], result["axis"]
            # Order windows are uniform in angle; map each row to uniform time
            # before placing a rectangular image, keeping original arrays on export.
            uniform = np.linspace(x[0], x[-1], max(2, len(x)))
            db = np.asarray([np.interp(uniform, x, row) for row in db])
            if "valid_time_ranges" in result:
                valid = np.zeros(len(uniform), dtype=bool)
                for start, end in result["valid_time_ranges"]:
                    valid |= (uniform >= start) & (uniform <= end)
                    if start == end:
                        valid[np.argmin(abs(uniform-start))] = True
                db[:, ~valid] = np.nan
            img = pg.ImageItem(axisOrder="row-major")
            img.setImage(db, autoLevels=False, levels=(-70, 0))
            self.map_plot.addItem(img)
            dx = (uniform[-1]-uniform[0])/max(1, len(uniform)-1)
            if dx <= 0:
                dx = self.metadata["parameters"]["window"]/self.metadata["fs"]
            dy = (axis[-1]-axis[0])/max(1, len(axis)-1) if len(axis)>1 else 1
            img.setRect(QRectF(uniform[0]-dx/2, axis[0]-dy/2,
                               uniform[-1]-uniform[0]+dx, axis[-1]-axis[0]+dy))
            self.colorbar = pg.ColorBarItem(values=(-70, 0), colorMap=pg.colormap.get(self.palette.currentData()),
                                             label="相对功率 dB", interactive=False)
            self.colorbar.setImageItem(img, insert_in=self.map_plot)
            is_order = result["kind"] == "阶次"
            self.map_plot.setLabel("left", "阶次" if is_order else "频率", units="" if is_order else "Hz")
            self.map_plot.setTitle(result["kind"]+" · "+result["unit"])
            self.status.setText((f"Δ = {result['resolution']:.4g} " + ("阶" if is_order else "Hz")
                                 if "resolution" in result else
                                 "Morlet ω₀=6 · L2 归一化 · 边缘影响区已留空；|W|²不等同于物理能量 J")
                                + " ｜ 色标以本图峰值为 0 dB；跨实验定量比较请使用导出的原始功率")
            if result.get("message"):
                self.status.setText(result["message"]+f" ｜ Δ阶次 = {result['resolution']:.4g}")
        self.map_plot.enableAutoRange()
        self.export.setEnabled(True)

    def _export(self):
        if self.result is None:
            return
        path, selected = QFileDialog.getSaveFileName(self, "保存分析数组或图像", "动态分析.npz",
                                                    "NumPy 压缩数据 (*.npz);;图像 (*.png)")
        if not path:
            return
        try:
            if "*.png" in selected or Path(path).suffix.lower() == ".png":
                if not self.graph.grab().save(path, "PNG"):
                    raise OSError("图像写入失败")
                self.status.setText("已保存图像："+path)
                return
            arrays = {k: v for k, v in self.result.items() if isinstance(v, np.ndarray)}
            info = dict(self.metadata, result={k: v for k, v in self.result.items()
                                              if not isinstance(v, np.ndarray)})
            with open(path, "wb") as stream:
                np.savez_compressed(stream, **arrays, metadata=json.dumps(info, ensure_ascii=False))
            self.status.setText("已保存："+path)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def reject(self):
        if self.embedded:
            return
        if self.busy():
            self._cancel()
            return
        self._dispose_graph()
        super().reject()

    def _dispose_graph(self):
        if getattr(self, "_graph_closed", False):
            return
        self._graph_closed = True
        # PlotItem.close detaches QGraphics proxy widgets before the owning
        # dialog's deferred deletion (required by PySide on Windows).
        if self.colorbar is not None:
            self.map_plot.layout.removeItem(self.colorbar)
            self.colorbar.close()
            self.graph.scene().removeItem(self.colorbar)
            self.colorbar = None
        self.wave_plot.close()
        self.map_plot.close()
        self.graph.close()

    def closeEvent(self, event):
        if self.busy():
            self._cancel()
            event.ignore()
        else:
            self._dispose_graph()
            super().closeEvent(event)
