"""实验日志页：选实验 → 生成固定模板日志（HTML + PDF）→ 预览；可让 AI 起草结论或自己写。

两种模板：单次实验、对比实验（一个基准 + 一个或多个对照，看改参数后结果有什么不同）。
数据部分全部由 experiments/experiment_log 经 harness 只读工具计算；结论分“操作者 / AI”两块。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from ai.experiment_tools import waveform_batches
from core.dyno_load import LOAD_MODES
from experiments.experiment_log import (
    AI_FILE, LOG_HTML, LOG_PDF, USER_FILE, ExperimentLogGenerator, _print_pdf_textdocument,
    _write_json, draft_ai_conclusion, load_json, pdf_page_layout,
)
from widgets.experiment_conclusion_dialog import ExperimentConclusionDialog

_SINGLE, _COMPARE = "single", "comparison"


class _AiSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


class _GenerateSignals(QObject):
    progress = Signal(int, str)
    done = Signal(object)
    failed = Signal(str)


class PdfPrinter(QObject):
    """异步把 HTML 打印成 PDF：QtWebEngine 只能在 GUI 线程用，这里靠信号回调，不开局部事件循环。"""

    finished = Signal(object)   # Path | None

    def __init__(self, parent=None, timeout_ms: int = 60000) -> None:
        super().__init__(parent)
        self._timeout_ms = timeout_ms
        self._page = None
        self._pdf_path: Path | None = None
        self._done = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._finish(False))

    def start(self, html_path: Path, pdf_path: Path) -> None:
        self._pdf_path = Path(pdf_path)
        try:
            if os.environ.get("HMI_NO_WEBENGINE"):
                raise ImportError
            from PySide6.QtWebEngineCore import QWebEnginePage
        except ImportError:
            # 无 WebEngine：QTextDocument 打印很快，放到下一轮事件里做，保持接口一致
            QTimer.singleShot(0, lambda: self._finish(
                _print_pdf_textdocument(html_path, pdf_path) is not None))
            return
        self._page = QWebEnginePage(self)
        self._page.loadFinished.connect(self._on_loaded)
        self._page.pdfPrintingFinished.connect(lambda _path, ok: self._finish(ok))
        self._timer.start(self._timeout_ms)
        self._page.load(QUrl.fromLocalFile(str(Path(html_path).resolve())))

    def _on_loaded(self, ok: bool) -> None:
        if not ok:
            self._finish(False)
        elif not self._done:
            self._page.printToPdf(str(self._pdf_path), pdf_page_layout())

    def _finish(self, ok: bool) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        path = self._pdf_path if ok and self._pdf_path and self._pdf_path.exists() else None
        self.finished.emit(path)


class ExperimentLogPage(QWidget):
    def __init__(self, repository, *, conclusion_updater: Callable | None = None,
                 ai_client_provider: Callable | None = None, params_provider=None,
                 embedded: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.generator = ExperimentLogGenerator(repository, params_provider)
        self._update_conclusion = conclusion_updater
        self._ai_client_provider = ai_client_provider
        self._folder: Path | None = None            # 当前日志所在目录
        self._comparison_ids: list[str] = []
        self._preview = None
        self._ai_signals = _AiSignals()
        self._ai_signals.done.connect(self._on_ai_done)
        self._ai_signals.failed.connect(self._on_ai_failed)
        self._ai_signals.progress.connect(lambda text: self._status.setText(f"AI：{text}"))
        # 生成在后台线程，PDF 在 GUI 线程异步打印：生成期间界面照常可用
        self._gen_signals = _GenerateSignals()
        self._gen_signals.progress.connect(self._on_generate_progress)
        self._gen_signals.done.connect(self._on_generate_done)
        self._gen_signals.failed.connect(self._on_generate_failed)
        self._busy = False
        self._printer: PdfPrinter | None = None
        self._pending_comparison: list[str] | None = None

        root = QVBoxLayout(self)
        if embedded:
            # 作为“实验管理”里的一个标签页：页标题已由外层给出
            root.setContentsMargins(0, 4, 0, 0)
        else:
            title = QLabel("实验日志")
            title.setObjectName("TitleLabel")
            root.addWidget(title)
        intro = QLabel("固定模板：数据部分全部由驭衡记录的文件经只读工具计算得到（附来源审计），"
                       "结论分“操作者”与“AI 分析（需人工确认）”两块。对比实验看改参数前后结果的差别。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8fa3b8;")
        if embedded:
            intro.hide()                       # 标签页里空间紧，说明放到悬停提示
            self.setToolTip(intro.text())
        root.addWidget(intro)

        # 先建预览区：左侧列表建好时会自动选中一次实验并预览它已有的日志
        self._preview_host = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_host)
        self._preview_layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("选择实验后点“生成实验日志”，日志会显示在这里。")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color:#8fa3b8;")
        self._preview_layout.addWidget(self._placeholder)
        splitter = QSplitter(Qt.Horizontal)
        left = QScrollArea()                   # 窗口矮时左侧按钮不被裁掉
        left.setWidgetResizable(True)
        left.setFrameShape(QScrollArea.NoFrame)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.setWidget(self._build_left())
        left.setMinimumWidth(400)
        splitter.addWidget(left)
        splitter.addWidget(self._preview_host)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 900])
        root.addWidget(splitter, 1)
        self.refresh()

    def set_providers(self, *, ai_client_provider: Callable | None = None,
                      params_provider=None) -> None:
        """主窗口建好诊断助手与通信后再注入（本页先于它们构造）。"""
        if ai_client_provider is not None:
            self._ai_client_provider = ai_client_provider
        if params_provider is not None:
            self.generator.data.params_provider = params_provider

    # ------------------------------------------------------------ 布局
    def _build_left(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(380)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("日志模板与实验")
        form = QFormLayout(box)
        self._form = form
        self._template = QComboBox()
        self._template.addItem("单次实验日志", _SINGLE)
        self._template.addItem("对比实验日志（改参数前后）", _COMPARE)
        self._template.currentIndexChanged.connect(self._on_template_changed)
        form.addRow("模板", self._template)
        self._baseline = QComboBox()
        self._baseline.setToolTip("对比的基准实验（通常是改参数之前那次）")
        form.addRow("基准实验", self._baseline)
        self._experiments = QListWidget()
        self._experiments.setMinimumHeight(110)
        self._experiments.setSelectionMode(QAbstractItemView.SingleSelection)
        self._experiments.currentItemChanged.connect(self._on_experiment_selected)
        form.addRow(self._experiments)
        self._batch = QComboBox()
        self._batch.setToolTip("高速数据的保存批次（同一实验可能保存过多次波形）")
        form.addRow("高速数据", self._batch)
        self._whole = QCheckBox("整段")
        self._whole.setChecked(True)
        self._t0, self._t1 = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self._t0, self._t1):
            spin.setRange(0.0, 3600.0)
            spin.setDecimals(2)
            spin.setSuffix(" s")
            spin.setEnabled(False)
        self._t1.setValue(20.0)
        self._whole.toggled.connect(lambda on: [s.setEnabled(not on) for s in (self._t0, self._t1)])
        window_row = QHBoxLayout()
        window_row.addWidget(self._whole)
        window_row.addWidget(self._t0)
        window_row.addWidget(QLabel("至"))
        window_row.addWidget(self._t1)
        form.addRow("分析时间窗", window_row)
        self._load_mode = QComboBox()
        self._load_mode.addItem("按实验记录", None)
        for key, text in LOAD_MODES.items():
            if key != "unknown":
                self._load_mode.addItem(text, key)
        self._load_mode.setToolTip("对拖负载工况；旧实验没有记录时可在这里指定，"
                                   "选“上电未启动”会加入短路制动模型与实测对比")
        form.addRow("负载工况", self._load_mode)
        layout.addWidget(box, 1)

        self._btn_generate = QPushButton("生成实验日志")
        self._btn_generate.setObjectName("PrimaryButton")
        self._btn_generate.clicked.connect(self.generate)
        self._btn_ai = QPushButton("AI 写结论")
        self._btn_ai.setToolTip("由诊断助手当前的模型依据日志数据起草结论（需人工确认）")
        self._btn_ai.clicked.connect(self.request_ai_conclusion)
        self._btn_conclusion = QPushButton("编辑我的结论")
        self._btn_conclusion.clicked.connect(self.edit_conclusion)
        self._btn_browser = QPushButton("浏览器打开")
        self._btn_browser.clicked.connect(lambda: self._open(LOG_HTML))
        self._btn_pdf = QPushButton("打开 PDF")
        self._btn_pdf.clicked.connect(lambda: self._open("实验日志.pdf"))
        self._btn_folder = QPushButton("打开文件夹")
        self._btn_folder.clicked.connect(lambda: self._open(""))
        self._btn_refresh = QPushButton("刷新列表")
        self._btn_refresh.clicked.connect(self.refresh)
        layout.addWidget(self._btn_generate)
        for pair in ((self._btn_ai, self._btn_conclusion), (self._btn_browser, self._btn_pdf),
                     (self._btn_folder, self._btn_refresh)):
            row = QHBoxLayout()
            for button in pair:
                row.addWidget(button)
            layout.addLayout(row)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#8fa3b8;")
        layout.addWidget(self._status)
        self._on_template_changed()
        return panel

    # ------------------------------------------------------------ 列表
    @property
    def comparison_mode(self) -> bool:
        return self._template.currentData() == _COMPARE

    def refresh(self) -> None:
        sessions = self.repository.list_sessions()
        current = self.selected_ids()
        baseline = self._baseline.currentData()
        self._experiments.blockSignals(True)
        self._experiments.clear()
        self._baseline.clear()
        for session in sessions:
            folder = self.repository.session_dir(session.experiment_id)
            has_high = any(b["high_rate_file"] for b in waveform_batches(folder))
            text = f"{session.experiment_id}  {session.name}" + ("" if has_high else "  （无高速数据）")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, session.experiment_id)
            if self.comparison_mode:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if session.experiment_id in current else Qt.Unchecked)
            self._experiments.addItem(item)
            self._baseline.addItem(f"{session.experiment_id}  {session.name}", session.experiment_id)
        self._experiments.blockSignals(False)
        if baseline:
            index = self._baseline.findData(baseline)
            if index >= 0:
                self._baseline.setCurrentIndex(index)
        if not self.comparison_mode and self._experiments.count():
            ids = [self._experiments.item(i).data(Qt.UserRole) for i in range(self._experiments.count())]
            row = ids.index(current[0]) if current and current[0] in ids else 0
            self._experiments.setCurrentRow(row)
        self._status.setText(f"共 {len(sessions)} 次实验。" if sessions else
                             "还没有实验记录：在“实验管理”开始一次实验，并在监控页保存波形。")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        # 每次切到本页都重新读实验列表：实验管理页刚结束的实验要立刻能选到
        super().showEvent(event)
        self.refresh()

    def selected_ids(self) -> list[str]:
        if self.comparison_mode:
            return [self._experiments.item(i).data(Qt.UserRole)
                    for i in range(self._experiments.count())
                    if self._experiments.item(i).checkState() == Qt.Checked]
        item = self._experiments.currentItem()
        return [item.data(Qt.UserRole)] if item else []

    def set_selection(self, ids: list[str], baseline: str | None = None) -> None:
        if baseline:
            self._baseline.setCurrentIndex(max(0, self._baseline.findData(baseline)))
        for i in range(self._experiments.count()):
            item = self._experiments.item(i)
            if self.comparison_mode:
                item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in ids else Qt.Unchecked)
            elif item.data(Qt.UserRole) in ids:
                self._experiments.setCurrentRow(i)

    def _on_template_changed(self, *_args) -> None:
        compare = self.comparison_mode
        self._baseline.setEnabled(compare)
        self._form.setRowVisible(self._baseline, compare)      # 单次日志不需要基准行
        self._form.setRowVisible(self._batch, not compare)
        if hasattr(self, "_load_mode"):
            self._form.setRowVisible(self._load_mode, not compare)
        self._batch.setEnabled(not compare)
        self._experiments.setToolTip("勾选对照实验（可多选）" if compare else "选择一次实验")
        if hasattr(self, "_experiments"):
            self.refresh()

    def _on_experiment_selected(self, current, _previous) -> None:
        self._batch.clear()
        if current is None or self.comparison_mode:
            return
        experiment_id = current.data(Qt.UserRole)
        folder = self.repository.session_dir(experiment_id)
        for batch in waveform_batches(folder):
            if batch["high_rate_file"]:
                self._batch.addItem(f"{batch['batch']}（{batch['high_rate_file']}）", batch["batch"])
        if self._batch.count():
            self._batch.setCurrentIndex(self._batch.count() - 1)
        existing = folder / "report" / LOG_HTML
        self._folder = existing.parent
        if existing.exists():
            self._show(existing)

    # ------------------------------------------------------------ 生成
    def _window_args(self) -> tuple[float | None, float | None]:
        if self._whole.isChecked():
            return None, None
        return self._t0.value(), self._t1.value()

    @property
    def busy(self) -> bool:
        return self._busy

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in (self._btn_generate, self._btn_ai, self._btn_conclusion):
            button.setEnabled(not busy)
        self._btn_generate.setText("正在生成…" if busy else "生成实验日志")

    def generate(self, *, reuse_assets: bool = False) -> bool:
        """在后台线程生成；返回 True 表示已开始（完成后自动预览、再异步打印 PDF）。"""
        if self._busy:
            self._status.setText("上一份日志还在生成，请稍候。")
            return False
        t0, t1 = self._window_args()
        try:
            if self.comparison_mode:
                baseline = self._baseline.currentData()
                others = [i for i in self.selected_ids() if i != baseline]
                if not baseline or not others:
                    raise ValueError("对比实验：请选择基准实验，并在列表里勾选至少一个对照实验")
                ids = [baseline] + others
                folder = self._folder if reuse_assets and self._comparison_ids == ids else None

                def job(progress):
                    return self.generator.generate_comparison(
                        baseline, others, t0, t1, progress, folder=folder, make_pdf=False,
                        reuse_assets=reuse_assets)
                self._pending_comparison = ids
            else:
                ids = self.selected_ids()
                if not ids:
                    raise ValueError("请先在列表里选择一次实验")
                experiment_id, batch = ids[0], self._batch.currentData()
                load_mode = self._load_mode.currentData()

                def job(progress):
                    return self.generator.generate(
                        experiment_id, batch, t0, t1, progress, make_pdf=False,
                        reuse_assets=reuse_assets, load_mode=load_mode)
                self._pending_comparison = None
        except ValueError as exc:
            self._status.setText(f"生成失败：{exc}")
            QMessageBox.warning(self, "实验日志生成失败", str(exc))
            return False
        self._set_busy(True)
        self._progress.setValue(0)
        self._status.setText("正在后台生成实验日志，可以继续使用其他页面…")
        signals = self._gen_signals

        def work() -> None:
            try:
                result = job(signals.progress.emit)
            except Exception as exc:  # noqa: BLE001 - 把原因告诉用户
                signals.failed.emit(str(exc))
            else:
                signals.done.emit(result)

        threading.Thread(target=work, name="experiment-log-generate", daemon=True).start()
        return True

    def wait_idle(self, timeout_s: float = 180.0) -> bool:
        """处理事件直到生成与 PDF 打印都结束（测试与脚本用；界面里不需要调用）。"""
        deadline = time.monotonic() + timeout_s
        while self._busy and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        return not self._busy

    def _on_generate_progress(self, value: int, text: str) -> None:
        self._progress.setValue(value)
        self._status.setText(text)

    def _on_generate_failed(self, message: str) -> None:
        self._set_busy(False)
        self._status.setText(f"生成失败：{message}")
        QMessageBox.warning(self, "实验日志生成失败", message)

    def _on_generate_done(self, result) -> None:
        self._folder = result.folder
        if self._pending_comparison is not None:
            self._comparison_ids = self._pending_comparison
        self._show(result.html)
        self._progress.setValue(95)
        note = "；".join(result.warnings)
        self._result_text = f"已生成：{result.html}" + (f"\n注意：{note}" if note else "")
        self._status.setText(self._result_text + "\n正在后台打印 PDF…")
        self._printer = PdfPrinter(self)
        self._printer.finished.connect(self._on_pdf_done)
        self._printer.start(result.html, result.folder / LOG_PDF)

    def _on_pdf_done(self, pdf) -> None:
        if self._printer is not None:
            self._printer.deleteLater()
            self._printer = None
        self._progress.setValue(100)
        self._status.setText(self._result_text + (
            f"\nPDF：{pdf}" if pdf else "\nPDF 未生成（缺少 QtWebEngine 或打印失败）"))
        self._set_busy(False)

    # ------------------------------------------------------------ 预览
    def _ensure_preview(self):
        if self._preview is not None:
            return self._preview
        view = None
        if not os.environ.get("HMI_NO_WEBENGINE"):
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                view = QWebEngineView()
            except ImportError:
                view = None
        if view is None:
            # 无 WebEngine 时的简易预览：日志是浅色页面，不跟随深色主题
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setStyleSheet("QTextBrowser { background:#ffffff; color:#243447; }")
        self._placeholder.hide()
        self._preview_layout.addWidget(view)
        self._preview = view
        return view

    def _show(self, html_path: Path) -> None:
        view = self._ensure_preview()
        url = QUrl.fromLocalFile(str(Path(html_path).resolve()))
        if isinstance(view, QTextBrowser):
            view.setSource(url)
        else:
            view.load(url)

    def _open(self, name: str) -> None:
        if self._folder is None:
            self._status.setText("还没有生成日志。")
            return
        target = self._folder / name if name else self._folder
        if name and not target.exists():
            self._status.setText(f"文件不存在：{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ------------------------------------------------------------ 结论
    def edit_conclusion(self) -> None:
        if self._folder is None or not (self._folder / LOG_HTML).exists():
            QMessageBox.information(self, "编辑结论", "请先生成实验日志。")
            return
        ai_text = load_json(self._folder / AI_FILE).get("text", "")
        if self.comparison_mode:
            current = load_json(self._folder / USER_FILE)
            dialog = ExperimentConclusionDialog("对比实验", current, self)
        else:
            experiment_id = self.selected_ids()[0]
            session = self.repository.load(experiment_id)
            dialog = ExperimentConclusionDialog(experiment_id, session.conclusion, self)
        if ai_text:
            adopt = QPushButton("用 AI 草稿填入“主要观察”（可再修改）")
            adopt.clicked.connect(lambda: dialog.observations.setPlainText(ai_text))
            dialog.layout().insertWidget(dialog.layout().count() - 1, adopt)
        if dialog.exec() != ExperimentConclusionDialog.Accepted:
            return
        values = dialog.conclusion()
        if self.comparison_mode:
            _write_json(self._folder / USER_FILE, values)
        elif self._update_conclusion is not None:
            self._update_conclusion(self.selected_ids()[0], values)
        self.generate(reuse_assets=True)

    def request_ai_conclusion(self) -> None:
        if self._folder is None or not (self._folder / "log_data.json").exists():
            QMessageBox.information(self, "AI 写结论", "请先生成实验日志，AI 依据日志里的数据起草结论。")
            return
        provided = self._ai_client_provider() if callable(self._ai_client_provider) else None
        if not provided or provided[0] is None:
            QMessageBox.information(
                self, "AI 写结论",
                "诊断助手当前没有可用的对话模型（JEV / 本地 Laya 不支持工具调用）。"
                "请在“诊断助手”页选择并配置 OpenAI 兼容模型后再试。")
            return
        client, model = provided
        folder = self._folder
        self._btn_ai.setEnabled(False)
        self._status.setText("AI 正在依据日志数据起草结论…")

        def work() -> None:
            try:
                result = draft_ai_conclusion(client, self.generator, folder, model,
                                             on_event=self._ai_signals.progress.emit)
            except Exception as exc:  # noqa: BLE001
                self._ai_signals.failed.emit(str(exc))
            else:
                self._ai_signals.done.emit(result)

        threading.Thread(target=work, name="experiment-log-ai", daemon=True).start()

    def _on_ai_done(self, _result) -> None:
        self._btn_ai.setEnabled(True)
        self._status.setText("AI 结论已写入（标注为需人工确认），正在更新日志…")
        self.generate(reuse_assets=True)

    def _on_ai_failed(self, message: str) -> None:
        self._btn_ai.setEnabled(True)
        self._status.setText(f"AI 起草失败：{message}")
