from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from pages.ai_page import AIPage
from widgets.diagnostic_sidebar import DiagnosticSidebar


def test_sidebar_shares_config_stream_and_rejects_duplicate_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(AIPage, "_ensure_rag_index", lambda self: None)
    monkeypatch.setattr("pages.ai_page._CONFIG_FILE", tmp_path / "missing.json")
    class HeldThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr("pages.ai_page.threading.Thread", HeldThread)
    page = AIPage(CommManager())
    sidebar = DiagnosticSidebar(page)
    try:
        sidebar.profile.setCurrentText("Kimi")
        assert page._profile.currentText() == "Kimi"
        page._profile.setCurrentText("Qwen")
        assert sidebar.profile.currentText() == "Qwen"
        page._chk_rag.setChecked(False)
        assert not sidebar.rag.isChecked()
        # 假客户端只用于组装请求，HeldThread 不发起网络调用。
        page._client = object()
        page._jev_client = page._laya_client = None
        page._chk_tools.setChecked(False)
        sidebar.input.setPlainText("分析转速波动")
        sidebar._send()
        assert page.request_busy and not sidebar.send.isEnabled()
        assert not sidebar.input.toPlainText()
        assert not page.send_question("重复请求")
        page._on_chunk("测试回复")
        assert "测试回复" in sidebar.transcript.toPlainText()
        assert "重复请求" not in sidebar.transcript.toPlainText()
        page._on_stream_done("测试回复")
        assert sidebar.send.isEnabled() and not page.request_busy
        page._chat_display.clear()
        assert not sidebar.transcript.toPlainText()
    finally:
        sidebar.close()
        page.close()


def test_sidebar_toggle_persists_and_full_page_avoids_duplicate_view(monkeypatch, tmp_path):
    from ui_theme import appearance_manager
    from main_window import MainWindow
    monkeypatch.setattr(AIPage, "_ensure_rag_index", lambda self: None)
    monkeypatch.setattr("pages.ai_page._CONFIG_FILE", tmp_path / "missing.json")
    manager = appearance_manager()
    previous_settings = manager.settings
    manager.settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.IniFormat)
    window = MainWindow(enable_training=False)
    try:
        assert not window.appearance_bar.diagnostic_toggle.isChecked()
        assert window.diagnostic_sidebar.isHidden()
        window.appearance_bar.diagnostic_toggle.setChecked(True)
        assert not window.diagnostic_sidebar.isHidden()
        assert manager.settings.value("diagnostic/sidebar_enabled", type=bool)
        # 窗口保持隐藏，测试不启动原有页面截图过渡。
        window.nav.select_page(window.stack.indexOf(window.ai_page))
        assert window.diagnostic_sidebar.isHidden()
        assert window.appearance_bar.diagnostic_toggle.isChecked()
        window.nav.select_page(window.stack.indexOf(window.vector_page))
        assert not window.diagnostic_sidebar.isHidden()
        window.diagnostic_sidebar.closeRequested.emit()
        assert not window.appearance_bar.diagnostic_toggle.isChecked()
        assert not manager.settings.value("diagnostic/sidebar_enabled", type=bool)
    finally:
        window.close()
        QApplication.instance().processEvents()
        manager.settings = previous_settings
